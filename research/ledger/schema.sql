-- The transactional ledger: DDL for review (docs/LEDGER_DESIGN.md, revision 4). NOT used by the bot yet.
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
    -- the caller's STABLE key for this economic effect (rev 3, an eleventh review): a retry of the same effect - a
    -- crash after commit and before the acknowledgement, a replayed receipt - finds it and gets the original back;
    -- the same key with different content is a conflict, never a second event. Unique per account and kind.
    -- (rev 4, a twelfth review: NOT NULL again - an invariant isn't weakened to keep old fixtures; a database from an
    -- older schema is quarantined on open, see reference.py)
    effect_key  TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,                  -- sha256 of (account, kind, corrects, legs, payload, effects)
    seal_sums   TEXT,                              -- JSON {asset: "0"} written by the writer at seal: every asset nets 0
    seal_sha256 TEXT,                              -- sha256 of the canonical postings, set at seal
    UNIQUE (account_id, kind, effect_key)
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

-- Append-only: no edit or removal of sealed history; postings only while their event is open. An event is INSERTED
-- open and unsealed, and the one update allowed seals it: every other column is frozen (rev 3, an eleventh review).
-- (These are defences against a buggy writer or a bad migration; a writer able to rewrite the whole file defeats
-- any in-file check - startup recomputes every seal, see reference.py's startup_check.)
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_insert_open BEFORE INSERT ON events
WHEN NEW.status IS NOT 'open' OR NEW.seal_sums IS NOT NULL OR NEW.seal_sha256 IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an event is inserted open and unsealed'); END;
CREATE TRIGGER IF NOT EXISTS events_only_seal BEFORE UPDATE ON events
WHEN OLD.status = 'sealed' OR NEW.status IS NOT 'sealed' OR NEW.seal_sums IS NULL OR NEW.seal_sha256 IS NULL
     OR NEW.seq IS NOT OLD.seq OR NEW.event_id IS NOT OLD.event_id OR NEW.account_id IS NOT OLD.account_id
     OR NEW.kind IS NOT OLD.kind OR NEW.schema_version IS NOT OLD.schema_version
     OR NEW.code_revision IS NOT OLD.code_revision OR NEW.causation_id IS NOT OLD.causation_id
     OR NEW.correlation_id IS NOT OLD.correlation_id OR NEW.corrects IS NOT OLD.corrects
     OR NEW.observed_at IS NOT OLD.observed_at OR NEW.chain_time IS NOT OLD.chain_time
     OR NEW.recorded_at IS NOT OLD.recorded_at OR NEW.payload IS NOT OLD.payload
     OR NEW.payload_sha256 IS NOT OLD.payload_sha256 OR NEW.effect_key IS NOT OLD.effect_key
     OR NEW.content_sha256 IS NOT OLD.content_sha256
BEGIN SELECT RAISE(ABORT, 'an event can only be sealed, once, with its sums - nothing else about it changes'); END;
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

-- Per-effect uniqueness (rev 3): each fill (signature + its event's place) and each signature's network fee is
-- booked once per account, whatever event carries it - one signature can hold several fills, so it isn't the key.
CREATE TABLE IF NOT EXISTS effects (
    account_id  TEXT NOT NULL REFERENCES accounts (account_id),
    effect      TEXT NOT NULL CHECK (effect IN ('fill', 'network_fee', 'rent', 'transfer')),
    locator     TEXT NOT NULL,                     -- fill: '<signature>:<event index>'; network_fee: '<signature>'
    seq         INTEGER NOT NULL REFERENCES events (seq),
    PRIMARY KEY (account_id, effect, locator)
);
CREATE TRIGGER IF NOT EXISTS effects_open_event BEFORE INSERT ON effects
WHEN (SELECT status FROM events WHERE seq = NEW.seq) IS NOT 'open'
  OR (SELECT account_id FROM events WHERE seq = NEW.seq) IS NOT NEW.account_id
BEGIN SELECT RAISE(ABORT, 'effects only into an open event of their account'); END;
CREATE TRIGGER IF NOT EXISTS effects_no_update BEFORE UPDATE ON effects
BEGIN SELECT RAISE(ABORT, 'effects are append-only'); END;
CREATE TRIGGER IF NOT EXISTS effects_no_delete BEFORE DELETE ON effects
BEGIN SELECT RAISE(ABORT, 'effects are append-only'); END;

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
    block_height INTEGER,                          -- the chain's block height when observed (an absence's proof)
    history_searched INTEGER NOT NULL DEFAULT 0 CHECK (history_searched IN (0, 1)),   -- a full-history lookup
    -- what it PROVES (rev 4): typed, never read from an error string alone. success: landed, no error; executed
    -- failure: landed with an error; absent: not found by a full-history search; inconclusive: anything else
    outcome     TEXT NOT NULL CHECK (outcome IN ('success', 'executed_failure', 'absent', 'inconclusive')),
    observed_at REAL NOT NULL,
    digest      TEXT NOT NULL UNIQUE,              -- sha256 of every field above: recomputed at startup
    CHECK ((outcome = 'success' AND slot IS NOT NULL AND err IS NULL)
        OR (outcome = 'executed_failure' AND slot IS NOT NULL AND err IS NOT NULL AND err != 'absent')
        OR (outcome = 'absent' AND slot IS NULL AND history_searched = 1 AND block_height IS NOT NULL)
        OR outcome = 'inconclusive')
);
CREATE INDEX IF NOT EXISTS observations_sig ON observations (signature, event_index);
-- observations are immutable (rev 3): a new answer is a new row
CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;

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
-- one live sell intent per position (rev 3): two concurrent sells of one holding can't both be signed
CREATE UNIQUE INDEX IF NOT EXISTS one_live_sell ON orders (account_id, asset)
WHERE side = 'sell' AND state IN ('intent', 'active');
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders (order_id),
    signature   TEXT NOT NULL UNIQUE,              -- the exact signed transaction's signature
    blockhash   TEXT NOT NULL,
    last_valid_height INTEGER NOT NULL,
    -- the exact signed transaction, durable BEFORE it's sent (rev 3: a hash alone can't be rebroadcast); the writer
    -- checks tx_sha256 = sha256(signed_tx) when it stores it and at every startup. 1232 bytes: Solana's packet limit
    signed_tx   BLOB NOT NULL CHECK (typeof(signed_tx) = 'blob' AND length(signed_tx) BETWEEN 1 AND 1232),
    tx_sha256   TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('signed', 'submitted', 'unknown', 'landed_ok', 'landed_failed',
                                               'expired_absent', 'not_sent')),
    resolved_by INTEGER REFERENCES observations (obs_id),   -- the chain observation that settled it (terminal states)
    created_at  REAL NOT NULL
);
-- an attempt is inserted 'signed'; afterwards only its state (and the evidence that settled it) moves, along legal
-- transitions: signed -> submitted | not_sent; submitted | unknown -> unknown | landed_ok | landed_failed |
-- expired_absent. A terminal state needs an observation of this signature; expired_absent needs a FINALIZED
-- observation that it's absent, made above its last valid block height (proof, not a timeout).
CREATE TRIGGER IF NOT EXISTS attempts_insert_signed BEFORE INSERT ON attempts
WHEN NEW.state IS NOT 'signed' OR NEW.resolved_by IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an attempt is recorded signed, before it is sent'); END;
CREATE TRIGGER IF NOT EXISTS attempts_frozen BEFORE UPDATE ON attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.order_id IS NOT OLD.order_id OR NEW.signature IS NOT OLD.signature
  OR NEW.blockhash IS NOT OLD.blockhash OR NEW.last_valid_height IS NOT OLD.last_valid_height
  OR NEW.signed_tx IS NOT OLD.signed_tx OR NEW.tx_sha256 IS NOT OLD.tx_sha256 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'an attempt''s transaction is immutable'); END;
