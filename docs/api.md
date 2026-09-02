# API reference

MailVerdict is built to be driven by an agent as much as by the browser UI — the MCP server at
`/mcp` and the REST API at `/api` are both first-class surfaces over the same application. This
document is the entry point for the REST API: enough to get a client from nothing to a working,
configured instance, plus the two things about it that are easy to assume wrongly. It does not
restate the schema — every request and response shape, exhaustively, is the generated OpenAPI
document at `/api/openapi.json`, with an interactive form at `/api/docs`. Fetch that first; treat
this page as the map, not the territory.

[docs/architecture.md](architecture.md) explains *why* the API is shaped this way — the
PostIMAP split, how live updates reach a client, the message pipeline, threading. Read it before
building anything nontrivial on top of this API; the worked examples below assume it.

## Access

There is no authentication of any kind on this API. No key, no header, no session — every
endpoint answers whoever can reach it. The deployment model is an authenticating proxy in front
of the whole application (OIDC, basic auth, an internal SSO), handling sign-in before a request
ever reaches MailVerdict, so the application itself never checks a credential and never stores
one. See the [chart README](../charts/mail-verdict/README.md) for a worked proxy example. A
client talking to a production instance needs to authenticate to *that proxy*, in whatever way
it requires — MailVerdict has nothing of its own to add on top.

## Two ways a write can be wrong, and they behave differently

Most endpoints validate synchronously and reject a bad request outright: naming a folder that
does not exist to `POST /api/messages/{id}/action` with `action: "move"`, for instance, is a
`400`. The write never happens.

The pipeline configuration API is the one deliberate exception. A stage referencing a folder by
name or `special_use` is accepted even when nothing currently resolves it — `PUT /api/pipeline`
and `POST /api/pipeline/stages` both return `200`/`201` for such a document — because folders
arrive asynchronously as PostIMAP discovers them on an account that has not finished its first
sync yet. Rejecting the write would make it impossible to configure a pipeline for an account
before its folders exist. Instead:

- Every write endpoint under `/api/pipeline` still rejects outright anything that can never
  become valid later: an unknown stage type, an unknown effect or condition type, a duplicate
  `stage_id`. That class of error is a `400` exactly like everywhere else.
- An unresolved folder reference is reported as a warning on the `warnings` array of every
  document response (`GET`/`PUT /api/pipeline`, and every stage-mutating endpoint), and the same
  information is available standalone at `GET /api/pipeline/health` — one entry per stage per
  account it applies to, `ok` and not-`ok` alike.

A client that writes a pipeline stage and only checks the status code will believe a
misconfigured folder reference succeeded. Check `warnings` (or poll `/api/pipeline/health`) if
that distinction matters.

## Quickstart

Every example assumes the API is reachable at `http://localhost:8080/api` (production compose)
or `http://localhost:18080/api` (dev compose) with no auth in front, per above.

### Add an account and watch its first sync

```bash
curl -X POST localhost:8080/api/accounts -H 'content-type: application/json' -d '{
  "name": "Personal", "imap_host": "imap.example.com", "imap_port": 993,
  "imap_user": "me@example.com", "imap_password": "hunter2",
  "smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_user": "me@example.com",
  "smtp_password": "hunter2"
}'
# -> 201, AccountResponse with "id"
```

PostIMAP picks the account up without a restart. Poll sync progress:

```bash
curl localhost:8080/api/accounts/{account_id}/sync-status
```

`state` moves `created` → `syncing` → `active`, or `error`. `error` alone does not mean the
account is dead — PostIMAP retries unboundedly with backoff, and only `sync_state.last_full_sync`
being non-null tells apart "never once connected, needs a fix" from "worked before, having a bad
time now, usually self-heals." Watching a first sync in more detail is in
[architecture.md](architecture.md); the practical summary is that `folders_synced`/
`folders_total` on this response are the honest progress indicator, not a byte count.

### Set a provider key

Classification and embeddings need a key for whichever provider `ai.provider` names (`openai` or
`anthropic`; a bare-metal deployment can also fall back to the `OPENAI_API_KEY`/
`ANTHROPIC_API_KEY` environment variables instead of storing one). Through the API, a key is
write-only — it can be set and its presence checked, never read back:

```bash
curl -X PUT localhost:8080/api/settings/ai -H 'content-type: application/json' -d '{
  "data": {"openai_api_key": "sk-..."}
}'

curl localhost:8080/api/settings/ai
# -> {"provider": "openai", ..., "openai_api_key_configured": true,
#     "openai_api_key_hint": "...ab12"}
```

