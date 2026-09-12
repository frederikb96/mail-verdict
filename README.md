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
  list that stays smooth on large mailboxes, and unified views: any set of folders, across
  accounts, merged into one list with an icon of its own in the sidebar -- one folder can sit in
  several. A conversation reads newest first, and a message header's sender and recipients copy
  their addresses on a click, each alone or a whole line comma-separated. Folders can be created
  and deleted (renaming and re-nesting are an IMAP limitation, not implemented). A quick filter
  narrows the open folder or view by subject, sender or recipient, its rows carrying the same
  actions an ordinary row does, and a toggle beside it shows only unread mail. Opening a reply
  collapses the quoted original behind a "Show
  quoted text" control, and ctrl+F (or a control beside the message's other icons) searches and
  highlights matches inside the open message, which the browser's own find cannot reach. An
  image or PDF attachment opens full screen for a look at it, rendered rather than downloaded;
  the download control stays for that and for anything else the preview does not recognise.
- **Compose** — a rich-text editor with Markdown input rules, tables, checklists and pasted
  images, send, reply, reply-all, forward and drafts, with attachments. The panel resizes by
  dragging its top edge or expanding to fill the window. A reply or forward embeds the original as
  a collapsible quote, replies thread correctly, and a sending identity is chosen for you —
  whichever address the original arrived at, or the account's starred default for a fresh message
  — shown, and changeable, in one From control spanning every account that always names the
  exact address (`Name <address>`), not just the account. Reopening a draft continues editing it
  in place, and sending one leaves no draft behind. Pressing Send takes the message out of the
  composer at once and sends it exactly once -- the server refuses a repeat whatever the browser
  does -- and a failure brings the composer back as it was. A send can be undone for a few seconds
  after pressing Send, held durably on the server rather than in the browser, and Undo reopens the
  composer with everything that was written rather than only cancelling the send. Any message can
  be downloaded as a raw `.eml` file.
- **Actions** — read/unread, flag, archive, trash, permanent delete (with confirmation, since it
  is irreversible), keywords, drag-and-drop moves (a long press selects instead of dragging on a
  touch device), and bulk actions over a selection or a whole folder, threaded or not. A folder
  can be worked through from the keyboard alone: the arrow keys move the reading pane between
  messages, `e` archives, `Delete` trashes, `r` toggles read and unread, and whatever takes the
  open message out of the list opens the next one in the direction you were already going.
  Archiving a message, or filing it as spam, marks it read as it moves, and mail in Archive or
  Trash stays read even when another client put it there — a setting turns this off for Archive
  and Junk.
  An account can also be given a Trash retention and a Junk retention, each in days and set
  independently, so mail sitting in either long enough is permanently removed on its own.
- **Notifications** — a durable, acknowledgeable record of any write that never reached the mail
  server, including a send that never left, surfaced with the reason and a live update the moment
  it happens. A message still waiting on its way out long after it should have gone raises an
  alert of its own, rather than waiting silently.
- **Alerts** — new mail raises a bell with a durable, dismissable list, and — once notifications
  are turned on for a device — a system notification that reaches it even with no MailVerdict page
  open, using the browser's own Web Push. Which folders alert is a per-device setting; every
  registered device can be reviewed and removed from Settings. Requires a home-screen install on
  iOS/iPadOS, and stops the moment the browser itself is quit on a desktop — the durable list is
  what a closed tab or a declined permission falls back to. An alert resolves itself once its mail
  is read anywhere, and a setting keeps new-mail alerts out of the bell's badge so it counts only
  system notifications. Calendar reminders are not delivered as alerts yet.
- **Spam verdicts** — each new message is classified by a language model, with the reasoning
  visible and a correction loop when it gets one wrong. A dedicated review screen lists every
  message currently called spam with no ruling yet, across every account and folder including
  Junk, for confirming or correcting them singly or in bulk. Historical mail is never classified,
  and nothing is classified twice.
- **Rules** — conditions over incoming mail with actions that move, tag, flag or delete it.
- **Search** — text search scoped to whichever accounts, folders and fields (subject, from, to,
  body) you pick, ranked by whether the word itself matched rather than merely started a longer
  one, then by where the match lands and newest first within that, with a typo-tolerant fallback
  for a query the primary match misses entirely; plus semantic search over an embedding of every
  message, with a Loose/Balanced/Strict control, for finding mail by meaning rather than exact
  words. Both remember the account and folder scope you chose, independent of whichever account
  the sidebar shows. Opening a result lands on that message in its
  folder wherever it sits, however far back, rather than only reaching whatever the newest page
  happens to include.
- **Calendars and contacts** — CalDAV and CardDAV servers mirrored the same way mail is: calendars
  with recurring events, RSVPs and per-occurrence editing, and address books with compose
  autocomplete. An emailed invitation is parsed and offered for import on its own, and a reply
  goes back over the identity's own outbox rather than the server's scheduling engine. An event
  can carry any number of reminders and say whether it makes you free or busy; a calendar's own
  default reminder can override or switch off the global one. Dates and times are entered and
  shown day-first in a 24-hour clock, in an app-owned control rather than the browser's own.
- **Privacy** — remote images are blocked by default, with a per-sender and per-domain allowlist.
  A sender's avatar shows their address-book photo when one exists — an embedded photo is served
  from this application's own endpoint, never a third-party request, and a remote one follows the
  same allowlist as any other remote image — and initials otherwise; never a lookup against an
  unrelated third party. Message HTML, including a sender's own stylesheet, is sanitized on the
  server and rendered in an isolated shadow root — every escaping declaration dropped, every
  remote reference gated behind the same allowlist that governs images. Opening a message from a
  sender that is not allowlisted makes no request anywhere, and a read-receipt request is never
  answered; an allowlisted sender's images and backgrounds load, while active content
  (frames, scripts, media, forms) and anything that escapes the message's box stay blocked for
  everyone. In the dark theme a message opens dark when it declares its own dark-mode support,
  with the sender's own dark styles applied, or when its own colours read safely on dark; in
  the light theme every message opens light. A toggle in its header always overrides either
  default. Hovering a link shows where it leads. The image block does
  not extend to the message body itself: a newly arrived message's subject, sender and a
  truncated body go to the configured model provider twice, once for spam classification and once
  for the embedding that powers semantic search — a deliberate design choice, not something a
  setting turns off.
- **MCP server** — connect an MCP client and let it search, read, organise and send mail, and
  read, create, edit and delete calendar events and contacts.
- **Installable** — a browser that supports it offers to install the site, which then runs in its
  own window with its own icon. The installed application registers as a `mailto:` handler, so it
  can be the machine's mail client: a `mailto:` link opens the composer filled in from the link.
  `Ctrl+Shift+1`, `Ctrl+Shift+2` and `Ctrl+Shift+3` jump to mail, calendar and contacts.

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
API keys — is a **setting**, stored in the database (keys encrypted) and edited through the API or
the Settings page, not in a file.

A handful of values stay environment variables regardless, because they either gate config loading
itself or are the fallback path for a deployment that would rather not put a key in the database:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Fallback provider keys, used only when nothing is stored via the Settings API |
| `MAIL_VERDICT_DATABASE_URL` | PostgreSQL connection, shared with PostIMAP |
| `POSTGRES_PASSWORD` | Database password, used by compose |
| `ENCRYPTION_KEY` | Encrypts provider keys stored via the Settings API, PostIMAP's own credential-at-rest encryption, and this server's Web Push signing key (generated on first use, never provisioned) — one key shared by all three. Optional; without it, provider keys can only come from the two env vars above, and push notifications are unavailable |

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
