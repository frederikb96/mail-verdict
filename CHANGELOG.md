# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Body snippet/preview (first 120 chars) in mail list items for both single-account and unified views
- Auto-select inbox folder on initial load and account switch

### Changed

- Mail list item layout: dynamic height with `py-3` padding (was fixed `h-16`)
- Typography hierarchy: sender = `text-foreground font-medium/semibold`, subject + snippet = `text-muted-foreground`
- Selected mail state: left border accent (`border-l-primary`)
- Hover action buttons: improved sizing, transitions, and color feedback
- Explicit `text-foreground` on sender name span (prevents color inheritance issues)
- Content div uses `overflow-hidden` to prevent flex collapse

### Security

- SSE endpoint (`/api/events`) now validates API key (was bypassing FastAPI auth middleware)
- `EventRing.add()` protected by `asyncio.Lock` to prevent interleaved mutations
- `list_mails` endpoint requires `account_id` (no longer returns cross-account data)
- `restore_remote_images` validates URL scheme (blocks `javascript:`, `vbscript:` XSS vectors)

## [1.0.0] - 2026-03-21

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
- **E2E test suite:** 91 tests across 15 files (accounts, sync, pagination, folders, images, mail actions, search, selection, SSE, unified view, settings, health)
- **Unit test suite:** 503 tests across 36 files
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
