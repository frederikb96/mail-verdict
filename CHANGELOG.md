# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **PostgreSQL image now ships pgvector** (`pgvector/pgvector:pg18`), in both compose files and the test container. The stock image carries no `vector` extension, and pgvector is not a trusted extension so it cannot be added by an unprivileged role at runtime. Same major version and data directory as before, so an existing volume mounts unchanged. Deployments supplying their own PostgreSQL must provide the extension; on Kubernetes that means a vector-enabled image.

### Fixed

- **Contract-owned columns are no longer written.** `outbox.status`, `outbox.attempts` and `accounts.state` are managed by PostIMAP, but the ORM models carried Python-side defaults for them, which SQLAlchemy sends on every insert regardless of what the calling code specifies. The tables carry table-level insert grants, so those writes were accepted rather than refused, and only matched PostIMAP's own initial values by coincidence — a divergence would have left outbox rows the processor never claims, so mail would have stopped sending with nothing reporting an error.
- **Account health:** an account in `error` that has completed a full sync before is shown as `Retrying` rather than as a failure, since PostIMAP retries it unboundedly and it recovers on its own. Only an account that has never once synced is presented as needing attention.
- **Spam prompt:** removed the description of a `neighbors` input that is never sent, so the classifier is no longer instructed to weigh context it does not receive.

## [1.0.0] - 2026-08-30

### Breaking Changes

- **PostIMAP integration:** Entire IMAP sync layer (~4,700 LOC) replaced with the PostIMAP microservice — MailVerdict is now a pure PostgreSQL application, targeting PostIMAP's versioned contract (`contract_version` 1, service `1.1.0`)
- **`Mail` → `Message`:** Model renamed, table `mails` → `messages`
- **`uid` → `imap_uid`:** IMAP UID field renamed across entire stack; now nullable, `NULL` meaning an optimistic move is pending (surfaced in the API as `pending_sync`)
- **`is_read` → `is_seen`:** Read state field renamed across entire stack
- **DB-centric architecture:** API layer is now pure DB reads/writes — zero IMAP imports
- **`is_deleted` → `expunged_at`:** Message soft-delete moved from a boolean to a nullable timestamp, matching PostIMAP's own column rename
- **Fresh Alembic migration:** All migration history squashed to a single v1 baseline against an empty database (no upgrade path from pre-contract schemas — PostIMAP re-syncs from IMAP)
- **Folder counts:** Maintained by PostIMAP's own triggers
- **Semantic search removed:** Qdrant, OpenAI embeddings, and the `mode=semantic` search/MCP option are gone; full-text search (`websearch_to_tsquery('simple', ...)`) covers v1
- **LLM provider switched to Anthropic:** `openai` and `qdrant-client` dependencies dropped, `anthropic` added; default model `claude-haiku-4-5`, configurable via the `ai` settings category
- **Credential handling:** MailVerdict no longer encrypts anything — it always writes the contract's plaintext format (`0x00` prefix byte); PostIMAP encrypts credentials itself when its own key is configured. `MAIL_VERDICT_ENCRYPTION_KEY` and the `core/encryption.py` module are gone
- **Owned tables carry no foreign keys onto PostIMAP tables:** the consumer database role has no `REFERENCES` grant, and verdict history must outlive PostIMAP's retention purge of expunged messages
- **Config loader rewritten:** pydantic-validated, `${VAR}` placeholder resolution, arbitrary-depth `MAIL_VERDICT_<SECTION>_<KEY>` env overrides, fail-fast on missing required values (no more silently defaulted `cors_origins`)

### Added