An empty string clears a stored key. `settings/defaults.py` documents every other setting category
(`ai`, `retry`, `pipeline`, `semantic`) and its default with a comment — that file is the
authoritative list, not repeated here.

### Edit the pipeline: write a rule as a stage

Spam classification and rules share one mechanism — see
["The message pipeline"](architecture.md#the-message-pipeline). A rule is a `match` stage: a
condition tree (`subject_contains`, `sender_domain`, `has_attachment`, `verdict_is` and more,
combinable with `all`/`any`/`not`) plus the effects to apply when it matches. `GET
/api/pipeline/stage-types` returns the full vocabulary as a JSON schema, so a client discovers it
rather than working from this list.

```bash
curl -X POST localhost:8080/api/pipeline/stages -H 'content-type: application/json' -d '{
  "stage_id": "archive-newsletters",
  "type": "match",
  "name": "Archive known newsletters",
  "config": {
    "when": {"sender_domain": "newsletter.example.com"},
    "effects": [{"move": {"special_use": "archive"}}]
  }
}'
```

`position` (omitted here, defaults to append) controls where in the stage order this lands —
stages run in order, and an earlier `halt: true` stage stops the rest. `GET
/api/pipeline/stage-types` returns every registered stage type's JSON Schema, generated from the
same Pydantic model the write path validates against, so it can never drift from what an actual
write accepts — fetch it before hand-writing a `config` blob. Try a stage against a real message
before trusting it:

```bash
curl -X POST localhost:8080/api/pipeline/stages/archive-newsletters/test \
  -H 'content-type: application/json' -d '{"message_id": "...", "origin": "live"}'
```

`/test` (and the whole-pipeline `POST /api/pipeline/test`) dry-runs against an existing message
with nothing applied or persisted — the same path the runner exposes for exactly that purpose.

Every write to `/api/pipeline` (`PUT` the whole document, or the per-stage `POST`/`PATCH`/`DELETE`)
accepts an optional `base_revision`: supply the revision your edit was computed against and get a
`409` on a stale write instead of silently clobbering a concurrent editor — the browser UI and an
agent editing at the same time, most notably. Omit it to write unconditionally.

### Search semantically

```bash
curl 'localhost:8080/api/embeddings/search?q=that+invoice+about+the+conference+trip&limit=10'
```

Complements `GET /api/search` (literal full-text) rather than replacing it — semantic search wins
on a half-remembered topic with none of the same words, full-text wins on a known sender or an
exact phrase. Coverage is never assumed complete: `GET /api/embeddings/status` reports what
fraction of in-scope mail has a current-model embedding, and `POST /api/embeddings/backfill`
enqueues whatever is missing rather than waiting out the periodic reconciler.

## Endpoint groups

One line each; the OpenAPI document has every parameter, every response field, every status
code. Everything is under `/api` and every id is a UUID unless noted.

| Group | Prefix | What it's for |
|---|---|---|
| Accounts | `/accounts` | Create, update, delete, list; folder listing; sync status and manual sync trigger |
| Identities | `/identities` | An account's addresses to send as — create, update, delete, list (optionally scoped by `account_id`); at most one default per account, enforced at the database level. `POST /outbox`'s `identity_id` resolves through these |
| DAV accounts | `/dav-accounts` | CalDAV/CardDAV server connections, mirrored the same way a mail account is — create, update, delete, list with each account's collections, manual sync trigger. Every route under this and the four groups below answers `501` until PostIMAP reports `service_version >= 1.6.0` |
| Calendars | `/calendars`, `/calendar/links`, `/addressbooks` | Calendar create/update/delete/list, merged with local prefs (colour, invitation intake, linked identity); deletion destroys every event in it on the server, so `DELETE /calendars/{id}` requires a `confirm_event_count` query parameter the same way folder deletion does. `/calendar/links` reads and replaces the whole identity-to-calendar mapping as one document, optimistic on `base_revision`. `/addressbooks` lists address books |
| Calendar events | `/calendar/events` | Month-windowed list with recurring series expanded to instances; detail; create (`tz`, an IANA zone name, binds `dtstart`/`dtend` to a named zone rather than only a fixed UTC offset — rejected with `400` for an all-day event or an unrecognised zone); edit with `scope=this\|following\|all` (`following` — splitting a series in two — is rejected with `422`, not implemented; `attendees` and `tz` in an edit are also rejected with `422` rather than silently ignored, since neither is applied); delete/cancel; `POST /{id}/respond` records an RSVP and sends the `METHOD:REPLY` over the linked identity's outbox |
| Invitations | `/calendar/invitations` | The message-shaped view of `calendar/intake.py`: `GET /{message_id}` returns the parsed invitation plus its intake status, running the same decision live for a message intake never saw; `POST /{message_id}/import` imports it (or retries a failed import) into a chosen calendar |
| Contacts | `/contacts` | Paged list with address-book filter and free-text search; `/contacts/search` returns one row per email address for compose autocomplete; structured create/update/delete parsed from the vCard body |
| Folders | `/accounts/{id}/folders`, `/folders/{id}` | Create and delete a folder, folder display prefs, custom ordering — see [known limitations](../README.md#known-limitations-deliberately) for what folder management deliberately does not do. Deletion is irreversible and destroys every message in the folder on the server, so `DELETE /folders/{id}` requires a `confirm_message_count` query parameter matching the folder's current message count; a call without it (or with a stale count) is a `409` naming the actual count rather than a deletion |
| Messages | `/accounts/{id}/messages`, `/messages/{id}` | Cursor-paginated list (optionally threaded), detail, thread, attachment streaming, `.eml` download, single and bulk actions |
| Outbox | `/outbox` | Send, save or edit a draft (JSON or multipart with attachments); list outbox rows for send/draft status. `identity_id` picks which of the account's identities to send as, falling back to its default identity and then to `accounts.imap_user` |
| Search | `/search`, `/embeddings/search` | Full-text and semantic search, respectively |
| Verdicts | `/verdicts`, `/mails/{id}/verdict`, `/mails/{id}/feedback` | Spam verdict history and the user-correction feedback loop |
| Pipeline | `/pipeline` | Read/replace the whole stage document, per-stage CRUD and reorder, stage-type schemas, revision history and restore, health, dry-run testing — see the quickstart above and [architecture.md](architecture.md#the-message-pipeline) |
| Pipeline runs | `/runs`, `/mails/{id}/runs` | Per-message pipeline execution history and trace — "why did this message get that treatment"; retry a failed run |
| Queues | `/queues` | Every registered background queue's state (embedding, pipeline), live concurrency control |
| Embeddings | `/embeddings` | Coverage status, on-demand backfill, semantic search (see above) |
| Settings | `/settings` | Every runtime-configurable behaviour by category, plus the write-only provider-key extension on `ai` — see [Config and settings never overlap](../README.md#configuration) |
| Image exceptions | `/accounts/{id}/image-exceptions` | Per-sender/per-domain allowlist for remote image loading |
| Notifications | `/accounts/{id}/notifications` | The durable, acknowledgeable record of a write that never reached the server — see [architecture.md](architecture.md#notifications) |
| Unified view | `/unified`, `/accounts/{id}/emoji` | Cross-account folder merging and message listing |
| Stats | `/stats` | Dashboard aggregates: spam metrics, accuracy trend, per-account sync summary |
| Events | `/events` | Server-Sent Events stream — see below |
| Health | `/health`, `/health/live` | Readiness (DB + PostIMAP contract confirmed) and liveness |

## Live updates

`GET /api/events` is a Server-Sent Events stream, not a resource endpoint — connect once and keep
it open rather than polling. [architecture.md](architecture.md#events-and-live-updates) covers
the mechanism (PostIMAP's notification channel, the per-account ring buffer, why folder counts
are deliberately not evented); the two things a client integrating against it needs to know:

- Pass `?account_id=` to scope the stream to one account, or omit it for every account this
  instance knows about.
- Reconnect with the last event id you saw, either via the standard `Last-Event-ID` header (what
  a browser's `EventSource` sends automatically on its own reconnect) or a `?last_event_id=`
  query parameter for a client that manages its own connection. A gap too large to replay from
  the in-memory ring sends a `resync` event instead of the missed ones — treat it as "invalidate
  everything you cached from this stream," not as one more event to interpret.

## Building a client against this API

The MCP server at `/mcp` (FastMCP, streamable-http transport) wraps a curated subset of this
same functionality — search, read, organise, send, spam feedback, semantic search, and reading,
creating, editing and deleting calendar events and contacts — as typed tools for an MCP client,
and is very often the better fit for an agent than calling REST directly. Each event tool takes a
raw `RRULE` value where recurrence applies, the same full RFC 5545 vocabulary the REST API
accepts, not a fixed preset. Reach for the REST API when the MCP surface does not cover what's
needed (folder or pipeline management, queue control, settings, or creating/deleting a calendar
or address book itself) or when building the browser UI.
