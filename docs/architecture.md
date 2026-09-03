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
- **`queue/`** — a Postgres-native work-queue engine (claim, lease, backoff, a persisted circuit
  breaker), parameterised by table and knowing nothing about what it queues.
- **`pipeline/`** — the one machinery every message passes through: stages, effects, the runner,
  and enqueueing an arrival. See "The message pipeline" below.
- **`spam/`, `settings/`** — feedback recording and application settings on top of the mail data.

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

## The message pipeline

Spam classification and rules are one system: a message passes through an ordered list of
*stages*, each returning declarative *effects* for the runner to apply, and spam classification
is simply the `classify` stage. Processing is sequential per message, parallel across messages —
`pipeline_runs` is a work queue (`queue/`), claimed by an asyncio worker pool inside the same
process.

A stage never writes SQL and never calls an action directly:

```
message arrives ──► pipeline_runs row ──► runner claims it
                                              │
                                for each stage in the pipeline definition:
                                   outcome = stage.execute(view, ctx)
                                   apply outcome.effects, each guarded
                                   project the applied effects onto `view`
                                   if outcome.halt: stop
```

Two built-in stage types today: `match` (a rule, generalised — the same condition tree, plus a
`verdict_is` condition) and `classify` (one structured-output model call, writing a `RecordVerdict`
effect and nothing else — it does not move mail). "Spam moves to Junk" is an ordinary `match`
stage composed on top of `classify`'s output, not a side effect hidden inside the classifier.

`api/pipeline.py` is how the definition is edited — inserting into `pipeline_revisions` by hand is
not the intended path. `GET`/`PUT /api/pipeline` read and replace the whole document (`PUT`
optionally carrying `base_revision` for optimistic concurrency, so an agent and the UI editing at
once get a `409` rather than one silently clobbering the other), `POST`/`PATCH`/`DELETE
/api/pipeline/stages` and `.../reorder` operate on one stage at a time, and `GET
/api/pipeline/stage-types` returns each registered type's JSON schema — generated from the same
Pydantic model `pipeline/registry.py` validates a write against, so it cannot drift from what
actually gets accepted. Validation itself splits in two: an unknown stage type, unknown effect,
unknown condition type or duplicate stage name can never become valid later and are rejected
outright; an unresolved folder reference is accepted, since folders appear asynchronously as
PostIMAP discovers them, and is reported instead as a warning on every document response and
through `GET /api/pipeline/health`. `POST /api/pipeline/test` (and `.../stages/:id/test`) dry-run
the definition, or one stage of it, against an existing message with nothing applied — the same
path `pipeline/runner.py`'s `dry_run`/`dry_run_stage` expose for that purpose alone, never
registered with the queue manager.

A stage that cannot do its job raises rather than returning a success flag — a `Move` effect
whose target folder does not resolve is exactly the kind of failure a success-flag result type
would let slip through as reported success on a write that did nothing. The exception type tells
the runner whether to retry with backoff, suspend the queue (a provider outage or a rejected key,
refunding the attempt), or fail the run permanently with the offending stage named in
`pipeline_runs.failed_stage`.

### Triggered by arrival only

A live message is embedded before it ever reaches the pipeline. `message`/`insert` with
`origin = "sync"` enqueues a `message_embeddings` row, not a `pipeline_runs` row — the pipeline
row is only inserted once that embedding reaches a terminal state, `done` or `failed`, in the same
transaction as the write that reaches it (`embeddings/repository.py`, calling
`pipeline.enqueue.enqueue_pipeline_run_if_live_eligible`). Both the embedding call and the
classify call hit the same provider, so gating on the first costs no real availability — if the
provider is down, nothing downstream was going to be classified either — and it buys the
invariant that everything in the pipeline queue has a vector, which is what neighbour hints below
depend on. A message whose embedding permanently fails is not stranded: reaching `failed` opens
the gate exactly as `done` does, just with no neighbour hints available, which the classify stage
records in its own trace. Reconciliation's gap-recovery pass (a listener reconnect) respects the
same gate, so it cannot enqueue a run ahead of a still-pending embedding.

Never on an update, either way. A stage reacting to a folder-move update could loop on its own
writes: PostIMAP's `origin` field distinguishes its own sync writes from this application's, but
not the pipeline's own write from a user's a moment later, so a move-triggered stage acting on a
message its own move-to-junk effect just relocated is one edit away from acting on itself again. A
folder move is handled by a separate, stateless listener instead (`spam/feedback.py`): it records
a correction only when the move contradicts the message's *current* verdict, which is something
the classifier can never do to what it just wrote, and excludes moving spam to Trash — deleting
mail already agreed to be spam is the ordinary use of a junk folder, not a correction.

