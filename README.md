# MailVerdict

[![CI](https://github.com/frederikb96/mail-verdict/actions/workflows/ci.yaml/badge.svg)](https://github.com/frederikb96/mail-verdict/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/frederikb96/mail-verdict)](https://github.com/frederikb96/mail-verdict/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

AI-powered email management: spam detection via LLM verdicts, smart rule engine, and vector similarity search over email history.

## Quick Start

```bash
# Development (build from source, hot-reload)
cp .dev.env.example .dev.env        # Fill in secrets
podman compose --env-file .dev.env -f compose.dev.yaml up -d

# Production (pre-built image, persistent volumes)
cp .prod.env.example .prod.env      # Fill in secrets
podman compose --env-file .prod.env up -d
```

## Configuration

All config with defaults and comments lives in `config/config.yaml` -- this is the single source of truth.

**Loading chain** (highest priority wins):
- `MAIL_VERDICT_<SECTION>_<KEY>` environment variables
- Custom override YAML in `config-custom/` (sparse, only changed values)
- `config/config.yaml` (always present, complete)

**Secrets** required as environment variables:

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for AI verdict and embeddings |
| `MAIL_VERDICT_ENCRYPTION_KEY` | AES-256-GCM key for IMAP/SMTP credential encryption (used by PostIMAP) |
| `POSTGRES_PASSWORD` | Database password (used by compose `${VAR}` interpolation) |

Compose injects secrets via `--env-file` flag -- see `.dev.env.example` / `.prod.env.example` for the structure.

## Compose Files

| File | Purpose | App port | Data |
|------|---------|----------|------|
| `compose.yaml` | Production | 8080 | Named volumes |
| `compose.dev.yaml` | Development | 18081 | `/tmp/mv-dev-*` |
| `compose.test.yaml` | Test infrastructure | 18080 | `/tmp/mv-test-*` |

Dev and prod can run simultaneously on different ports. Test compose is for E2E test infrastructure (see CLAUDE.md for test details).

## Tech Stack

- **Python 3.13+**, FastAPI, SQLAlchemy 2.0 async, Alembic, asyncpg
- **PostIMAP** (external TypeScript microservice — IMAP↔PostgreSQL sync, v0.2.0)
- **Qdrant** + OpenAI embeddings for semantic search
- **React 19 + Next.js 16** (static export served by FastAPI)
- **aiosmtplib** (SMTP), **FastMCP** (MCP tool interface)

## License

[MIT](LICENSE) -- Frederik Berg