- **`postimap/` package:** the only module that knows the PostIMAP contract — `contract.py` (version handshake), `listener.py` (LISTEN `postimap_events`, typed event parsing, reconnect/keepalive), `commands.py` (`postimap_commands` sync requests), `actions.py` (every contract write in one place)
- **Threading:** `messages.thread_id` is now mirrored and indexed; `GET /api/accounts/:id/messages?threaded=true` returns one row per conversation (latest message, `thread_count`, `unread_in_thread`), and `GET /api/messages/:id/thread` returns every message in the conversation across folders, ascending
- **Attachment streaming:** `GET /api/messages/:id/attachments/:attachment_id` streams from `attachments.data` with content type and a download disposition; inline `cid:` references in message HTML are rewritten to this endpoint
- **Outbox:** `POST /api/outbox` sends or drafts a message (JSON, or multipart when attachments are present) and `GET /api/outbox` lists outbox rows for the send/draft status view — inserting into `outbox` is the only way this application originates mail
- **Bulk actions:** `POST /api/accounts/:id/messages/bulk-action` applies one action to many messages, by an explicit id list or by a server-resolved scope (folder + read/unread filter + exclusions) — replaces the deleted server-side selection state
- **Folder preferences:** `PATCH /api/folders/:id/prefs` consolidates visibility, display name, unified name, and special-use override into the folder's one write surface
- **Read-time HTML sanitization:** `GET /api/messages/:id` now runs the nh3 sanitizer on `body_html` at read time — PostIMAP owns message inserts, so store-time sanitization is no longer possible
- **Search snippets:** `GET /api/search` highlights matches via `ts_headline` against the same coalesced subject/sender/body text the generated `search_vector` column indexes on
- **MCP tools:** `get_thread`, `list_mails`, `mark_mail`, `submit_spam_feedback`, `send_mail`, `draft_mail` — `send_mail` is the first capability letting an MCP client actually send mail through the outbox
- **Passive "moved out of junk" feedback:** `postimap_events` carries `old_folder_id` on a `folder_id`-changing update (PostIMAP 1.1.0); `spam/processor.py` records a correction when a move's source folder was junk and its destination isn't, the same path the explicit not-spam control takes. Only fires for moves made inside this application — a move made in another mail client is an expunge in the source plus a separate insert in the destination, not a `folder_id` change, so it carries no `old_folder_id` and no signal fires; documented in `spam/processor.py`'s module docstring
- **`is_truncated` surfaced:** oversized messages (over `storage.max_message_bytes`) are now distinguishable in the API
- **Verdict durability gate:** partial unique index on `(account_id, message_id_hdr) WHERE source = 'ai'` — an AI verdict is never reissued for the same message header, surviving both retention purge and a UIDVALIDITY resync
- **`FakeSpamAnalyst`:** deterministic, keyword-driven `SpamAnalyst` implementation for tests and API-key-free local development
- **`tests/setup/`:** testcontainers-based test infrastructure — container runtime bootstrap (rootless podman or a standard Docker/DinD socket, fails loudly with the fix command otherwise), pinned image tags, session-scoped Postgres + PostIMAP fixtures
- **`tests/e2e/`:** end-to-end scenarios against the full stack with the application in-process — account onboarding (including the assertion that a first sync of an existing mailbox produces zero verdicts), mail actions with one case asserted all the way onto the real IMAP server, and the compose flow with delivery asserted against a real SMTP sink
- **`tests/pg/`:** integration tests against a real Postgres + PostIMAP — Alembic migrating cleanly next to PostIMAP's own schema, the contract-version gate (pass and mismatch), and `postimap/actions.py`'s SQL round-tripping (move, expunge, flags, the verdict durability gate)
- **CI:** `lint`, `unit`, and `pg` jobs, with a `pg` job running the new testcontainers suite

### Changed

- All mail actions are direct SQL `UPDATE`s through `postimap/actions.py` — PostIMAP's own triggers propagate them to IMAP
- `postimap/listener.py` replaces the old custom `mv_message_notify` trigger — MailVerdict never installs DDL on a table it doesn't own
- Folder-changing actions (`api/mails.py`, `rules/executor.py`, `api/mcp_tools.py`) use the contract's `folder_id + imap_uid = NULL` move instead of a random negative `imap_uid` sentinel
- `verdicts` gained `account_id` and `message_id_hdr`; `create_verdict` now takes `account_id` and drops the removed `neighbor_ids` field
- Search config switched from `'english'` to `'simple'` to match `search_vector`'s own tsvector config
- `account_prefs.folder_mapping` dropped — `folders.special_use`, overridable per-folder via `folder_prefs.special_use_override`, is now the single source of truth
- `folder_prefs.subscribed` dropped (unused concept)
- Account update moved from `PUT` to `PATCH /api/accounts/:id`
- `GET /api/search` drops the per-result `score`/`source` fields and the response's `mode` field (fulltext is the only mode); results carry a `snippet` instead

### Fixed