### Never classifying the same message twice

Sending every message to a language model would be expensive and, on a first sync, absurd — a
new account against an existing mailbox would classify years of history as if it had all just
arrived.

Layered gates prevent it:

- **Backfill suppression.** During a folder's first full sync PostIMAP suppresses per-message
  events entirely and emits one completion event instead. Historical mail never reaches the
  pipeline as a live arrival at all.
- **A durable verdict record.** Verdicts are keyed by the account and a `msg_key` under a unique
  index — the message's RFC `Message-ID` header when it has one, otherwise a hash of its envelope
  (sender, subject, receipt time, size). Either form is stable across resyncs, so a folder that is
  fully resynchronised — after a UIDVALIDITY change, say, where rows are recreated with new
  identifiers — cannot trigger reclassification, including for messages with no header at all. The
  index also constrains the sender: a message forging the `Message-ID` of one already verdicted
  does not inherit that verdict, it gets its own. `classify` checks this before ever calling a
  model.
- **Folder, draft and account scope.** A message in Sent/Drafts/Trash/Junk/Archive, or a draft,
  never enters the queue; `classify` additionally skips an account with spam detection disabled.

Backfill suppression only applies to a folder's *first* sync, so events from a later resync are
indistinguishable from new mail — the verdict record is what tells them apart there. A `classify`
stage instance is also structurally ineligible for anything but a live-origin run, which matters
once a deliberate historical reprocessing pass exists: a property of the stage type, not a
parameter a sweep request could get wrong.

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

### Neighbour hints, and why the classifier's own verdicts never feed them

Mail is repetitive — the same sender, the same template, month after month — so a new message's
nearest neighbours are dominated by near-identical past mail. If the classifier's own verdicts
were part of that neighbour pool, a wrong first verdict for a sender would become permanent: every
later near-identical message would see that verdict as a neighbour, agree with it, and become
another neighbour agreeing with it — indistinguishable, from the outside, from the model actually
being right. `pipeline/neighbors.py`'s `NeighborService` closes this by construction: its pool is
exactly two kinds of *human*-originated evidence — an explicit user correction
(`verdicts.source = 'user_feedback'`), or the folder a message currently sits in — and never a
`source = 'ai'` or `'rule'` row. Folder placement is asymmetric evidence rather than a second kind
of verdict: Junk membership is a strong spam signal, but sitting in the inbox is weak evidence of
not-spam (the message may simply not have been dealt with yet), and the prompt states both
directions explicitly rather than collapsing them into one label. Off by default
(`settings.semantic.neighbor_hints_enabled`), so its effect on accuracy can be measured before it
is ever the default; there is no near-duplicate short-circuit, since even a very close match stays
a hint for the model to weigh, never a reason to skip classifying.

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

## Notifications

A write to a column the contract grants is accepted immediately and reaches the mail server
afterwards; between those two moments it can fail permanently — the folder is gone, the
credential stopped working, the server rejects the flag, an SMTP send never leaves. PostIMAP
writes a durable `sync_notifications` row for every such failure, mirrored read-only as
`SyncNotification` (`database/models.py`), covering a failed sync operation and a failed send
alike. `acknowledged_at` is the only column a consumer writes; everything else is PostIMAP's own
account of what it attempted and why it gave up.

The row's insert fires a `notification` event on `postimap_events`, which the listener forwards
as `notification.new` over SSE so a client refreshes rather than polls. `reverted_at` records
whether PostIMAP has since put the mirror right for that one operation — re-reading a message's
flags after a failed flag change, restoring `folder_id`/`imap_uid` after a failed move, and so
on. A row with `reverted_at` still NULL is one where the value written through this application
may still be sitting in the column looking applied, when the server never actually received it.

Available from PostIMAP `service_version` 1.3.0 onward, gated in `postimap/contract.py` the same
way folder CRUD is.

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

## Calendars and contacts

CalDAV and CardDAV are mirrored the same way IMAP is: PostIMAP does the protocol work, mirroring
`dav_accounts`/`dav_collections`/`dav_objects`/`dav_notifications` the same way it mirrors
`accounts`/`folders`/`messages`/`sync_notifications`, and MailVerdict never imports a DAV client
library of its own. Available from PostIMAP `service_version` 1.6.0 onward, gated in
`postimap/contract.py` (`supports_dav()`) the same way folder CRUD is. `calendar/repository.py`
holds the SELECT-only reads on those four tables; every write still goes through
`postimap/actions.py`.

