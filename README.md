<p align="center">
  <img src="docs/images/header.jpg" alt="Envelopes flying into a letter tray under a green check mark, a crumpled envelope dropping into a bin beside it" width="100%">
</p>

<table>
<tr>
<td width="100">
<img src="ui/public/icon.svg" alt="MailVerdict" width="84">
</td>
<td>
<h1>MailVerdict</h1>
A self-hosted mail client with an AI layer.
</td>
</tr>
</table>

[![CI](https://github.com/frederikberg/mail-verdict/actions/workflows/ci.yaml/badge.svg)](https://github.com/frederikberg/mail-verdict/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/frederikberg/mail-verdict)](https://github.com/frederikberg/mail-verdict/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Mail, calendars and contacts from any IMAP, CalDAV and CardDAV server, in one web app you run
yourself. A language model decides what is spam and explains why, bundles the mail about each
order or booking into one entry, and writes rules from a sentence. Plain rules handle the rest.

<p align="center">
  <img src="docs/images/mail.png" alt="The mail view: folders on the left, the inbox in the middle, an open conversation on the right" width="100%">
</p>

## Why

A mail client is mostly the same everywhere. MailVerdict adds what the usual ones don't:

- **Spam verdicts with reasons.** Every new message gets a verdict from a language model, with the
  reasoning shown next to it. A review screen collects every open verdict across all accounts,
  and a wrong one is corrected right from the message.
- **Orders and tickets.** Confirmation, invoice, shipping notice, delivery, return: the mail about
  one purchase or booking becomes one entry with a status and a summary. Attached PDFs, Wallet
  passes and QR codes are read, so a train ticket's code opens full screen, ready to scan.
- **Rules from a sentence.** "These should go to Newsletter too" on an open mail proposes the rule
  change, shows what it would have caught in the last 100 mails, and waits for Accept. Rules can
  also post a message to a webhook.
- **Search by meaning.** Besides ordinary text search, every message has an embedding, so "the mail
  about the broken heater" finds it without the exact words.
- **Glacier storage.** Old mail can leave the mail server for good and live on in MailVerdict's own
  database, still searchable, readable and answerable, and movable back with its flags and date.
- **Mail that behaves.** Undo send held on the server, every action sent exactly once even across a
  flaky connection and a reload, Ctrl+Z for the last few actions, and keyboard shortcuts for the
  everyday ones.
- **Built to be driven by an agent.** A REST API with worked examples, and an MCP server that
  searches, reads, files and sends mail and edits calendars and contacts.

Every feature, area by area: [docs/features.md](docs/features.md).

<p align="center">
  <img src="docs/images/calendar.png" alt="The calendar's week view with the month and calendar list on the left" width="100%">
</p>

## How it fits together

```
browser, iPhone app, MCP client
              │
              ▼
         MailVerdict  →  PostgreSQL  ←  PostIMAP  →  IMAP, SMTP, CalDAV, CardDAV
```

MailVerdict does not speak IMAP. [PostIMAP](https://github.com/frederikberg/postimap) mirrors your
mailboxes, calendars and address books into PostgreSQL in both directions, and MailVerdict is an
ordinary Postgres-backed web app on top of that mirror. Marking a message read is an `UPDATE`;
PostIMAP carries it to the mail server. [docs/architecture.md](docs/architecture.md) explains the
split and everything built on it.

## What you need

- A container runtime (Podman or Docker with compose), or a Kubernetes cluster.
- One or more mail accounts reachable over IMAP and SMTP. Calendars and contacts need a CalDAV or
  CardDAV server, for example Nextcloud.
- An API key for at least one model provider: OpenAI, Anthropic, or any OpenAI-compatible server.
  Without one, mail works and the AI features skip their work until a key is set.
- An OpenID Connect identity provider whose tokens MailVerdict accepts, and an authenticating
  proxy in front of the web UI that signs a browser in ([Access](#access)).

## Run your own

With compose, which brings up MailVerdict, PostIMAP and PostgreSQL together:

```bash
cp .prod.env.example .prod.env      # fill in the secrets, see Configuration
podman compose --env-file .prod.env up -d
```

The UI is then on <http://localhost:8080>, behind the authenticating proxy that signs a browser in
and forwards its token ([Access](#access)). Add an account under *Settings → Accounts*; PostIMAP starts
syncing it right away and mail appears as it arrives. An administrator then sets a provider key
and picks the models under *Admin → Providers & keys* and *Admin → Models*.

### Gmail and iCloud

Both are ordinary IMAP and SMTP accounts that sign in with an app password, and *Add account* has a
preset for each: pick the provider, enter the address and the app password, and the servers are
filled in.

- **Gmail.** A personal Google Account needs 2-Step Verification on before it offers app passwords
  ([how Google's work](https://support.google.com/accounts/answer/185833)); the 16-digit password
  signs in to IMAP and SMTP. Google allows 500 sent messages a day per personal account across
  all its clients and caps how much IMAP may download per day (its Workspace documentation names
  2,500 MB), so a large mailbox can take several days to finish its first sync.
- **iCloud Mail.** The Apple Account needs two-factor authentication, then an app-specific password
  ([how Apple's work](https://support.apple.com/en-us/102654)). IMAP signs in with the part of the
  address before the @, SMTP with the full address. Changing the Apple Account password revokes
  every app-specific password, after which the account shows an error until a new one is entered.
- **Google Workspace (work and school accounts) is not supported.** Google allows only OAuth for
  them, and PostIMAP, which does the IMAP and SMTP, signs in with passwords only.

Gmail shows one message in every label that carries it, in Starred and in All Mail, each as a
folder. So that mail is not classified and embedded once per copy, a Gmail account's folders are set
up on first sync: All Mail becomes the Archive, and label folders, Starred and Important have the
AI switched off. The switch is the sparkle beside each folder under *Settings → Accounts → Folder
Order & Visibility*; turn it on for a label whose mail should be classified. The inbox, Sent, Drafts,
Trash and Spam work as on any other server. A message that sits in the inbox and in All Mail is
processed through its inbox copy.

On Kubernetes, a Helm chart is published with every release:

```bash
helm install mail-verdict oci://ghcr.io/frederikberg/charts/mail-verdict --version <version>
```

It expects an external PostgreSQL shared with PostIMAP. The
[chart README](charts/mail-verdict/README.md) walks through the three steps — database, PostIMAP,
then MailVerdict — with a CloudNativePG example and the access setup.

For development — hot reload, plus a throwaway mail server and SMTP sink — see
[docs/development.md](docs/development.md).

## Configuration

Every option, with its default and an explanation, lives in
[`config/config.yaml`](config/config.yaml). That file is the documentation; this README does not
repeat it. Configuration comes from that file, then a sparse override file, then environment
variables, and the app refuses to start on anything missing rather than quietly substituting a
fallback.

Anything that changes at runtime is a **setting**, stored in the database and edited in the web
UI or through the API. Each setting is the instance's or a user's own: the machinery (the AI
providers and models, provider API keys stored encrypted, queues, secrets, webhooks) belongs to
admins and sits under *Admin*, which only administrators see; each user has their own mail,
calendar, undo-send and orders preferences under *Settings*, and an admin's own preferences are
theirs alone.

- **One provider and model per AI task:** spam verdicts (`ai`), embeddings (`semantic`), the
  order-matching, order-text and image-reading calls (`orders`' `decide_*`, `write_*` and `read_*`
  keys) and the rule assistant (`assistant`). A provider is OpenAI, Anthropic (not for embeddings,
  it has no embedding model), or any OpenAI-compatible server by its own address, key and model
  name; a custom server's address is set on the task that uses it (`ai.base_url`,
  `semantic.base_url`, `orders.decide_base_url`, ...).
- A key is stored once per provider and shared by every task that names it, so mail that must stay
  private can sit on one provider while order analysis uses another. The Models page states
  beside each task which mail it reads; [docs/features.md](docs/features.md#privacy) has the
  details.
- A provider that runs out of credit, or rejects its key, pauses its tasks until it answers again,
  and raises an alert if that lasts (`providers.suspended_alert_after_seconds`).
- **Token usage:** the tokens of every paid model response are recorded locally with their
  purpose, provider and model, and pushed to an Elasticsearch data stream once
  `usage_export.elasticsearch_url` is set and an API key for it is stored
  (`elastic_usage_api_key`, written like a provider key). *Admin → System* shows how the push is
  going.

A handful of values stay environment variables, because they gate config loading itself or are the
fallback for a deployment that would rather not put a key in the database:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `CUSTOM_AI_API_KEY` / `ELASTIC_USAGE_API_KEY` | Fallback provider and usage export keys, used only when nothing is stored via the Settings API |
| `MAIL_VERDICT_DATABASE_URL` | PostgreSQL connection, shared with PostIMAP |
| `POSTGRES_PASSWORD` | Database password, used by compose |
| `ENCRYPTION_KEY` | Encrypts provider keys stored via the Settings API, PostIMAP's own credential-at-rest encryption, and this server's Web Push signing key (generated on first use, never provisioned) — one key shared by all three. Optional; without it, provider keys can only come from the env vars above, and push notifications are unavailable |

## Access

Every request to the API and the MCP server needs an OpenID Connect token, which MailVerdict
validates against your identity provider's published keys: an ID token for a browser, forwarded by
an authenticating proxy in front of the web UI (oauth2-proxy, for example, which also handles the
sign-in), or an access token a native app or an agent sends itself. MailVerdict keeps no sessions
and no passwords of its own. Set the `auth:` section of
[`config/config.yaml`](config/config.yaml): the issuer, its key set, the audiences to accept, and
the owner's subjects. It refuses to start without them. A token issued for longer than
`auth.max_token_lifetime_seconds` (a day by default) is refused however valid its signature, so
long-lived tokens cannot outlast a revocation; phones renew their short access tokens on their own.

The identity provider decides who someone is; MailVerdict decides which of their sign-ins may
still be used. It lists every signed-in phone, a person's browsers, and every machine client (an
agent or script with its own token), with the name a phone gives itself and when each was last
seen. Anyone can sign out one of their own phones or browsers in *Settings*; an admin can revoke any
sign-in, and sign a person out everywhere at once. A revoked phone stays signed out until it signs
in again, a revoked machine client's subject stays refused, and a person's browsers come back once
they sign in again at the identity provider. Telling one phone from another relies on how Zitadel
shapes its access tokens (`auth.native_sign_in_handle`); with another identity provider, phones are
listed one per person.

Several people can share one instance. Each person sees only the mail, calendars and contacts of
the accounts they added. The owner is the first admin and owns whatever existed before there were
users; `auth.owner_subjects` signs them in on a new instance, and after that their subjects are
ordinary entries an admin adds and removes like anyone's. Admins manage users, their subjects and
who owns which account through `/api/admin`; no admin screen shows another person's mail. A new
person becomes a user the first time they sign in, unless `auth.auto_provision` is off, in which
case an admin adds them first. The chart README has a worked example.

Like any server-side mail client, MailVerdict is not end-to-end encrypted: whoever runs the server
can technically reach the mail it stores, and so can an admin, who can give any account to
themselves.

## On a phone

<p align="center">
  <img src="docs/images/phone.png" alt="The inbox and an open message at phone width" width="60%">
</p>

The web UI works at phone width and can be installed to the home screen, where it also gets push
notifications.

[mail-verdict-ios](https://github.com/frederikberg/mail-verdict-ios) is a native iPhone client for
the same server. It talks to the same API as the browser, signing in with the same identity provider and sending
its access token as a bearer token.

Its notifications go through a push relay run by the app's publisher, because only the publisher
can sign pushes for the app. Each notification is sealed on this server with a key only the phone
holds, so the relay forwards ciphertext:

- **The relay sees** the phone's device token, this server's IP address, and the size and timing
  of each push.
- **The relay never sees** a subject, a sender, an account, a message or a badge count, and it
  stores nothing.

Native push needs `ENCRYPTION_KEY`. The relays a phone may use are listed in
`push.relay_urls` in [`config/config.yaml`](config/config.yaml), and every push names the app
it is for (`push.relay_app`), since one relay serves several apps. Set it to `[]` to turn native
push off; nothing is then sent to any relay. With the chart's network policy enabled, allow egress
to those relays.

## Docs

- [docs/features.md](docs/features.md): every feature, area by area, and what reaches the model
  provider
- [docs/architecture.md](docs/architecture.md): how MailVerdict and PostIMAP divide the work, live
  updates, the message pipeline, orders, glacier storage, calendars
- [docs/api.md](docs/api.md): the REST API with worked examples, live updates, and the MCP server
  at `/mcp`; the generated OpenAPI document is served at `/api/openapi.json`
- [docs/development.md](docs/development.md): setup, the test layers, running a dev stack,
  releasing
- [charts/mail-verdict/README.md](charts/mail-verdict/README.md): the Helm chart
- [docs/IOS_PARITY.md](docs/IOS_PARITY.md): which web features the iPhone app has

Built with FastAPI, SQLAlchemy and Alembic on the server, React and Next.js on the client, and
FastMCP for the tool interface.

## Known limitations, deliberately

- **Folders can be created and deleted, never renamed or re-nested.** IMAP's rename operation
  renames every child folder along with it, so it can't be mirrored as a single-row update; faking
  it by creating a new folder, moving the mail and deleting the old one would lose flags and
  dates. Deleting a folder destroys every message in it on the server with no undo, so it always
  asks for confirmation naming the message count.
- **Accounts sign in with a password.** Providers that accept only OAuth, such as Google Workspace,
  cannot be connected.
- No multi-user isolation, no offline mode, no PGP.

## License

[MIT](LICENSE) — Frederik Berg
