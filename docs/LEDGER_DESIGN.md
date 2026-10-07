# One transactional ledger: schema and migration (DESIGN, revision 3, for review before implementation)

**Status: a proposal, nothing built.** It answers the eighth review's answer 7 and the ninth review's 12-point acceptance list, and is meant to be reviewed before implementation. Numbers in brackets, like [A3], refer to that list.

**The current specification** is `research/ledger/schema.sql` and `reference.py`, with the revision 3 and 2 sections below. The revision 1 sections after them are kept for history and are **superseded wherever they disagree** (listed at the end of revision 3).

## Revision 3 (the eleventh review): acceptance work before any engine integration

The reviewer approved the architecture and continued isolated work, not engine migration. Revision 3 makes its blockers concrete, each with tests on real SQLite (`tests/test_ledger_design.py`):

- **The opening is the observed cash.** Revision 2's worked vectors debited the legacy residual from cash a second time, although the observed cash already reflects it. Now:
  - the adoption posts the observed cash (3.472391524 SOL in the vector);
  - the residual (0.00804 SOL, the legacy books' expected cash minus the observed) is carried as `unresolved` **against the opening book**, so no cash moves;
  - the opening book's total is then the legacy expected cash.

  The test compares the result with a separate source snapshot, not with the vector's own legs. `unresolved` is a suspense item: not cash, not equity.
- **Every economic append has the caller's stable key:** `(account, kind, key)`, unique.
  - The same content again is a replay: the original is returned and nothing is written. This covers a crash after commit and before the acknowledgement, which is tested.
  - Different content under the same key is a conflict.
  - An append without a key is refused. Only an adoption and a residual, once per account, default to their kind.
- **Effects are unique** whatever event carries them: each fill (`signature:event index`) and each signature's network fee once per account. One signature can carry several fills.
- **Holdings never go negative.** Cash, reserved, inventory and rent are checked after each event, inside its transaction. External and unresolved books may go negative.
- **Seals:**
  - an event is inserted open and unsealed;
  - sealing freezes every other column;
  - startup recomputes every sealed event's payload hash, content hash, per-asset sums and postings hash, plus every holding and every stored signed transaction's hash, and fails closed on any inconsistency.

  This catches a forged seal from a bad writer, and history edited around the triggers. It's a defence against bugs and bad migrations: a writer able to rewrite the whole file, hashes included, defeats any in-file check.
- **Observations are append-only**, and carry the block height they were made at.
- **Attempts:**
  - an attempt stores the exact signed transaction (≤ 1232 bytes) with its signature and expiry, before it's sent, and the bytes are immutable;
  - its state moves only along legal transitions: signed → submitted or not sent; submitted or unknown → unknown, landed OK, landed failed, or expired absent;
  - a terminal state needs an observation of that signature;
  - expired absent needs a **finalized** absence observed above the last valid block height;
  - a restored unknown attempt blocks any new signature for its order, and there's one live sell intent per position.
- **Crash, lock and fault injection on the writer:** a crash at each point inside the transaction leaves nothing; a locked database fails the append cleanly and a later retry succeeds.

**Still before integration** (the reviewer's conditions, unchanged):
- the real order, fill, fee and outbox paths, written atomically with crash injection, disk and lock failure, replayed receipts, recovery, and exact-export tests;
- residual partial-fill accounting;
- the all-writer cutover with the actual opening, unknown orders and inventory, rent and the residual, validated against the source and chain balances;
- owner and manual-order durability, with no unjournaled emergency send.

**Superseded revision-1 statements:**
- `event_id` as a ULID: now `seq` is the commit order and the id derives from the key;
- `receipts UNIQUE(signature, commitment, source)`: now observations, keyed by a digest of everything they say, with contradictions appended;
- `fills UNIQUE(signature, event_index)` and a fee `UNIQUE(signature)`: now the `effects` table;
- an attempt's transaction **hash**: now the signed bytes plus their hash;
- migration step 3's residual: now the opening rule above.

## Revision 2 (the tenth review): DDL, protocol and worked postings

The reviewer approved the direction and asked for an executable design before implementation. It's in `research/ledger/`, still **not used by the bot**:
- `schema.sql`: the DDL;
- `reference.py`: a reference single writer;
- `posting_vectors.json`: worked events;
- `tests/test_ledger_design.py`: the tests that prove the rules below on real SQLite.

Answers to the ten points, in order:

1. **Signed postings.**
   - Amounts are canonical signed integer TEXT: `0`, `123`, `-123`, with no `+`, no leading zeros, and no `-0`. A `CHECK` enforces the form.
   - Ranges are the writer's job: a leg that is an on-chain amount is at most u64; a book may hold wider aggregates, up to 2^127.
   - Inventory is never negative in a projection. A negative would be a writer error, and the writer fails closed.
2. **Exact summation.**
   - No balance is ever computed in SQL. The single writer sums each asset's legs as exact Python integers and seals only if every asset nets to 0.
   - The seal stores those sums and the postings' sha256, and projections sum the same way.
   - The test reproduces your counterexample: SQLite's `SUM` gives a REAL; the writer gives exactly 1.
3. **Event completion.**
   - One transaction per event: insert it `open`, insert its postings, check the exact sums, seal, commit. Any failure rolls back everything.
   - Triggers:
     - postings may be inserted only into an `open` event;
     - an event's only allowed update is open → sealed, once, with its sums;
     - postings and events can never be updated or deleted.
   - Readers see sealed events only. An open event found at startup is corruption: the writer refuses to start.
4. **Contradictory receipts.** Observations are keyed by an immutable digest of everything they say: signature, event index, commitment, source, slot, error and content hash.
   - An identical observation dedupes.
   - A contradictory one from the same source is **appended** beside the first and ranked by the fork rules, never refused by a uniqueness key.
5. **Attempts versus orders.**
   - One order has any number of attempts, and a partial unique index allows at most one unresolved attempt (`signed`, `submitted`, `unknown`) per order.
   - A new signature is possible only after the old attempt is `landed_failed` or proved `expired_absent`. A rebroadcast resends the stored transaction (`tx_sha256`).
   - A partial fill updates `filled`; the residual is `target − filled`.
6. **Commit order.** `events.seq` is the database's monotone commit sequence (AUTOINCREMENT). `event_id` is a unique id, and the observed, chain and recorded times are kept separately. Projections replay in `seq` order.
7. **The migration barrier.** It's an **all-writer cutover**, not entries only:
   - stop the engine, so entries, exits, fee booking, reconciliation and the outbox all stop at once (a deploy restart already takes seconds);
   - snapshot and hash every source file;
   - import (outstanding signatures and unknowns become `unknown` attempts; the −0.00804 SOL legacy residual becomes an `unresolved` posting, as in the vectors);
   - start the new code with the database as the only authority.

   There are no parallel writers. The "shadow" comparison replays the imported opening against the source files, at an exact boundary.
8. **Journal failure.**
   - No automated entry without a durable commit.
   - A protective exit proceeds only if its own durable order commit succeeds. If storage can't commit at all: alert and halt, with **no unjournaled sell as a fallback**.
   - Bot-UI manual orders need durable commits too. The owner acting outside the bot enters through `wallet_sync` reconciliation, with unknown reservations protected.
9. **Recovery scope.**
   - **RPO:** for a process crash, the last committed transaction (WAL, `synchronous=FULL`, ext4 on the P8's SSD with honest fsync). For loss of the disk, the last 6-hourly backup.
   - **RTO:** minutes. After a restore, the wallet's chain history since the backup (`getSignaturesForAddress`) and the outbox are reconciled before any entry.
   - **Restored attempts:** a restored unresolved attempt is `unknown`, never resent as a new intent.
10. **Posting examples.** `posting_vectors.json` covers:
    - adoption and the legacy residual;
    - deposit and withdrawal;
    - reserve and release;
    - rent paid and reclaimed (a balance, not an expense);
    - a buy (swap fees inside the pool leg; the network fee once per signature);
    - a failed sell and a failed buy with no position (fee only);
    - a partial and a full sell;
    - a reset into a new account;
    - a correction, as a linked reversal (`corrects`).

    Every event nets to 0 per asset, and the test checks the end balances. A posting can't cross accounts (a trigger), and paper, live and synthetic accounts are separate rows with a `mode`.

**Still not implemented:** wiring this into the engine, fault injection against the real order paths, and a real post-adoption interval. Those come after this design is reviewed.

> **Revision 1 (history).** The sections from here on are the first, logical design. Where they disagree with revisions 2 and 3 above, those win. The superseded statements are listed at the end of revision 3, and marked "(superseded)" in place.

## Why

Today the account lives in four places:
- the JSON book (`sniper_state_paper.json`, rewritten atomically);
- the outbox (`data/orders.db`, SQLite);
- the typed account journal (`account-<mode>.jsonl`, appended after the in-memory change, with I/O errors only counted);
- the trade logs.

They're kept consistent by careful ordering, not by a transaction. The journal can miss an event, and nothing ties a fill's cash to its order row and its journal line atomically.

## The rule

**One SQLite database is the authority for the account.** The in-memory book is a projection rebuilt from it at start, and every economic change commits there before it's acted on or displayed. There's no second authority: no JSON book and no JSONL journal written alongside it. Exports are read from the database.

## Storage

- **One writer:** the engine process. Short write transactions, `BEGIN IMMEDIATE`, with `busy_timeout` bounded at 2 s. A failed write is an error, never a silent skip [A11].
- **Settings:** `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`. Schema migrations are versioned in a `migrations` table, each with its sha256 [A10].
- **Durability assumptions, written down:**
  - `fsync` on commit and on the database's directory after creation, on the P8's ext4 SSD.
  - A unit test can prove process-kill recovery, not power-loss guarantees. Power loss is covered by the backup policy below, not by a claim [A10].
- **Backups:** an online `VACUUM INTO` copy every 6 h and before every migration, kept 7 days, each with its sha256. A restore is verified by rebuilding the projection and comparing it to the live one [A10].

## Amounts [A1]

- **SOL:** integer **lamports** in `INTEGER` (fits int64).
- **Tokens:** integer **atoms**, stored as decimal `TEXT` validated by a `CHECK` (digits only, ≤ 39 characters). A token supply can exceed int64, so floats are never used.
- **Every token amount names its mint,** and every mint row has its program (legacy or Token-2022) and decimals.
- **Floats only in presentation views,** never in a stored balance.

## Tables (logical)

| table | what | uniqueness / constraints |
|---|---|---|
| `accounts` | scope: mode (`paper` / `live` / `synthetic`), chain/genesis, owner wallet, strategy or test account, the opening event | `UNIQUE(account_id)`; `CHECK(mode IN (...))`. A synthetic account's rows can't reference a live account (enforced by the foreign key plus mode checks in the posting trigger) |
| `events` | append-only economic journal: every cash or inventory change, and every lifecycle fact | `event_id` (ULID) primary key *(superseded: rev 2/3, `seq` and a key-derived id)*. Also: account, mode, `schema_version`, `code_revision`, `recorded_at` (wall), `engine_ts`, `chain_ts`, `causation_id` / `correlation_id`, `payload` (JSON), `payload_sha256`, `corrects` (the event id it reverses or replaces) [A2, A7]. No UPDATE or DELETE: a trigger raises |
| `postings` | the balanced integer effects of each event, per asset and account (cash, inventory, reserved, fees, rent, external flows) | `FOREIGN KEY(event_id)`; per event, `sum(amount)` per asset across the account's books and the external-flow book is 0 (checked in the same transaction) |
| `orders` | durable logical intent: buy/sell, mint, size, reason, policy and strategy versions, state | `state IN (intent, signed, submitted, unknown, confirmed, final, failed, expired)` [A4]; one open sell per position (partial unique index) |
| `attempts` | each signed transaction: exact signature, blockhash and expiry, serialized transaction hash *(superseded: rev 3 stores the signed bytes)*, submission state | `UNIQUE(signature)`. A retry reuses the attempt, never signs a second economic order for the same intent [A4, A5] |
| `receipts` | what the chain said about an attempt: commitment, slot, error, source, observed time | `UNIQUE(signature, commitment, source)` *(superseded: rev 2 observations keyed by digest, contradictions appended)*. The strongest evidence wins, by the fork rules |
| `fills` | a proven execution: attempt, event locator, tokens, lamports, fees broken down | `UNIQUE(signature, event_index)`. A network fee is `UNIQUE(signature)` once per transaction, never once per event *(superseded: rev 3's `effects` table)* |
| `inventory`, `lots` | projections: holdings, cost basis and unknown-basis lots, quarantined tokens | rebuilt from `postings`; a cache only [A3] |
| `quotes` | the quote observer's records (T9-E1's quote contract) | `UNIQUE(quote_id)` |
| `outbox` | durable effects with stable keys: notifications, dashboard pushes | `UNIQUE(effect_key)`. Delivery at least once; application idempotent |
| `checkpoints` | projection checkpoints: last `event_id`, its sha256, the ordering boundary, history completeness (`complete` / `folded` / `evicted` / `unknown`), source commitments | one per projection and version [A8] |
| `migrations` | the schema version and migration sha256 | applied once |

## The network boundary [A4, A5]

Never hold a write transaction while awaiting RPC:
1. **Reserve.** In one transaction: the order `intent`, the cash reservation posting, the `reserve` event.
2. **Sign.** In one transaction: the exact signed transaction, signature, blockhash and expiry stored in `attempts`, state `signed`. Then send, outside any transaction.
3. **Submit.** In one transaction: state `submitted`, or `unknown` if the send's result is lost. A restart resumes the same signature and **never creates a fresh conflicting order**. Unknown stays reserved until reconciled; it is never released on a timeout.
4. **Settle.** In one transaction: the receipt, then `fills` plus `postings`. That means the fee once per signature, proceeds, the rent change and the reservation release, plus the order state, the projection update and the outbox entries.
5. **Display only from committed state.**

## Economics [A6]

Separate event kinds, each with its own postings:
- entry cost and sell proceeds;
- attached and unattached failed fees;
- priority and base network fees;
- transfers, deposits and withdrawals;
- rent paid and reclaimed (a balance, not an expense);
- adoption and reset openings;
- wallet sync.

Swap fees already inside a fill's lamports are **not** charged again.

## Fork corrections [A7]

- **Observed market events are immutable.** A correction is a new event linked by `corrects`.
- **Our own fills and fees are facts of our own transactions.** A leader's observation changing never erases them.

## Journal failure policy [A11]

- **If a commit fails** (disk full, read-only, locked past the timeout): automated **entries stop** (fail closed), with an alert.
- **Protective exits continue** through the durable order lifecycle, provided their own commit succeeds. If even that fails, the engine halts.
- **The owner's manual trading isn't blocked by the bot's policy,** but it's recorded like any other order.

## Crash and fault injection [A9]

The test harness kills the process at every boundary:
- before and after the reserve, sign, submit and settle commits;
- between a send and its result;
- inside the settle transaction;
- after the commit but before the outbox delivery.

It also injects:
- duplicate and out-of-order receipts;
- a corrected market event;
- a midnight close;
- disk-full and read-only errors;
- a locked database, a concurrent second writer, truncated legacy input, and unavailable RPC.

**After each one,** the account rebuilt from the database must equal the uninterrupted run's balances, inventory, reservations and rows. There must be no double fill, fee or order, and unknown outcomes stay reserved.

## Export invariants [A12]

- **Closed rows reconcile exactly.** Each closed row's P&L and the account's economic P&L agree at atom precision, net of external flows, with every open or quarantined token and rent balance accounted for.
- **The exporter fails** if an invariant doesn't hold.

## Migration from the JSON book

1. **Quiesce entries.** Exits and reconciliation stay supervised.
2. **Back up.** Take hashed, immutable copies of the book, outbox, receipts, trade logs, account journal and config.
3. **Import into a new epoch:**
   - original ids and account boundaries;
   - unknown outcomes, reservations, and the opening provenance;
   - the **legacy residual (−0.00804 SOL)**, kept as an unresolved reconciliation item. No trades or fees are fabricated to make it balance. *(Revision 3: the opening is the observed cash, and the residual is carried against the opening book without moving cash.)*
4. **Prove it against the sources:**
   - cash, held inventory, reservations, closed results and per-transaction fees;
   - for a live scope, on-chain balances first.
5. **Shadow-rebuild the projections** and compare them to the running book for 24 h, read-only.
6. **Switch atomically:** select the active epoch. The old inputs become read-only. A checkpointed rollback can never resend an already-signed attempt.

## Not decided here (for the owner)

- **When to schedule it.** It touches every order path. The bot's halted paper account makes now a quiet time.
- **Whether the outbox database becomes this database or is migrated into it.**