- Account deletion (`DELETE /api/accounts/:id`) is a real, working `DELETE FROM accounts` — PostIMAP 1.0.1 added the grant, so no interim "disable instead" workaround was needed
- SQLAlchemy's default `Enum` column type persists a Python enum member's `.name`, not its `.value` — a raw-SQL predicate written against the value (like the verdict durability index's `source = 'ai'`) silently never matched. All three enum columns (`VerdictSource`, `TagSource`, `ImageExceptionType`) now explicitly persist `.value`
- Alembic's `env.py` called `asyncio.run()` internally, which fails when migrations are triggered from code already inside a running event loop (the pg-layer test fixtures); the pg-layer harness now offloads the migration call to a worker thread
- `postimap_info`'s actual primary key is `singleton` (a `BOOLEAN CHECK`), not `id` — the mapped model was wrong
- `outbox.next_retry_at` is `NOT NULL` with a server-side default in the real schema; the mapped column had neither, so SQLAlchemy sent an explicit `NULL` on every insert and every send/draft failed with a constraint violation. Caught by a pg-layer test seeding a real outbox row, not by unit tests against a mocked session
- The SSE endpoint answered 404, so every live update was dead. Starlette matches routes in registration order and a `Mount` claims every path beneath it, so registering `/api/events` after mounting `/api` left it unreachable and the interface sat on "Reconnecting..." indefinitely. A test now asserts the ordering
- Postgres 18 requires its volume mounted at `/var/lib/postgresql`, not the pre-18 `/var/lib/postgresql/data`; with the old path it refuses to start and reports the mount as unused, so neither the development nor the production stack came up
- The development app container never started: podman-compose places services in a pod and rejects `userns_mode` alongside it
- The development stack could not sync at all — its throwaway mail server presents a self-signed certificate, which PostIMAP correctly rejects by default. Certificate validation is disabled for development only; production validates
- **The `/mcp` endpoint had no authentication.** The API key is enforced per-router inside the mounted API application, and a mount is an ASGI boundary, so it never ran for the separately mounted MCP application — which exposes reading, searching, moving and sending mail. Now wrapped in ASGI middleware that checks the same key
- Editing an account failed with a permission error on any deployment using the restricted database role: the update path sent `imap_host`, `imap_port` and `imap_user`, which the contract permits only on insert. They are no longer updatable, and the interface says so — changing an IMAP host means removing the account and adding it again
- Readiness latched false permanently when PostIMAP had not yet created its contract row, so a pod installed alongside PostIMAP never became Ready. Readiness now re-checks until confirmed
- The event listener never reconnected once its connection closed. The keepalive skipped its probe on a closed connection instead of treating it as the reason to reconnect, so live updates, classification and rules all stopped silently until the process was restarted. Reconnection now retries with backoff
- Outbox status notifications could never fire: the event carried only the changed column names, never the status itself. The event now carries the row's current status and kind
- `mail.updated` refreshed nothing, because the event names the row `id` and the client read `message_id`. Open messages and conversations now refresh, and the conversation view is invalidated alongside the message
- `folder_prefs.special_use_override` was applied when listing folders but ignored when resolving trash, archive and junk, so on servers that do not advertise SPECIAL-USE the folder tree looked correct while every action failed. `account_prefs.spam_enabled` was documented as a gate but never read, leaving the per-account toggle inert
- The keyword-based analyst existed but nothing could select it, so spam classification required a live API key. The `ai` settings category now takes a `provider` of `anthropic` or `fake`
- An open server-sent-events stream blocked graceful shutdown indefinitely, so a process with a browser attached never exited on its own. Shutdown is now bounded
- `pytest -m pg` and `-m e2e` selected nothing: a `pytestmark` in a directory's own conftest does not apply to sibling modules, so the marker the docstrings described was never actually attached. Tests are now marked by the directory they live in

### Removed

- `sync/` directory (14 files, ~4,700 LOC) — replaced by PostIMAP microservice
- `jobs/` directory (3 files) — PostIMAP manages account state
- `sync_utils.py`, `folder_utils.py` — no longer needed
- `SyncQueue`, `SyncAudit`, `JobState` models
- `imap-tools` dependency
- Direct IMAP connection management (SyncEngine, SyncManager, SyncConnector)
- IDLE watchers (`sync/idle.py`), sync trackers (`sync/tracker.py`)
- OutboundProcessor (`sync/outbound.py`), ActionPropagator (`sync/actions.py`)
- `jobs.py` API endpoint (job state managed by PostIMAP)
- `semantic/` package, Qdrant service (compose + config), `core/openai_provider.py`
- `core/encryption.py`, `core/jsonb.py` (PostIMAP's jsonb columns are native jsonb now, no double-encoding to work around)
- `database/pg_listener.py` (replaced by `postimap/listener.py`)
- `api/selection.py` and its schemas (server-side per-account selection state; client-held selection + a bulk-action endpoint replaces it), `api/jmap.py` (placeholder)
- `devenv/` CLI, `docker/stalwart-*.toml`, `compose.test.yaml`, the Stalwart-based `tests/e2e/` suite and its container manager — replaced by the testcontainers-based `tests/setup/` and `tests/pg/`
- Committed E2E screenshots and result artifacts
- `POST /api/accounts/:id/test-connection` — there are no IMAP imports in this codebase; `state`/`state_error` on the account row is the connectivity truth surface
- `GET`/`PUT /api/accounts/:id/folder-mapping` and `POST .../folder-mapping/auto-detect` — `folders.special_use` plus the per-folder override was already the single source of truth these read from
- `PATCH .../folders/:id/visibility` and `PUT .../folders/:id/unified-name` — folded into `PATCH /api/folders/:id/prefs`

## [0.2.0] - 2026-03-20

### Added

- IMAP sync engine with three-tier strategy (QRESYNC, CONDSTORE, full diff) and IDLE watcher
- LLM-powered spam verdict pipeline with configurable system prompt and feedback loop
- Event-driven rule engine with 14 condition types and 11 action types
- Semantic search over email history via Qdrant + OpenAI embeddings
- REST API + SSE + MCP tool interface
- DB-managed settings system (AI, spam, sync, retry, rules) with REST API
- Account management with Fernet-encrypted IMAP/SMTP credentials
- Per-account folder mapping with SPECIAL-USE auto-detection
- IMAP spam flagging via folder move + $Junk/$NotJunk keywords
- Background job system with per-account state machine
- SvelteKit 5 web UI with dark theme (dashboard, mail, accounts, settings, verdicts, search)
- E2E test suite (27 tests) with Stalwart mail server, Postgres, Qdrant
- Unit test suite (319 tests)
- OpenAI API key configurable via Settings API (not env var)
- YAML-based infrastructure config with env var overrides
- Alembic database migrations for PostgreSQL
- HTML sanitization via nh3 with remote image blocking
