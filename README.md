# MailVerdict

[![CI](https://github.com/frederikb96/mail-verdict/actions/workflows/ci.yaml/badge.svg)](https://github.com/frederikb96/mail-verdict/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/frederikb96/mail-verdict)](https://github.com/frederikb96/mail-verdict/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A self-hosted mail client with an AI layer: read and organise mail across accounts, send and
reply, and let a language model sort out what is spam while rules handle the rest.

MailVerdict does not speak IMAP. Mail transport is handled by
[PostIMAP](https://github.com/frederikb96/postimap), which mirrors your mailboxes into PostgreSQL
in both directions. MailVerdict is an ordinary Postgres-backed web application on top of that
mirror, which is what keeps it simple.

## What it does

- **Mail** — multiple accounts, folder tree with live counts, conversation threading, a virtualized
  list that stays smooth on large mailboxes, and unified views across accounts. Folders can be
  created and deleted (renaming and re-nesting are an IMAP limitation, not implemented).
- **Compose** — send, reply, reply-all and drafts, with attachments. Replies thread correctly.
  Reopening a draft continues editing it in place, and sending one leaves no draft behind.
- **Actions** — read/unread, flag, archive, trash, permanent delete, keywords, drag-and-drop moves,
  and bulk actions over a selection or a whole folder.
- **Spam verdicts** — each new message is classified by a language model, with the reasoning
  visible and a correction loop when it gets one wrong. Historical mail is never classified, and
  nothing is classified twice.
- **Rules** — conditions over incoming mail with actions that move, tag, flag or delete it.
- **Search** — full text across subject, sender and body.
- **Privacy** — remote images are blocked by default, with a per-sender and per-domain allowlist.
  Message HTML is sanitized on the server and rendered in an isolated shadow root.
- **MCP server** — connect an MCP client and let it search, read, organise and send mail.

## Running it

Both compose files bring up MailVerdict, PostIMAP and PostgreSQL together.

```bash
cp .prod.env.example .prod.env      # fill in the secrets below
podman compose --env-file .prod.env up -d
```

The UI is then on <http://localhost:8080>. Add an account through the interface; PostIMAP starts
syncing it immediately and the mail appears as it arrives.

For development — hot reload, plus a throwaway mail server and SMTP sink so there is a mailbox to
work against — see [docs/development.md](docs/development.md).

## Kubernetes

A Helm chart ships in [`charts/mail-verdict`](charts/mail-verdict) and is published on release:

```bash
helm install mail-verdict oci://ghcr.io/frederikb96/charts/mail-verdict --version 1.0.0
```

It expects an external PostgreSQL shared with PostIMAP. The
[chart README](charts/mail-verdict/README.md) walks through the three steps — database, PostIMAP,
then MailVerdict — and includes a CloudNativePG example and the authentication setup.

## Configuration

Every option, with its default and an explanation, lives in
[`config/config.yaml`](config/config.yaml). That file is the documentation; this README does not
repeat it.

Configuration comes from that file, then a sparse override file, then environment variables — and
the application refuses to start on anything missing rather than quietly substituting a fallback.
Anything that changes at runtime (the AI model, spam behaviour, rules) is a **setting**, stored in
the database and edited in the interface, not in a file.

Secrets are environment variables and are never stored in the database:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Spam classification and rule enrichment |
| `MAIL_VERDICT_API_KEY` | Guards the API and MCP endpoints. Unset disables the check — only appropriate locally |
| `MAIL_VERDICT_DATABASE_URL` | PostgreSQL connection, shared with PostIMAP |
| `POSTGRES_PASSWORD` | Database password, used by compose |
| `ENCRYPTION_KEY` | Optional. Passed to PostIMAP, which encrypts stored mail credentials at rest |

## Authentication

MailVerdict has no login screen. It checks an API key on its API and MCP endpoints, and that is
the whole mechanism.

For browser access, put an authenticating proxy in front of it and have that proxy inject the key
once a user has signed in — so people never handle it and the application stays free of session
management. The chart README has a worked example. Programmatic clients send the key themselves.

## Architecture

[docs/architecture.md](docs/architecture.md) covers how MailVerdict and PostIMAP divide the work,
how live updates reach the browser, and the two design decisions that are not obvious: why
MailVerdict's tables carry no foreign keys onto PostIMAP's, and how a message is guaranteed never
to be classified twice.

Built with FastAPI, SQLAlchemy and Alembic on the server, React and Next.js on the client, and
FastMCP for the tool interface.

## License

[MIT](LICENSE) — Frederik Berg
