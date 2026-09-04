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
- **Compose** — send, reply, reply-all, forward and drafts, with attachments. Replies thread
  correctly. Reopening a draft continues editing it in place, and sending one leaves no draft
  behind. Any message can be downloaded as a raw `.eml` file.
- **Actions** — read/unread, flag, archive, trash, permanent delete (with confirmation, since it
  is irreversible), keywords, drag-and-drop moves, and bulk actions over a selection or a whole
  folder.
- **Notifications** — a durable, acknowledgeable record of any write that never reached the mail
  server, including a send that never left, surfaced with the reason and a live update the moment
  it happens.
- **Spam verdicts** — each new message is classified by a language model, with the reasoning
  visible and a correction loop when it gets one wrong. Historical mail is never classified, and
  nothing is classified twice.
- **Rules** — conditions over incoming mail with actions that move, tag, flag or delete it.
- **Search** — full text across subject, sender and body, plus semantic search over an embedding
  of every message for finding mail by meaning rather than exact words.
- **Calendars and contacts** — CalDAV and CardDAV servers mirrored the same way mail is: calendars
  with recurring events, RSVPs and per-occurrence editing, and address books with compose
  autocomplete. An emailed invitation is parsed and offered for import on its own, and a reply
  goes back over the identity's own outbox rather than the server's scheduling engine.
- **Privacy** — remote images are blocked by default, with a per-sender and per-domain allowlist.
  A sender's avatar is initials only, never a lookup against a third party. Message HTML is
  sanitized on the server and rendered in an isolated shadow root, on a light canvas by default —
  mail is written assuming one, and cannot be recoloured reliably — with dark rendering available
  per message, either through a toggle in its header or automatically for a message that declares
  its own dark-canvas support.
- **MCP server** — connect an MCP client and let it search, read, organise and send mail, and
  read, create, edit and delete calendar events and contacts.

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
then MailVerdict — and includes a CloudNativePG example and the access setup.

## Configuration

Every option, with its default and an explanation, lives in
[`config/config.yaml`](config/config.yaml). That file is the documentation; this README does not
repeat it.

Configuration comes from that file, then a sparse override file, then environment variables — and
the application refuses to start on anything missing rather than quietly substituting a fallback.
Anything that changes at runtime — the AI provider and model, spam behaviour, rules, and provider
API keys — is a **setting**, stored in the database (keys encrypted) and edited through the API,
not in a file.

A handful of values stay environment variables regardless, because they either gate config loading
itself or are the fallback path for a deployment that would rather not put a key in the database:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Fallback provider keys, used only when nothing is stored via the Settings API |
| `MAIL_VERDICT_DATABASE_URL` | PostgreSQL connection, shared with PostIMAP |
| `POSTGRES_PASSWORD` | Database password, used by compose |
| `ENCRYPTION_KEY` | Encrypts provider keys stored via the Settings API, and PostIMAP's own credential-at-rest encryption — one key shared by both. Optional; without it, provider keys can only come from the two env vars above |

## Access

MailVerdict has no login screen and no auth mechanism of its own — nothing checks a header or a
key on any endpoint. Put an authenticating proxy in front of it (OIDC, basic auth, an internal
SSO) and let that handle sign-in — people never touch application credentials, and the application
stays free of session management. The chart README has a worked example.

## API

MailVerdict is built to be driven by an agent as much as by the browser UI. [docs/api.md](docs/api.md)
is the reference: worked examples from an empty instance to a configured one, the REST endpoint
groups, and the two things about the API that are easy to assume wrongly — that there is no
authentication at all, and that a pipeline write behaves differently from every other write when
it names something that does not exist yet. The generated OpenAPI document at `/api/openapi.json`
is the exhaustive schema; the MCP server at `/mcp` wraps a curated subset of the same
functionality as typed tools.

## Architecture

[docs/architecture.md](docs/architecture.md) covers how MailVerdict and PostIMAP divide the work,
how live updates reach the browser, and the two design decisions that are not obvious: why
MailVerdict's tables carry no foreign keys onto PostIMAP's, and how a message is guaranteed never
to be classified twice.

Built with FastAPI, SQLAlchemy and Alembic on the server, React and Next.js on the client, and
FastMCP for the tool interface.

## Known limitations, deliberately

- **Folders can be created and deleted, never renamed or re-nested.** IMAP's rename operation
  renames every child folder along with it, so it can't be mirrored as a single-row update; faking
  it by creating a new folder, moving the mail and deleting the old one would lose flags and
  dates. Deleting a folder destroys every message in it on the server with no undo, so it always
  asks for confirmation naming the message count.
- No multi-user isolation, no offline mode, no PGP.

## License

[MIT](LICENSE) — Frederik Berg
