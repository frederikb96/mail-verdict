# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Breaking Changes

- **PostIMAP integration:** Entire IMAP sync layer (~4,700 LOC) replaced with the PostIMAP microservice — MailVerdict is now a pure PostgreSQL application, targeting PostIMAP's versioned contract (`contract_version` 1, service `1.0.0`)
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
- **Threading:** `messages.thread_id` is now mirrored and indexed, ready for conversation grouping
- **`is_truncated` surfaced:** oversized messages (over `storage.max_message_bytes`) are now distinguishable in the API
- **Verdict durability gate:** partial unique index on `(account_id, message_id_hdr) WHERE source = 'ai'` — an AI verdict is never reissued for the same message header, surviving both retention purge and a UIDVALIDITY resync
- **`FakeSpamAnalyst`:** deterministic, keyword-driven `SpamAnalyst` implementation for tests and API-key-free local development
- **`tests/setup/`:** testcontainers-based test infrastructure — container runtime bootstrap (rootless podman or a standard Docker/DinD socket, fails loudly with the fix command otherwise), pinned image tags, session-scoped Postgres + PostIMAP fixtures
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

### Fixed

- Account deletion (`DELETE /api/accounts/:id`) is a real, working `DELETE FROM accounts` — PostIMAP 1.0.1 added the grant, so no interim "disable instead" workaround was needed
- SQLAlchemy's default `Enum` column type persists a Python enum member's `.name`, not its `.value` — a raw-SQL predicate written against the value (like the verdict durability index's `source = 'ai'`) silently never matched. All three enum columns (`VerdictSource`, `TagSource`, `ImageExceptionType`) now explicitly persist `.value`
- Alembic's `env.py` called `asyncio.run()` internally, which fails when migrations are triggered from code already inside a running event loop (the pg-layer test fixtures); the pg-layer harness now offloads the migration call to a worker thread
- `postimap_info`'s actual primary key is `singleton` (a `BOOLEAN CHECK`), not `id` — the mapped model was wrong

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

## [1.0.0] - 2026-03-22

### Breaking Changes

- **IMAP library migration:** aioimaplib replaced with imap-tools (all IMAP operations now run in `asyncio.to_thread`)
- **Complete UI rewrite:** SvelteKit frontend replaced with React + Next.js
- **SSE event types renamed:** `new_mail` -> `mail.new`, `folder_change`/`flags_changed` -> `mail.updated`, `verdict_issued` -> `verdict.issued`
- **SSE redesign:** Queue-per-client fan-out replaced with centralized EventRing + waiter pattern
- **Pagination:** Offset-based pagination replaced with cursor-based (`before` parameter, `has_more`, `next_cursor`)
- **Mail list API:** Returns `MailListResponse` wrapper (replaces bare list)
- **HTML sanitization:** Moved from read-time to store-time (nh3 applied during sync)
- **OPENAI_API_KEY:** Now environment variable only (removed from DB settings)

### Added

- **Two-phase sync:** Headers fetched first (fast display), bodies fetched in background
- **SyncTracker:** Per-account in-memory sync progress with phase, folder info, derived fields
- **EventRing:** In-memory ring buffer (500 events/account) with monotonic IDs and Last-Event-ID replay
- **Cursor-based pagination:** Composite index `(folder_id, received_at DESC)`, `before` cursor
- **On-demand body fetch:** GET /mails/:id triggers IMAP fetch when `body_synced=False`
- **Account state machine:** CREATED -> SYNCING -> SEEDING -> ACTIVE (+ ERROR with retry)
- **Image privacy:**
  - `ImageException` model + CRUD API for per-account sender/domain image allowlist
  - Read-time remote image stripping (separate from store-time nh3 XSS sanitizer)
  - `has_blocked_images` and `images_allowed` fields on MailDetail schema
  - Per-message, per-sender, per-domain image loading control
- **Folder management:**
  - Folder ordering, visibility toggle, IDLE per-folder config with validation
  - `folder_order` (JSONB) and `idle_folders` (JSONB) on Account model
  - `is_visible` on Folder model
  - SPECIAL-USE dedup with RFC 6154 flag priority and name fallback
- **Mail interactions:**
  - Per-mail hover actions (star, archive, spam, delete)
  - Backend SelectionManager: in-memory per-account selection state (virtual scroll compatible)
  - Selection API: toggle, range (shift-click), select-all, clear under `/accounts/:id/selection/`
  - Bulk actions API: move, archive, star, unstar, spam, mark_read, mark_unread, delete
  - SSE `selection.changed` events for real-time selection state sync
  - Extended mail action endpoint with `archive` and `spam` action types
- **Unified view:**
  - Cross-account folder merging with emoji icons
  - `unified_name` on Folder model, `emoji` on Account model (migration 006)
  - Unified API: GET /unified/folders, GET /unified/mails (cross-account paginated)
  - Account emoji API, folder unified name API, unified folder order API
- **Complete React + Next.js UI:**
  - Three-pane layout: collapsible sidebar, mail list, reading pane
  - shadcn/ui + base-ui component library with dark theme
  - TanStack Query for data fetching with localStorage persistence
  - Virtual scrolling via virtua VList for 100k+ mail performance
  - Jotai state atoms for SSE-driven state
  - SSE client hook with reconnect and cache invalidation
  - Email HTML rendering via Shadow DOM with DOMPurify sanitization
  - Folder assignment UI with auto-detect
  - Account management page with sync progress from SSE
  - Settings editor with category tabs and theme toggle
  - Semantic + fulltext search with mode toggle
  - Multi-select UI: checkbox on mail rows, shift/ctrl-click, bulk action toolbar
  - Drag-and-drop: mail(s) to sidebar folders via dnd-kit
  - Skeleton loading states and empty states for all views
  - Android-ready architecture: hooks/logic separated from UI components
- **UX polish:**
  - SSE connection state indicator: colored dot (green/yellow/red) with tooltip
  - Keyboard shortcuts: j/k navigate, Enter/Escape open/close, x toggle selection, e/s/#/!/r/u actions
  - Responsive layout: mobile viewport (< 768px) shows list or pane with back button
  - Error boundaries for sidebar and content areas
  - Folder order via dnd-kit drag-and-drop sortable
  - Focused mail tracking via Jotai atom with visual ring indicator
- **Jinja2 prompt templates:** All 4 LLM prompts as external files in `config/prompts/`
- **Prompt loader utility** (`core/prompts.py`) with multi-path Jinja2 environment
- **Real-time sync progress:** Preflight message counts, per-folder progress, batched fetch (50/batch)
- **Dynamic account sync:** Trigger/cancel per account without app restart
- **Sync concurrency safety:** asyncio.Lock serializes IDLE, poll, manual triggers
- **CI:** Parallel UI job with Node.js 22 type check and build validation
- **Mail action IMAP propagation:** Star/archive/spam/delete sync back to IMAP server via ActionPropagator
- **IMAP sync resilience:** Auto-reconnect with exponential backoff, IMAP-wins conflict resolution, atomic batch actions with per-UID tracking
- **Mobile layout:** Hamburger menu opens sidebar as Sheet overlay (<768px), independent scroll for mail list and reading pane
- **Desktop sidebar collapse:** Toggle to icon-only mode (Ctrl+B shortcut), cookie-persisted state
- **Per-account settings on Accounts page:** Folder assignment, ordering, IDLE config, unified names, emoji picker, sync enable/disable toggle
- **SSE event emission:** mail.updated/mail.deleted events after every action, folder count invalidation
- **Body snippet/preview:** First 120 chars in mail list items for both single-account and unified views
- **E2E test suite:** 127 tests across 20 files (accounts, sync, pagination, folders, images, mail actions, search, selection, SSE, unified view, settings, health, sync recovery, account deletion cascade)
- **Unit test suite:** 536 tests across 37 files
- **UI test flows:** 33 documented browser automation flows with 25 playwright-local screenshots
- **Sync stability E2E test:** Verifies actions don't cause sync thrashing
- **Reasoning effort setting:** minimal/low/medium/high for OpenAI reasoning models
- **Embedding field locking:** embedding_model and dimensions locked after first use
- **Alembic migrations:** 004 (two-phase sync), 005 (image exceptions, folder management), 006 (unified view)

### Changed

- IMAP connector uses imap-tools `MailBox`, `move()`, `flag()`, `copy()`
- Folder discovery uses `mailbox.folder.list()` with SPECIAL-USE dedup
- IDLE watcher uses `mailbox.idle.wait()` in thread
- SyncManager uses SyncTracker for progress (replaces `push_sync_status`)
- SSE `sync.state` snapshot on fresh connect with full tracker state
- SSE keepalive interval reduced from 30s to 15s
- Folder message counts: `unread_count` and `total_count` in folder API response
- Header sync sets `headers_synced=True`, body sync sets `body_synced=True`
- OpenAI client uses lazy provider pattern (API key changes take effect without restart)
- Batched message fetch (chunks of 50) for progress reporting and clean cancellation
- Static file serving updated for Next.js export format
- CI workflow split into parallel `python` and `ui` jobs
- Settings page: global-only (per-account settings moved to Accounts page)
- Navigation: deterministic account/inbox auto-selection via useEffect
- Folder click from search/settings navigates to mail view
- Search filters by selected account (or all in unified view)
- Settings page independent of sidebar navigation state
- Mail list panel: fixed 400px width (replaces broken ResizablePanel)
- Typography hierarchy: sender=foreground, subject+snippet=muted-foreground
- SPA navigation: serve RSC flight payloads for client-side transitions (no full page reload)
- Provider field: static "OpenAI" label (not editable)
- API key: dynamically settable via UI (password field)

### Fixed

- **Sync thrashing:** Actions (flag, delete, move, archive, spam) no longer cause infinite sync loops
- **Deleted mail UID leak:** Soft-deleted mails filtered from UID diff
- **Flag comparison:** Only report actual flag changes (was reporting all UIDs every cycle)
- **Move phantom records:** Moved mails marked is_deleted instead of updating folder_id
- **HIGHESTMODSEQ fallback:** Graceful fallback when server doesn't support CONDSTORE
- **Connection error propagation:** Sync manager re-raises connection errors for proper backoff
- **Theme FOUC:** Blocking script sets dark class before first paint
- **Account switching jank:** keepPreviousData prevents loading skeleton flashes
- **Settings/accounts scroll:** overflow-y-auto on content wrapper

### Removed

- `sync/extensions.py` module (imap-tools handles SELECT, CONDSTORE, SPECIAL-USE natively)
- aioimaplib dependency
- Offset-based pagination (`offset` parameter)
- Hardcoded prompt strings from `enrichment.py`
- Old `config/prompts/spam_analyst.md` (replaced by Jinja2 templates)
- Static OpenAI client initialization from server lifespan
- Sync lookback_days and auto_detect_folders from global settings (per-account only)
- Legacy SvelteKit `_app` static file mount
- SvelteKit frontend (entire `ui/` rewritten)

### Security

- Store-time HTML sanitization via nh3 (defense-in-depth)
- Read-time remote image blocking with per-sender/domain allowlist
- Sync trigger rate limiting: 5s debounce per account (429 response)
- SSE cross-account event isolation
- SSE endpoint (`/api/events`) validates API key (was bypassing auth middleware)
- `EventRing.add()` protected by `asyncio.Lock`
- `list_mails` requires `account_id` (no cross-account data leaks)
- `restore_remote_images` validates URL scheme (blocks XSS vectors)

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
