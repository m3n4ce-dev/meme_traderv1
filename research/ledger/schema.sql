-- The transactional ledger: DDL for review (docs/LEDGER_DESIGN.md). NOT used by the bot yet.
-- One SQLite database is the account's authority; the in-memory book is a projection of SEALED events.
-- Amounts are canonical signed integer TEXT (lamports, token atoms): '0', '123', '-123' - no '+', no leading zeros,
-- no '-0'. SQLite's SUM() on such TEXT can turn into floating point past int64, so no balance is ever computed in SQL:
-- the single writer sums exact Python integers and records the result in the seal.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS migrations (
    version     INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL,
    applied_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id  TEXT PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('paper', 'live', 'synthetic')),
    genesis     TEXT NOT NULL,                     -- the chain's genesis hash (mainnet), or 'synthetic'
    owner       TEXT NOT NULL,                     -- wallet pubkey (live), 'paper', or a synthetic label
    strategy    TEXT NOT NULL DEFAULT '',          -- a test account (T9-E1 ...) or '' for the bot's book
    opened_event INTEGER                          -- the opening/adoption event
);

CREATE TABLE IF NOT EXISTS assets (
    asset       TEXT PRIMARY KEY,                  -- 'SOL' or a mint address
    program     TEXT NOT NULL,                     -- 'native', token, or token-2022 program id
    decimals    INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 18)
);

-- Books an event posts to. Each account has its books; 'external' books are where outside flows come from/go to.
CREATE TABLE IF NOT EXISTS books (
    book_id     TEXT PRIMARY KEY,                  -- e.g. 'A1:cash', 'A1:inventory:<mint>', 'A1:fees', 'external:deposits'
    account_id  TEXT REFERENCES accounts (account_id),   -- NULL only for external books
    kind        TEXT NOT NULL CHECK (kind IN ('cash', 'reserved', 'inventory', 'fees', 'rent', 'pnl', 'external',
                                              'unresolved'))
);

-- Economic events: append-only. seq is the database commit order; event_id is a stable unique id; the times are kept
-- separately (observed / chain / recorded). An event is written 'open', gets its postings, and is sealed in the SAME
-- transaction; a reader sees only sealed events. An open event found at startup is corruption: fail closed.
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    account_id  TEXT NOT NULL REFERENCES accounts (account_id),
    kind        TEXT NOT NULL,                     -- buy, sell, failed_buy, failed_sell, reserve, release, deposit,
                                                   -- withdrawal, rent_paid, rent_reclaimed, adoption, reset,
                                                   -- fork_correction, wallet_sync, residual
    status      TEXT NOT NULL CHECK (status IN ('open', 'sealed')),
    schema_version INTEGER NOT NULL,
    code_revision TEXT NOT NULL,
    causation_id TEXT,                             -- what caused it (an attempt, a receipt, an operator action)
    correlation_id TEXT,                           -- the logical order it belongs to
    corrects    INTEGER REFERENCES events (seq),   -- a correction names the event it reverses or replaces
    observed_at REAL NOT NULL,
    chain_time  REAL,
    recorded_at REAL NOT NULL,
    payload     TEXT NOT NULL,                     -- JSON: the facts the postings came from
    payload_sha256 TEXT NOT NULL,
    seal_sums   TEXT,                              -- JSON {asset: "0"} written by the writer at seal: every asset nets 0
    seal_sha256 TEXT                               -- sha256 of the canonical postings, set at seal
);

CREATE TABLE IF NOT EXISTS postings (
    seq         INTEGER NOT NULL REFERENCES events (seq),
    leg         INTEGER NOT NULL,
    book_id     TEXT NOT NULL REFERENCES books (book_id),
    asset       TEXT NOT NULL REFERENCES assets (asset),
    amount      TEXT NOT NULL CHECK (
                    length(amount) BETWEEN 1 AND 40 AND (
                        amount = '0'
                        OR (substr(amount, 1, 1) BETWEEN '1' AND '9' AND amount NOT GLOB '*[^0-9]*')
                        OR (substr(amount, 1, 1) = '-' AND substr(amount, 2, 1) BETWEEN '1' AND '9'
                            AND substr(amount, 2) NOT GLOB '*[^0-9]*'))),
    PRIMARY KEY (seq, leg)
);

