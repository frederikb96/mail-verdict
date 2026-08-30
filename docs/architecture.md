# Architecture

MailVerdict is a web mail client with an AI layer on top. It does not speak IMAP or SMTP. All
mail transport is handled by [PostIMAP](https://github.com/frederikb96/postimap), a separate
service that mirrors IMAP mailboxes into PostgreSQL in both directions.

The two services share a database and never talk to each other directly.

```
      browser
         │  HTTP + SSE
         ▼
   MailVerdict  ──────────┐
   (FastAPI + React)      │  SQL + LISTEN/NOTIFY
         │                ▼
         └──────────► PostgreSQL ◄──────────  PostIMAP
                                  SQL + triggers    │  IMAP + SMTP
                                                    ▼
                                              mail server
```

## Why the database is the interface

PostIMAP has no HTTP API and is not going to grow one. A consumer reads and writes plain SQL
and listens on a notification channel. Marking a message read is:

```sql
UPDATE messages SET is_seen = true WHERE id = $1;
```

A trigger enqueues the change, PostIMAP applies `\Seen` on the IMAP server, and the row already
reflects the new state. There is nothing to poll and no request to wait on.

This is what keeps MailVerdict simple. Forty years of IMAP inconsistency lives on the other side
of the database, and this application is an ordinary Postgres-backed web app.

The full set of tables, the exact columns a consumer may write, the notification payloads and
worked SQL for every operation are defined in PostIMAP's
[consumer contract](https://github.com/frederikb96/postimap/blob/main/docs/consumer-contract.md).
That document is authoritative; this one only explains how MailVerdict uses it.

## Layers

- **`api/`** — HTTP routers. Reads the database, and performs writes exclusively by calling
  `postimap/actions.py`. No IMAP or SMTP imports exist anywhere in the codebase, and none should
  ever appear.
- **`postimap/`** — the only module that knows the contract. Holds the supported contract
  version, the event listener, the command channel, and every write the contract permits. Keeping
  all contract SQL in one file means the statements PostIMAP's triggers depend on have exactly one
  definition.
- **`database/`** — connection handling, models and queries. Models of PostIMAP's tables are a
  projection the application asserts, not a schema it owns.
- **`spam/`, `rules/`, `settings/`** — application logic on top of the mail data.

## Contract version handshake

PostIMAP publishes a contract version in a single-row table. MailVerdict asserts it at startup
and refuses to run against a version it was not built for.

A mismatch is fatal rather than degraded. The failure mode this prevents is a schema assumption
that is quietly wrong — where queries succeed, data looks plausible, and something subtle is
broken far from the cause.

Readiness stays false until the database is reachable *and* the version matches, so deployment
order does not matter. Liveness only reports that the process is up.

## Events and live updates

PostIMAP emits every change a client might care about on one channel, covering messages, folders,
accounts and the outbox. Each payload carries the row's identity, which columns changed, and an
`origin` field distinguishing PostIMAP's own sync writes from writes made by this application.

```
postimap_events ──► listener ──► EventRing ──► SSE ──► browser
```

The EventRing is an in-memory ring buffer per account with monotonic ids, so a browser that
reconnects replays what it missed via `Last-Event-ID` rather than refetching everything.

Two deliberate details:

- **Counts are not evented.** Folder totals change on every sync cycle and notifying on each
  would be noise. The UI re-reads counts when a message event arrives for that folder.
- **A listener reconnect emits a resync event.** Notifications sent while the connection was down
  are gone for good, so clients are told once to invalidate everything rather than silently
  holding stale data.

## Owned tables carry no foreign keys onto PostIMAP's

MailVerdict's own tables live in the same database and reference PostIMAP rows by plain UUID
columns, with no foreign key constraints. Two independent reasons, either sufficient:

- **Grants.** PostIMAP enforces its write contract through PostgreSQL privileges. The consumer
  role is granted SELECT, INSERT and specific UPDATE columns — not `REFERENCES`. Creating a
  foreign key onto its tables fails outright on any deployment that actually uses that role.
- **Retention.** PostIMAP purges expunged messages after a configurable window. A cascading
  foreign key would delete verdicts along with them, destroying the record of what has already
  been classified.

The cost is that joins are explicit and orphaned rows need occasional cleanup. Both are cheap.

## Never classifying the same message twice

Sending every message to a language model would be expensive and, on a first sync, absurd — a
new account against an existing mailbox would classify years of history as if it had all just
arrived.

Three gates prevent it:

- **Backfill suppression.** During a folder's first full sync PostIMAP suppresses per-message
  events entirely and emits one completion event instead. Historical mail never reaches the
  pipeline.
- **Folder and account scope.** Only regular and inbox folders are classified, and only for
  accounts where the feature is enabled.
- **A durable verdict record.** Verdicts are keyed by the account and a `msg_key` under a unique
  index — the message's RFC `Message-ID` header when it has one, otherwise a hash of its envelope
  (sender, subject, receipt time, size). Either form is stable across resyncs, so a folder that is
  fully resynchronised — after a UIDVALIDITY change, say, where rows are recreated with new
  identifiers — cannot trigger reclassification, including for the messages that have no header at
  all. The index also constrains the sender: a message forging the `Message-ID` of one already
  verdicted does not inherit that verdict, it gets its own.

The third gate is what makes this correct rather than merely usual. Backfill suppression only
applies to a folder's *first* sync, so events from a later resync are indistinguishable from new
mail. The verdict record is what tells them apart.

## The semantic layer

`message_embeddings` (`embeddings/`) holds one pgvector row per message — subject, sender and the
first `content_chars` of the body, HTML-stripped when there is no plain-text body, envelope-only
when PostIMAP never fetched a body at all. It is keyed on the same `(account_id, msg_key)` identity
as `verdicts`, for the same reason: `messages.id` does not survive a UIDVALIDITY resync, and a
vector keyed on it would be silently orphaned.

The table is also its own work queue — `status`/`attempts`/`claimed_by`/`lease_expires_at` are the
generic engine in `queue/` (see its own module docstrings), parameterised by table rather than by
what it queues. Filling it is a backfill sweep: a self-advancing set-difference batch —
"messages missing a current-model embedding, `LIMIT n`" — run repeatedly rather than a cursor walk.
Inserting a row makes the message stop matching, so the query needs no persisted position, resumes
correctly after a crash, and is immune to a resync recreating message ids underneath it. `model` is
part of a row's identity, not a separate column to keep in sync — changing it in the `semantic`
settings category makes coverage for the new model start at zero rather than mixing two vector
spaces in one index; old rows are kept, not deleted, until the new coverage completes.

Search (`GET /api/embeddings/search`, MCP `semantic_search_mail`) embeds the query text and orders
messages by cosine distance, joined back to `messages` at read time — never a denormalised copy of
anything that changes, matching the no-foreign-key posture above. It complements full-text search
rather than replacing it: literal search wins on a known sender or an exact phrase, semantic search
wins on a half-remembered topic with none of the same words.

## Sending mail

MailVerdict does not open an SMTP connection. Sending is an insert into an outbox table with
structured fields; PostIMAP composes the message once, transmits it, and appends those same bytes
to the Sent folder, so the stored copy can never differ from what was actually sent. A draft uses
the same mechanism with the send step skipped.

The appended copy returns through the normal inbound sync, which means a sent message appears in
Sent on the next sync of that folder rather than instantly. The interface therefore reflects the
outbox row's status — which *is* evented — instead of waiting for the message to come back.

Editing a saved draft, or sending one, is the same insert with `replaces_message_id` set to the
draft's `messages.id`. PostIMAP appends the replacement first and only then removes the message it
names, so the two briefly coexist in Drafts; the composer renders from its own state rather than
by re-reading the mailbox, which is what makes that gap invisible.

## Folders

Creating a folder is an insert; IMAP has no parent concept, so the full path is built by joining
onto a parent folder's name with the account's own separator before the insert happens. Deleting
one sets `deleted_at`, which destroys every message in it on the server, irreversibly — refused
outright for INBOX rather than accepted and dead-lettered later. Both require a PostIMAP version
new enough to grant them, checked the same way account deletion is: a service-version comparison
at the call site, not the contract version, since granting a permission breaks nothing a consumer
already does.

## Threading

Conversations are grouped by a thread identifier that PostIMAP resolves from the `References` and
`In-Reply-To` headers when a message is stored, merging threads that turn out to be connected.
Replies sent from this application carry those headers, so the Sent copy joins the conversation it
belongs to when it syncs back.

There is no subject-based fallback. The raw headers remain on every row if one is ever wanted.

## Configuration and settings

Two separate mechanisms that must not overlap:

- **Config** is infrastructure — server, database, the encryption key. It comes from files and
  environment variables and takes effect at startup. `config/config.yaml` holds every default with
  a comment and is the only place a default exists; the application fails at startup rather than
  substituting a fallback for something missing.
- **Settings** are application behaviour — AI provider, model, reasoning effort, spam handling,
  rules, and provider API keys. They live in the database and change at runtime through the API.

Provider API keys sit inside the "ai" settings category but are write-only: settable, reportable
as present with a last-four-character hint, never returned by any read. They are encrypted at rest
with `security.encryption_key` (AES-256-GCM), the one config value in this system that protects a
setting rather than being one itself. An environment variable (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`) is the fallback for a deployment that would rather keep a key out of the database
entirely — read fresh on every call, so switching from the env var to a stored key, or rotating a
stored one, takes effect on the next request with no restart.

## Access

The application has no login and no auth mechanism of its own — nothing checks a header or a key
on any endpoint. The deployment model is an authenticating proxy in front of it, handling sign-in
before traffic ever reaches the application, so users never hold an application credential at all.