-- a settled attempt is final (rev 4): its state and the observation that proved it are frozen together; the proof
-- reference is set only by the update that settles it. A correction is new evidence, never an overwrite.
CREATE TRIGGER IF NOT EXISTS attempts_settled_final BEFORE UPDATE ON attempts
WHEN OLD.state IN ('landed_ok', 'landed_failed', 'expired_absent', 'not_sent')
  OR (NEW.resolved_by IS NOT OLD.resolved_by
      AND NOT (OLD.resolved_by IS NULL AND NEW.state IN ('landed_ok', 'landed_failed', 'expired_absent')))
BEGIN SELECT RAISE(ABORT, 'a settled attempt and its proof are final'); END;
CREATE TRIGGER IF NOT EXISTS attempts_transition BEFORE UPDATE OF state ON attempts
WHEN NOT ((OLD.state = 'signed' AND NEW.state IN ('submitted', 'not_sent'))
       OR (OLD.state IN ('submitted', 'unknown') AND NEW.state IN ('unknown', 'landed_ok', 'landed_failed',
                                                                   'expired_absent')))
BEGIN SELECT RAISE(ABORT, 'not a legal attempt transition'); END;
-- a terminal state needs a FINALIZED observation of this signature whose typed outcome is that state's (rev 4):
-- landed_ok <- success; landed_failed <- executed_failure; expired_absent <- absent (a full-history search, made above
-- the last valid block height). A processed or confirmed answer is recorded, but never settles - so it never frees
-- the order for a fresh signature.
CREATE TRIGGER IF NOT EXISTS attempts_evidence BEFORE UPDATE OF state ON attempts
WHEN NEW.state IN ('landed_ok', 'landed_failed', 'expired_absent') AND NOT EXISTS (
     SELECT 1 FROM observations o WHERE o.obs_id = NEW.resolved_by AND o.signature = OLD.signature
        AND o.commitment = 'finalized'
        AND ((NEW.state = 'landed_ok' AND o.outcome = 'success')
          OR (NEW.state = 'landed_failed' AND o.outcome = 'executed_failure')
          OR (NEW.state = 'expired_absent' AND o.outcome = 'absent' AND o.block_height > OLD.last_valid_height)))
BEGIN SELECT RAISE(ABORT, 'a terminal attempt state needs the finalized chain observation that proves it'); END;
-- every state an attempt passes through, append-only (written by the database itself)
CREATE TABLE IF NOT EXISTS attempt_transitions (
    n           INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  TEXT NOT NULL REFERENCES attempts (attempt_id),
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    resolved_by INTEGER REFERENCES observations (obs_id)
);
CREATE TRIGGER IF NOT EXISTS attempts_history AFTER UPDATE OF state ON attempts
BEGIN INSERT INTO attempt_transitions (attempt_id, from_state, to_state, resolved_by)
      VALUES (NEW.attempt_id, OLD.state, NEW.state, NEW.resolved_by); END;
CREATE TRIGGER IF NOT EXISTS attempt_transitions_no_update BEFORE UPDATE ON attempt_transitions
BEGIN SELECT RAISE(ABORT, 'attempt history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempt_transitions_no_delete BEFORE DELETE ON attempt_transitions
BEGIN SELECT RAISE(ABORT, 'attempt history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
-- an order's unresolved attempt is unique: no fresh signature while one might still land
CREATE UNIQUE INDEX IF NOT EXISTS one_open_attempt ON attempts (order_id)
WHERE state IN ('signed', 'submitted', 'unknown');

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