The unit of sync is the whole resource: one `dav_objects` row holds a whole VCALENDAR body — the
master `VEVENT` plus every `RECURRENCE-ID` exception — as verbatim text. Recurrence is never
expanded in storage. `calendar/ical.py` is where a body is parsed, expanded over a date range
(`recurring-ical-events`), and edited; `calendar/vcard.py` does the same for contacts
(`vobject`). Neither touches the database — the API layer hands the result to
`postimap/actions.py`.

The month view reads only what its window could contain: `list_in_collections()` filters in SQL to
a recurring master (any occurrence could fall inside the window) or a non-recurring object whose
own `[dtstart, COALESCE(dtend, dtstart))` overlaps it, using PostIMAP's own parsed
`dtstart`/`dtend`/`is_recurring` columns rather than parsing and expanding every object in every
visible calendar on every request. The `COALESCE` matters: PostIMAP only ever writes `dtend` from
an explicit `DTEND` property, so a `DURATION`-only event or an all-day `DTSTART;VALUE=DATE` with
neither leaves it `NULL`, and comparing a `NULL` `dtend` directly would silently drop the row under
SQL's three-valued logic.
Updating an event with attendees sends a `METHOD:REQUEST`, the same as creating one and the same as
deleting one sends `METHOD:CANCEL`, whenever the calendar's own identity is the `ORGANIZER`.
Correcting a wrong password on an already-running `dav_accounts` row force-reconnects it the same
way a mail account's credential change does — an `is_active` bounce across two committed
transactions, since a credential rewritten on a running account is not re-read until it restarts.

A DAV write PostIMAP could not complete surfaces on the event it concerns: `get_write_errors()`
reads `dav_notifications` for both a still-unresolved failure and a `412` conflict PostIMAP already
resolved by re-reading the server's copy over the row (`reverted_at` set) — the one case where a
user's edit was actually discarded rather than merely still pending, worded accordingly. A full DAV
notification centre (list/acknowledge endpoints, its own UI surface, mirroring the mail side's
bell) is not built; only the event-level surfacing above is.

Three tables are owned by MailVerdict, migrated alongside `identities`:

- **`calendar_prefs`** — one row per calendar, linking it to an identity and marking at most one
  calendar per identity as the one that receives its invitations (a partial unique index, the same
  shape as `identities.is_default`). `identity_id` carries a real foreign key onto `identities`
  (`ON DELETE SET NULL`) — both tables are MailVerdict-owned, so there is no grant boundary to
  cross the way there is onto a PostIMAP table.
- **`calendar_intake`** — the never-classify-twice gate's calendar counterpart, keyed on the same
  `(account_id, msg_key)` identity `verdicts` and `message_embeddings` use.
- **`calendar_replies`** — insert-only: every RSVP attempt gets its own row rather than overwriting
  one in place, so a reply that failed to send and was retried keeps its history. `own_reply` on an
  `EventInstance` is the latest row for `(object_id, recurrence_id)`, with its outbox status read
  live by joining `outbox_id` — never copied, since that status changes as PostIMAP processes the
  send.

Responding to an invitation (`POST /api/calendar/events/{id}/respond`) updates the held object's
`PARTSTAT` immediately and sends the `METHOD:REPLY` iTIP message over the identity's own outbox,
never the server's own scheduling engine — every object this application stores or sends carries
`SCHEDULE-AGENT=CLIENT` on the relevant `ORGANIZER`/`ATTENDEE` line for exactly that reason. A
failing send is not rolled back; calling `respond` again inserts a fresh `calendar_replies` row and
outbox attempt, the same "retry never destroys history" shape sending mail already has.

Creating an event with attendees requires its calendar to have a linked identity — there is nothing
else to send the `METHOD:REQUEST` invitation from — and is refused with `409` otherwise.

**Invitation intake** turns an emailed `.ics` attachment into a calendar entry automatically.
`calendar/intake.py` is a listener like `spam/feedback.py`, not a pipeline stage: it reacts to
`message`/`insert` with `origin = "sync"`, and for the same reason backfilled mail is never
classified, backfilled mail is never imported either — the contract's backfill suppression means
no per-row `message` event fires at all while a folder's first sync is in progress, so an arrival
this listener ever sees is live mail by construction, with no separate watermark needed.

`CalendarIntakeHandler.decide()` is a pure read — given a message and its parsed invitation, it
works out the outcome (which calendar, which existing object, which `calendar_intake` status)
without writing anything. `handle_message_event()` writes the `calendar_intake` row first, gated
on its own uniqueness on `(account_id, msg_key)`. For an outcome `_apply()` will actually write
something for (`imported`/`updated`/`cancelled`) the row is inserted as `pending` and promoted to
its real status only once that write lands, so anything interrupting between the two leaves an
honestly-labelled row rather than a terminal status with no object behind it; a `pending` row
found on a later call is retried rather than treated as already handled. A data migration rewrites
any row a pre-`pending`-status build already stranded (`imported` with no `object_id`) to
`pending`, so it heals rather than staying permanently unprocessable. `api/invitations.py`'s `GET`
reuses `decide()` for a message the listener never saw (backfilled mail, or one that arrived before
intake was wired up) to render the same view without ever writing `calendar_intake` itself; only
the explicit `POST .../import` writes for that case, which is what keeps "backfilled mail is never
imported automatically" true while still letting a person choose "add to calendar", or confirm a
change, for it.

