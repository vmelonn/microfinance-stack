# Microfinance transaction stack — build roadmap

A learning build of a card/account transaction stack: REST on the outside,
ISO 8583 on the inside, built stage by stage so each layer is testable before
the next one depends on it.

## Stage checklist

- [x] **Stage 1 — Message layer** (`iso8583/`)
      Build/parse raw ISO 8583 messages: MTI, bitmap, BCD-packed data elements.
      Already built — see `iso8583/parser.py` and `iso8583/DE_REFERENCE.md`.
      Hardening note: FIXED-length binary fields (DE 52 PIN block, DE 64/96/128
      MAC fields) were being `.rstrip()`'d on decode -- correct for
      space-padded TEXT fields like DE 43's merchant name, wrong for binary
      content. Random ciphertext that happened to end in a byte decoding to
      a whitespace codepoint would have its trailing byte(s) silently
      stripped, corrupting the value. Found via a genuinely flaky test
      failure -- rare enough to hide for a long time, since it only
      triggers when encrypted output happens to end in the right byte.
      Fixed by adding a `binary` flag to `FieldSpec`, skipping `.rstrip()`
      for those four DEs specifically. Regression-tested deterministically
      (not relying on random chance) in
      `iso8583/tests/test_binary_field_integrity.py`.

- [x] **Stage 2 — Connection layer** (`switch/`)
      A TCP client that frames messages with a 2-byte MLI (length header) and
      sends/receives them over a socket, plus a **host simulator** — a fake
      switch we run locally that responds to `0200` with a scripted `0210`,
      so we can test the whole stack without a real bank connection.
      Done — see `switch/framing.py`, `switch/client.py`, `switch/host_simulator.py`,
      and the round-trip proof in `tests/integration/test_layer2_roundtrip.py`.
      Hardening note 1: `host_simulator.py` originally blocked forever in
      `accept()`, which meant `stop()` couldn't actually free the listening
      port before a new one tried to bind it (closing a socket from another
      thread doesn't interrupt a blocking `accept()` call on Linux). Fixed
      by giving the socket a timeout so the accept loop wakes up
      periodically to check a stop flag. Regression-tested in
      `switch/tests/test_host_simulator_shutdown.py`.
      Hardening note 2: `client.py`'s connection loop originally only caught
      `ConnectionError`/`OSError` around the receive loop. Any other
      exception (found via a real Windows deployment) would escape
      uncaught, killing the receive thread WITHOUT clearing `_connected` --
      leaving the client looking connected (sends, including heartbeats,
      kept succeeding) while nothing could ever be received again. Fixed
      by catching broadly and using try/finally to guarantee `_connected`
      always clears, so the client reliably notices and reconnects.
      Regression-tested in `switch/tests/test_client_reconnect_on_error.py`.
      Hardening note 3: fixing note 1 introduced a NEW bug -- a connection
      accepted via `accept()` inherits the LISTENING socket's timeout by
      default. Setting that timeout to 0.5s (for note 1) meant every
      accepted connection also silently got a 0.5s read timeout, so any
      connection idle for more than 0.5s (i.e. every real connection --
      heartbeats are tens of seconds apart) got dropped and immediately
      reconnected, in an endless loop, found via a real deployment stuck
      permanently reconnecting. Fixed by explicitly calling
      `conn.settimeout(None)` right after `accept()`, so only the listening
      socket keeps the short timeout. Regression-tested in
      `switch/tests/test_idle_connection_survives.py`.
      Hardening note 4: `close()` calling `self._sock.close()` while the
      client's receive thread was blocked in `recv()` raised `WinError
      10053` on Windows (harmless, but noisy -- the same close-from-
      another-thread issue as note 1, just on the client side). Fixed with
      `select.select(..., timeout=1.0)` to wait for readability before
      reading, rather than putting a timeout directly on the socket -- a
      socket-level timeout risked firing mid-way through a multi-byte
      `read_exact()` call and corrupting the byte stream for every message
      after it, so `select()` only avoids blocking forever, while the
      actual read stays fully blocking once data is confirmed available.

- [x] **Stage 3 — Correlation layer** (`correlation/`)
      Tracks in-flight requests by STAN, matches responses back to their
      original caller, applies a timeout, and fires a reversal (`0400`) if a
      request times out with no response.
      Done — see `correlation/tracker.py` (CorrelationManager, send_and_wait)
      and `correlation/reversal.py` (builds the DE 90 reversal reference).
      Proven end-to-end in `tests/integration/test_layer3_correlation.py`,
      covering both a normal matched response and a timeout that triggers
      an automatic reversal.

- [x] **Stage 4 — Security layer** (`security/`)
      A mock HSM/key-management module for learning — same interface a real
      HSM client would expose (encrypt PIN block, verify MAC), but backed by
      local test keys instead of real hardware.
      Done — see `security/pin_block.py` (ISO 9564 Format 0 PIN block),
      `security/mock_hsm.py` (MockHSM: per-transaction key derivation,
      encrypt/decrypt, MAC), and `security/tests/test_mock_hsm.py`, which
      proves an encrypted PIN block survives a full DE 52 build/parse cycle
      through the Layer 1 message parser.

- [x] **Stage 5 — REST API layer** (`api/`)
      FastAPI service exposing endpoints like `POST /transactions/purchase`.
      This is what a mobile app would call; internally it drives stages 2-4.
      Done — see `api/main.py` (wires up the connection, correlation, and
      security layers via FastAPI's lifespan), `api/routes/transactions.py`
      (idempotency check → PIN encryption → message build → send_and_wait →
      JSON response), `api/schemas.py`, and `api/tests/test_transactions.py`,
      which proves a purchase goes approved end-to-end and that a repeated
      idempotency key never reaches the switch a second time.
      Run it for real with: `uvicorn api.main:app --reload` (from the repo root).

- [x] **Stage 6 — Ledger layer** (`ledger/`)
      A double-entry ledger (SQLite to start) that actually moves money once
      an ISO 8583 response comes back approved.
      Done — see `ledger/db.py` (schema: `transactions` PRIMARY KEY on RRN,
      `ledger_entries` for the debit/credit postings), `ledger/service.py`
      (`record_purchase`, `get_balance`, `is_balanced`), and
      `ledger/tests/test_ledger.py`, which includes a genuine concurrency
      test firing 10 simultaneous requests at the same RRN and confirming
      exactly one is recorded. Wired into `api/routes/transactions.py`:
      every approved purchase now generates an RRN, records it in the
      ledger, and the response reports `ledger_status`. Proven end-to-end
      in `api/tests/test_ledger_integration.py`.

- [x] **Stage 7 — Risk layer** (`risk/`)
      Basic fraud rules (velocity, amount thresholds, entry-mode checks)
      evaluated before a transaction is sent out over ISO 8583.
      Done — see `risk/rules.py` (`RiskEngine.evaluate`: velocity tracked
      per card, amount thresholds, manual-entry escalation, each rule able
      to push the outcome toward approve/review/decline) and
      `risk/tests/test_risk_engine.py`. Wired into `api/routes/transactions.py`
      as the very first gate — before security, message building, or
      correlation. Proven in `api/tests/test_risk_gate.py`: a declined or
      flagged transaction never reaches the switch simulator or the ledger
      at all.

- [x] **Stage 8 — Ops layer** (`ops/`)
      Audit logging (every message in/out, PAN/PIN masked) and a daily
      reconciliation job comparing ledger totals against settlement.
      Done — see `ops/audit_log.py` (`mask_fields`, `AuditLogger`: PAN
      truncated to last 4, PIN block/MAC/EMV data never logged at all),
      wired directly into `switch/client.py` so every outbound/inbound
      message is captured automatically. `ops/reconciliation.py`
      (`reconcile`: matches ledger vs. settlement file by RRN, catching
      matches, ledger-only, settlement-only, and amount mismatches) and
      `ops/run_reconciliation.py`, a CLI entry point suitable for a
      Kubernetes CronJob — exits 1 on any unclean result. Tested in
      `ops/tests/test_audit_log.py` (including a real socket connection,
      not just the masking function in isolation) and
      `ops/tests/test_reconciliation.py` (all four outcome categories plus
      a realistic mixed batch).

**All 8 stages are now built and tested, individually and wired together end-to-end.**

## Directory layout

```
microfinance-stack/
├── README.md                  <- this file, the roadmap
├── iso8583/                   <- Stage 1: message build/parse (done)
│   ├── parser.py
│   ├── DE_REFERENCE.md
│   └── tests/
├── switch/                    <- Stage 2: TCP client + host simulator (done)
│   ├── framing.py              <- MLI read/write helpers
│   ├── client.py               <- ISO8583Client: connect, sign-on, heartbeat, reconnect
│   └── host_simulator.py       <- fake switch for local testing
├── correlation/                <- Stage 3: STAN matching, timeouts, reversal (done)
│   ├── tracker.py               <- CorrelationManager: send_and_wait(), timeout handling
│   └── reversal.py              <- builds the DE 90 reversal reference
├── security/                  <- Stage 4: mock HSM / key management (done)
│   ├── pin_block.py             <- ISO 9564 Format 0 PIN block formatting
│   ├── mock_hsm.py               <- MockHSM: key derivation, encrypt/decrypt, MAC
│   └── tests/
│       └── test_mock_hsm.py
├── api/                        <- Stage 5: FastAPI REST layer (done)
│   ├── main.py                  <- app + lifespan wiring (client, correlator, hsm, jwt_secret)
│   ├── schemas.py                <- Purchase/Transfer/User/Login/Token request+response models
│   ├── routes/
│   │   ├── transactions.py       <- purchase, transfer, balance, ledger reset
│   │   ├── users.py               <- POST /users/register
│   │   ├── auth.py                <- POST /auth/login, POST /auth/set-password
│   │   └── health.py             <- GET /health
│   └── tests/
│       ├── _auth_helpers.py       <- shared register_and_login() for other test files
│       ├── test_transactions.py
│       ├── test_ledger_integration.py
│       ├── test_risk_gate.py
│       └── test_auth_integration.py
├── auth/                        <- Extension: JWT authentication + authorization (done)
│   ├── passwords.py               <- PBKDF2-HMAC-SHA256 hashing, separate from the card PIN
│   ├── tokens.py                  <- hand-rolled HS256 JWT: create_token, decode_token
│   ├── dependencies.py            <- get_current_user, the FastAPI Depends() every protected route uses
│   └── tests/
│       └── test_auth_primitives.py
├── cache/                       <- Extension: Redis-backed idempotency + risk state (done)
│   ├── idempotency_store.py       <- InMemory/RedisIdempotencyStore, atomic claim()/store_response()
│   ├── velocity_tracker.py        <- InMemory/RedisVelocityTracker, sliding-window sorted set
│   └── tests/
│       ├── test_idempotency_store.py
│       └── test_velocity_tracker.py
├── ledger/                    <- Stage 6: double-entry ledger (done)
│   ├── db.py                    <- SQLite schema: users, accounts, cards, transactions, ledger_entries
│   ├── service.py                <- record_purchase, get_balance, is_balanced
│   └── tests/
│       └── test_ledger.py
├── risk/                       <- Stage 7: fraud/risk rules (done)
│   ├── rules.py                  <- RiskEngine: velocity, amount, entry-mode checks
│   └── tests/
│       └── test_risk_engine.py
├── ops/                         <- Stage 8: audit logging, reconciliation (done)
│   ├── audit_log.py               <- AuditLogger, mask_fields
│   ├── reconciliation.py          <- reconcile(): matches ledger vs. settlement by RRN
│   ├── run_reconciliation.py      <- CLI entry point (CronJob-ready, exits 1 if unclean)
│   └── tests/
│       ├── test_audit_log.py
│       └── test_reconciliation.py
└── tests/
    └── integration/            <- end-to-end tests once multiple stages exist
```

## Extensions beyond the 8-layer foundation

The core 8 layers are the learning build. Three real feature extensions
have been added on top, each following the same "own directory, own
tests" pattern as everything else:

- **Identity model** (`ledger/db.py`, `api/routes/users.py`) — a proper
  `users → accounts → cards` hierarchy replaced the original bare-string
  account identifiers. `POST /users/register` creates all three in one
  atomic transaction. `resolve_account()` in `transactions.py` translates
  a card number or raw account ID into the real ledger account before
  anything else runs.

- **Authentication & authorization** (`auth/`) — JWT-based login, kept
  deliberately separate from the card PIN (one is app-login, checked
  locally; the other is transaction-time, checked by the switch). Every
  money-moving endpoint now requires a valid token (`Depends(get_current_user)`)
  *and* an ownership check (`_assert_owns_account`) confirming the caller
  actually owns the account they're trying to use — auth without that
  second check would only prove *who* is calling, not that they're
  *allowed* to touch this specific card.

- **Redis-backed idempotency & risk state** (`cache/`) — the API's
  idempotency cache and the risk engine's velocity tracking both used to
  live in a single process's memory, which breaks the moment you run more
  than one replica (a retry landing on a different pod, or an attacker
  spreading attempts across replicas, would be invisible to whichever pod
  didn't see the first attempt). `cache/idempotency_store.py` and
  `cache/velocity_tracker.py` each define a swappable interface with two
  implementations — in-memory (today's behavior, zero setup, selected by
  default) and Redis (shared across every replica, selected by setting
  `REDIS_URL`). The Redis idempotency store also closes a genuine race the
  *original* code had: the old pattern was check-then-process-then-store,
  three separate steps, so two concurrent requests with the same brand-new
  key could both slip through before either finished storing. `claim()` is
  now atomic via Redis's `SET NX`, the same category of guarantee the
  ledger's `PRIMARY KEY` gave against double-processing, just enforced one
  layer up. Proven under a genuine 20-thread concurrent race against a
  real Redis server in `cache/tests/test_idempotency_store.py`.

- **Key management service** (`security/kms.py`) — `MockHSM.base_key` used
  to be `os.urandom()`, regenerated fresh every process start, which meant
  anything encrypted before a restart became permanently undecryptable
  after one. `KeyManagementService` is the same swappable-interface
  pattern again: `LocalKeyManagementService` does real AES-256-GCM
  envelope encryption (not the mock XOR the rest of the HSM uses) to wrap
  and persist the HSM's base key across restarts, and
  `AWSKeyManagementService` is a correct, boto3-shaped implementation for
  real AWS KMS — genuinely untestable in a sandboxed environment with no
  network route to AWS's control plane, but ready to activate with real
  credentials. Set `HSM_KEY_PERSISTENCE_PATH` and `HSM_MASTER_KEY_HEX` to
  turn this on; proven in `security/tests/test_kms.py` by simulating a
  full process restart and successfully decrypting a PIN block encrypted
  before it.

- **Containerization** (`Dockerfile`, `docker-compose.yml`) — a multi-stage
  build (dependencies installed in a build stage, only the installed
  packages copied into a clean runtime stage), running as a non-root user,
  with a real health check against `/health`. `docker-compose.yml` runs
  **two app replicas sharing one Redis container** -- the actual point of
  everything built in the Redis phase, finally provable as more than one
  process. `verify_cross_replica.py` proves it directly: a purchase made
  through one replica, retried with the same idempotency key through the
  *other* replica (which has never seen that user or card in its own
  database), returns the identical cached result -- and risk velocity
  correctly escalates even when rapid attempts are split across both
  replicas. Docker itself isn't available in the sandbox this was built
  in, so the Dockerfile's dependency-install step and the app's boot
  sequence were verified directly (in a clean venv, with the exact
  environment variables the container sets) instead -- catching a real
  missing dependency (`cryptography` was missing from `requirements.txt`)
  before it could break an actual build. Run it yourself with:
  ```
  docker compose up --build
  python3 verify_cross_replica.py
  ```

- **Analytics warehouse** (`analytics/`) — the operational ledger (SQLite)
  is optimized for "does this account have the funds, right now" -- a few
  rows per transaction. A real warehouse is optimized for the opposite:
  scanning everything at once for reporting. Real systems export from one
  into the other precisely because a single engine being good at both jobs
  is rare.

  **This was Redshift, and is now ClickHouse.** `RedshiftWarehouse` was
  correct-shaped code that could never actually be run -- no AWS account, no
  cluster, no credentials -- so it sat in exactly the position
  `AWSKeyManagementService` still occupies: honest about being untestable,
  and untested. ClickHouse self-hosts in a container, so
  `analytics/tests/test_warehouse.py` now executes real SQL against a real
  server instead of asserting behaviour by reading the code. For the one
  component whose entire job is to be trusted with reporting numbers, that
  is a strictly better position. The swap was cheap because the
  `DataWarehouse` interface already existed -- which is the whole reason it
  existed.

  The loading model inverts, and it is worth knowing why: Redshift punishes
  row-by-row `INSERT` and wants `COPY FROM S3`, meaning an S3 bucket, an IAM
  role, a staging table, and `MERGE` grammar. ClickHouse wants large batched
  inserts and needs none of that -- several hundred lines of planned
  infrastructure simply disappeared. Idempotent re-loading comes from
  `ReplacingMergeTree(loaded_at)` keyed on `(transaction_ts, rrn)`;
  `PARTITION BY toYYYYMM` makes the 7-year retention TTL a metadata-only
  partition drop; and a `SummingMergeTree` materialized view keeps daily
  volume as a single-digit-millisecond read at any table size.

  Two ClickHouse traps are pinned by tests rather than discovered later:
  deduplication is **eventual**, happening at background merge time, so any
  query that must not see duplicates says `FINAL`; and a materialized view
  fires on `INSERT`, **before** that deduplication, so a re-loaded row
  double-counts in the aggregate even though the fact table self-corrects.
  That second one is why `sync_to_warehouse.py` advances its watermark only
  after a batch is confirmed — the watermark is what keeps the aggregates
  honest, not merely what makes the sync fast.

  The original timestamp bug is still fixed, differently and more simply.
  Comparing timestamps as raw strings across SQLite and Postgres used to
  break incremental sync, because Postgres's plain `TIMESTAMP` drops the UTC
  offset SQLite's string format carries -- every sync re-processed the same
  rows forever, saved from real duplicates only by `ON CONFLICT DO NOTHING`,
  which is luck rather than correctness. The watermark is now stored as the
  **exact string the source emitted** and fed back verbatim, so there is no
  parse and nothing to lose in translation.

- **IBM ACE transport** (`switch/transport.py`, `switch/soap_client.py`) —
  a second way for a transaction to reach the switch, selected by
  `ISO8583_TRANSPORT=direct|ace`.

  `direct` is everything Stages 1-3 built: this process owns the codec, the
  socket, and STAN correlation. `ace` hands a SOAP request to an IBM App
  Connect Enterprise integration server, which parses and serializes ISO
  8583 from a DFDL schema and holds the TCP connection itself. Under `ace`
  this process opens no socket to the switch at all.

  `api/routes/transactions.py` cannot tell the difference — it calls
  `transport.authorize(...)` with no MTI, no DE numbers, and no STAN,
  because under ACE it never sees any of them. The interface is narrow
  specifically so the two stay interchangeable.

  Stages 1-3 are **not** dead code once ACE arrives. The Python codec is the
  executable specification the DFDL schema has to agree with, it is what the
  test suite runs against, and it is what still works when the integration
  server is down. Deleting it would trade a tested implementation for an
  untested one.

  The IBM entitlement had not come through, so the ACE artifacts (DFDL
  schema, WSDL, ESQL, message-flow spec) live in the companion
  `microfinance-microservices` repository under `ace/`, alongside
  `ace-stub` — a Python service serving the identical WSDL that this
  transport can be pointed at today with no licence at all.

  This also introduced a third outcome. `AuthorizationResult.outcome` is
  `approved`, `declined`, or **`unknown`** — because a call can succeed
  while its response is lost, leaving the cardholder possibly debited for a
  transaction we cannot confirm. On `unknown` the ledger posting is skipped
  entirely and the response says so; `ops/reconciliation.py` is the backstop
  that catches it against the switch's own settlement file. The monolith
  previously collapsed that case into an error, which quietly meant
  reporting failure for money that may genuinely have moved.

## How we'll work through it

Each stage gets built as its own small, testable module before we wire it
into the next one — so at every point you have something that runs, not just
scaffolding. Next up: **Stage 2, the connection layer and host simulator.**
