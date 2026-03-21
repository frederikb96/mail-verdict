# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Complete React + Next.js UI rewrite (replaces SvelteKit)
  - Three-pane layout: collapsible sidebar, mail list, reading pane
  - shadcn/ui (base-ui) component library with dark theme
  - TanStack Query for data fetching with cursor-based pagination
  - Virtual scrolling via virtua (VList) for 100k+ mail performance
  - Jotai state atoms for SSE-driven sync state
  - SSE client hook with reconnect and cache invalidation
  - Email HTML rendering via Shadow DOM with DOMPurify sanitization
  - Remote image blocking with privacy banner
  - Per-message image/HTML loading control (Block 17): allow images per-message, per-sender, or per-domain
  - Image exceptions management in settings (view/delete sender and domain allowlist entries)
  - Folder assignment UI: map IMAP folders to inbox/spam/drafts/sent/archive/trash roles with auto-detect
  - Folder ordering and visibility: reorder folders, show/hide per folder, sidebar respects custom order
  - IMAP IDLE per-folder configuration with immediate validation
  - Flat folder hierarchy: all folders at same indent level, dot-separated names
  - Account management page with sync progress from SSE
  - Settings editor with category tabs and theme toggle
  - Semantic + fulltext search page with mode toggle
  - Skeleton loading states and empty states for all views
  - Android-ready architecture: hooks/logic separated from UI components
- Backend: updated static file serving for Next.js export format
- `ImageException` model + CRUD API for per-account sender/domain image allowlist
- `image_sanitizer.py`: read-time remote image stripping (separate from store-time nh3 XSS sanitizer)
- `MailDetail` schema: `has_blocked_images` and `images_allowed` fields
- `folder_management.py` API router: folder ordering, visibility toggle, IDLE config/validation
- `folder_order` (JSONB) and `idle_folders` (JSONB) on Account model
- `is_visible` (boolean) on Folder model
- Alembic migration 005: image_exceptions table, folder management columns
- SSE endpoint accepts `last_event_id` query parameter for manual reconnect replay

### Changed

- `SyncTracker` (`sync/tracker.py`): per-account in-memory sync progress with phase, folder info, derived fields
- `EventRing` (`api/event_ring.py`): in-memory ring buffer (500 events/account) with monotonic IDs and Last-Event-ID replay
- SSE Last-Event-ID reconnect support: replays missed events from EventRing, falls back to state snapshot
- SSE `sync.state` snapshot on fresh connect with full tracker state
- Keepalive interval reduced from 30s to 15s for faster disconnect detection
- `SyncEngine.get_tracker(account_id)` to retrieve per-account tracker
- Alembic migration 004: `headers_synced` + `body_synced` columns on Mail table
- Cursor-based pagination for mail list API (`before` parameter, `has_more`, `next_cursor`)
- Composite index `(folder_id, received_at DESC)` for efficient cursor queries
- Folder message counts: `unread_count` and `total_count` in folder API response
- On-demand body fetch: GET /mails/:id triggers IMAP fetch when `body_synced=False`
- `SyncManager.fetch_body_for_mail()` for single-message body retrieval
- `SyncEngine.get_account_sync_by_id()` for account lookup by UUID

### Changed

- SSE redesign: replaced queue-per-client fan-out with centralized EventRing + waiter pattern
- SSE event types renamed: `new_mail` -> `mail.new`, `folder_change` -> `mail.updated`, `flags_changed` -> `mail.updated`, `verdict_issued` -> `verdict.issued`
- SyncManager uses SyncTracker for progress (replaces `push_sync_status` calls)
- IMAP library migration: replaced aioimaplib with imap-tools (fixes RecursionError on large mailboxes)
- All IMAP operations wrapped in `asyncio.to_thread()` (imap-tools is synchronous)
- Two-phase sync: headers fetched first (fast display), bodies fetched separately
- Folder discovery uses `mailbox.folder.list()` with SPECIAL-USE dedup
- IDLE watcher uses `mailbox.idle.wait()` in thread
- Action propagator uses imap-tools `move()`, `flag()`, `copy()`
- Test connection endpoint uses imap-tools MailBox
- Mail list API returns `MailListResponse` wrapper (replaces bare list)
- Header sync sets `headers_synced=True`, body sync sets `body_synced=True`

### Removed

- `sync/extensions.py` module (imap-tools handles SELECT, CONDSTORE, SPECIAL-USE natively)
- aioimaplib dependency
- Offset-based pagination (`offset` parameter removed from mail list API)

## [0.2.2] - 2026-03-21

### Added

- Jinja2 prompt templates: all 4 LLM prompts now external files in `config/prompts/`
  - `spam_system.md.j2` — spam classification rules and output format
  - `spam_user.md.j2` — email context injection template
  - `enrichment_system.md.j2` — tag classification rules (with `{{ tag_list }}`)
  - `enrichment_user.md.j2` — email content for tag classification
- Prompt loader utility (`core/prompts.py`) with multi-path Jinja2 environment
- Debug-level logging of full system + user prompts sent to LLM (spam analyst + enrichment)
- `jinja2>=3.1.0` dependency

### Changed

- Prompt file resolution: multi-path search (dev source tree + container `/app/config/`)
- Prompts rewritten: system prompt explains everything, user prompt references system and provides data

### Removed

- Hardcoded prompt strings from `enrichment.py`
- Old `config/prompts/spam_analyst.md` (replaced by Jinja2 templates)

## [0.2.1] - 2026-03-21

### Added

- Real-time sync progress via SSE: preflight message counts, per-folder progress, batched fetch (50/batch), completion summary
- IMAP STATUS command support for cheap message count preflight without SELECT
- Dynamic account sync: trigger/cancel sync per account without app restart
- Sync concurrency safety via asyncio.Lock (IDLE, poll, manual triggers serialized)
- Global sync enable/disable toggle with precedence over per-account settings
- Auto-refresh sidebar folders and accounts on SSE sync_status/new_mail events
- Test email seeder (18 varied emails: spam, newsletters, normal) for manual testing
- Inline sync progress UI on accounts page: spinner, progress bar, folder counts, error display

### Changed

- OpenAI client now uses lazy provider pattern — API key changes via Settings API take effect without restart
- Batched message fetch (chunks of 50) replaces single-shot FETCH for progress reporting and clean cancellation
- Settings page: locked immutable fields (provider, embedding_model, embedding_dimensions), removed stale sync settings
- SSE push_sync_status() accepts rich progress payloads via **kwargs
- SSE route ordering fix: /api/events now correctly matched before FastAPI /api mount

### Removed

- Static OpenAI client initialization from server lifespan (replaced by provider)
- Sync lookback_days and auto_detect_folders from global settings (per-account only)

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