-- Append-only: no edit or removal of sealed history; postings only while their event is open.
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_only_seal BEFORE UPDATE ON events
WHEN OLD.status = 'sealed' OR NEW.status != 'sealed' OR NEW.seal_sums IS NULL OR NEW.seal_sha256 IS NULL
     OR NEW.payload IS NOT OLD.payload OR NEW.account_id IS NOT OLD.account_id OR NEW.kind IS NOT OLD.kind
BEGIN SELECT RAISE(ABORT, 'an event can only be sealed, once, with its sums'); END;
CREATE TRIGGER IF NOT EXISTS postings_no_update BEFORE UPDATE ON postings
BEGIN SELECT RAISE(ABORT, 'postings are append-only'); END;
CREATE TRIGGER IF NOT EXISTS postings_no_delete BEFORE DELETE ON postings
BEGIN SELECT RAISE(ABORT, 'postings are append-only'); END;
CREATE TRIGGER IF NOT EXISTS postings_open_event BEFORE INSERT ON postings
WHEN (SELECT status FROM events WHERE seq = NEW.seq) IS NOT 'open'
BEGIN SELECT RAISE(ABORT, 'postings only into an open event'); END;
-- an event's postings stay inside its account (and the external books): no cross-mode or cross-account legs
CREATE TRIGGER IF NOT EXISTS postings_same_account BEFORE INSERT ON postings
WHEN (SELECT account_id FROM books WHERE book_id = NEW.book_id) IS NOT NULL
 AND (SELECT account_id FROM books WHERE book_id = NEW.book_id) IS NOT (SELECT account_id FROM events WHERE seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'a posting crosses accounts'); END;

-- Orders and attempts: one logical intent, any number of signed attempts, at most ONE unresolved at a time.
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts (account_id),
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    asset       TEXT NOT NULL REFERENCES assets (asset),
    target      TEXT NOT NULL,                     -- lamports to spend, or atoms to sell (canonical integer text)
    filled      TEXT NOT NULL DEFAULT '0',         -- what's been filled so far (partial fills: the residual is target - filled)
    state       TEXT NOT NULL CHECK (state IN ('intent', 'active', 'done', 'cancelled')),
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders (order_id),
    signature   TEXT NOT NULL UNIQUE,              -- the exact signed transaction's signature
    blockhash   TEXT NOT NULL,
    last_valid_height INTEGER NOT NULL,
    tx_sha256   TEXT NOT NULL,                     -- the serialized signed transaction: a rebroadcast resends exactly it
    state       TEXT NOT NULL CHECK (state IN ('signed', 'submitted', 'unknown', 'landed_ok', 'landed_failed',
                                               'expired_absent', 'not_sent')),
    created_at  REAL NOT NULL
);
-- an order's unresolved attempt is unique: no fresh signature while one might still land
CREATE UNIQUE INDEX IF NOT EXISTS one_open_attempt ON attempts (order_id)
WHERE state IN ('signed', 'submitted', 'unknown');

-- What the chain said: every distinct observation is kept (identical ones dedupe by digest); a later contradictory
-- observation from the same source is APPENDED and ranked, never refused by a uniqueness key or overwritten.
CREATE TABLE IF NOT EXISTS observations (
    obs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,
    event_index INTEGER,
    commitment  TEXT NOT NULL CHECK (commitment IN ('processed', 'confirmed', 'finalized')),
    source      TEXT NOT NULL,                     -- method@host, no credentials
    slot        INTEGER,
    err         TEXT,
    content_sha256 TEXT,                           -- the decoded event's content, if fetched and decoded
    observed_at REAL NOT NULL,
    digest      TEXT NOT NULL UNIQUE               -- sha256(signature, index, commitment, source, slot, err, content)
);
CREATE INDEX IF NOT EXISTS observations_sig ON observations (signature, event_index);

-- Durable effects (notifications, dashboard pushes) keyed by the effect: delivered at least once, applied once.
CREATE TABLE IF NOT EXISTS outbox (
    effect_key  TEXT PRIMARY KEY,
    seq         INTEGER REFERENCES events (seq),
    payload     TEXT NOT NULL,
    delivered_at REAL
);

-- Projection checkpoints: what a projection was rebuilt from, so a restart or restore can prove it.
CREATE TABLE IF NOT EXISTS checkpoints (
    projection  TEXT PRIMARY KEY,
    last_seq    INTEGER NOT NULL,
    last_seal_sha256 TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('complete', 'folded', 'evicted', 'unknown')),
    note        TEXT NOT NULL DEFAULT ''
);