A `REQUEST` or `CANCEL` naming a UID already held by a `dav_objects` row is never applied
automatically, whoever it claims to be from. Matching the incoming `ORGANIZER` against the held
object's own was tried and does not authenticate anything a forger could not already produce: a
co-attendee of the real meeting already holds both the UID and the `ORGANIZER` address, since both
are lines in the same `.ics` they themselves received. Every such message becomes a
`pending_review` intake row instead, resolved wherever the UID already lives rather than imported a
second time (the hand-imported case, and what keeps two of an identity's own addresses invited to
the same event from producing two copies) but left untouched until a person confirms it through
`POST .../import` — the same endpoint the manual "add to calendar" flow uses, branching on the
invitation's own `METHOD` to apply a `CANCEL` as `STATUS:CANCELLED` rather than merging it into the
event's fields the way a `REQUEST` is (a `.ics` carrying `METHOD:CANCEL` commonly has little more
than the UID and `ORGANIZER`, and would blank the event out otherwise). The confirmation UI is the
actual defence: it shows the address the message came from next to the `ORGANIZER` it claims, and
says plainly what accepting would do. A `REPLY` is the one method still authorized and applied
automatically, against the message's own sender rather than the `ORGANIZER` — a mismatch is
`unauthorized` — before it updates the matching `ATTENDEE`'s `PARTSTAT` on the held object; the harm
a forged one can do is a wrong attendance mark, not a moved or cancelled meeting. A `REQUEST` naming
a UID nothing holds is a genuinely new invitation and keeps importing on its own, since the worst it
can do is calendar spam. The existing-object lookup is scoped to DAV accounts reachable from the
receiving mail account's own identities (`calendar_prefs.identity_id`) for the automatic listener;
`POST .../import` resolves a UID anywhere, since a person confirming one named message is not the
unauthenticated surface the scoping exists for, and `GET` on the same message matches that
unscoped resolution whenever the listener's own narrower one finds nothing, so the card never
describes an outcome the confirmation button would not actually produce. Past authorization, a
lower `SEQUENCE` than the held object's own is stale and ignored -- the one case still resolved
automatically, since an old `SEQUENCE` is far more likely a resend than something worth a person's
attention, and it never writes anything either.

Identity resolution for automatic import requires the identity to actually appear in the
`ATTENDEE` list — being a `to_addrs`/`cc_addrs` recipient is not being invited, and that fallback
was also how a spam invitation (or an unbounded `RRULE`, see below) reached the calendar before the
message was ever classified. `resolve_attendee_identity()` never falls back to to/cc for anyone —
the manual "add to calendar" flow needs no identity resolution at all, since the person doing it
names the calendar directly. An invitation to an address linked to no calendar
(`calendar_prefs.intake`), or whose intake calendar is `read_only`, is left `unlinked`, surfaced by
`GET /api/calendar/invitations/{message_id}` with the candidate calendars to import into.

`ical.expand_instances()` bounds a recurring series' expansion regardless of how the RRULE (or
RDATE) that produces it is spelled: it walks the requested window in progressively wider slices,
collecting the real occurrences `recurring-ical-events` returns for each slice by identity — a
slice hands back everything *overlapping* it rather than only what starts inside it, so an
occurrence wider than the slices it touches comes back from each of them — and refuses once more
than a few thousand have come back — rather than asking the library for the whole window in one
call, which is what a `FREQ=SECONDLY` series, or one whose `BYHOUR`/`BYMINUTE`/`BYSECOND` widen a
coarser `FREQ` to the same effect, or a large `RDATE` list, turns into tens of seconds and hundreds
of MB, synchronously, on the same event loop as mail sync and health checks — inspecting the
RRULE's own text first was tried and does not reliably catch any of those. Creating or editing an
event refuses `FREQ=SECONDLY`/`MINUTELY` outright, as a cheap first line rather than the only one.
`list_events` skips an object it cannot expand or parse rather than failing the whole request.
`SEQUENCE` only advances when the calendar's own identity organizes the event (or the
event has no `ORGANIZER` at all) — bumping it on an edit to an event held only as an attendee would
make the real organizer's next genuine update compare as stale against the check above and be
silently discarded.

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
