# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
