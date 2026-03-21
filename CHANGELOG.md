# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
