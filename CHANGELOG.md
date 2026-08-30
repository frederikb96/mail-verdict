# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Breaking Changes

- **Application-level auth removed entirely:** `MAIL_VERDICT_API_KEY`, `require_auth`, `ApiKeyASGIMiddleware` and every `X-API-Key` check are gone from `/api/*`, `/mcp` and the SSE endpoint. The deployment model is an authenticating proxy in front of the application; the chart's `secret.apiKey` / `existingSecretKeys.apiKey` values are gone with it
- **Settings genuinely take effect at runtime:** the pipeline runner consults the current pipeline definition and settings on every claimed run, instead of only existing when spam was enabled or a rule list was non-empty at process start. Enabling spam, editing the pipeline, or switching AI provider/model/reasoning effort through the settings API changes behaviour on the next message, not the next restart
- **Default AI provider is now OpenAI**, default model `gpt-5.4-nano`, default `reasoning_effort` `none`. Anthropic remains fully supported via `ai.provider: "anthropic"`
- **Provider API keys move into the database, encrypted:** settable and reportable as present (with a last-four hint) through `PUT /api/settings/ai`'s `anthropic_api_key` / `openai_api_key` fields, never returned by any read. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars remain a fallback. Requires `security.encryption_key` (`ENCRYPTION_KEY`, AES-256-GCM, 64 hex chars) — the same key format and, optionally, the same value PostIMAP uses for its own credential encryption
- **Config shrunk to infrastructure only:** `server.api_key` is gone; `security.encryption_key` is the only addition
- **Spam classification uses a strict JSON schema** (Anthropic's `output_config.format`, OpenAI's `text.format` with `strict: true`) instead of a loosely-requested JSON object, and every verdict now carries a one-sentence `reasoning` alongside the classification
- **Spam classification and rules are one pipeline.** `rules/engine.py`, `rules/executor.py`,
  `rules/enrichment.py` and `spam/pipeline.py` are gone. A message now passes through an ordered
  list of *stages* — `match` (a rule, generalised: the same condition tree plus a `verdict_is`
  condition, with the same effects) and `classify` (one structured-output model call, writing a
  verdict and nothing else — it no longer moves mail itself). `spam.auto_move_to_junk` /
  `spam.auto_mark_read` become a default `match` stage instead of a hardcoded side effect of
  classification. `settings.rules` and the `rules` settings category are retired; an existing
  deployment's rules and spam settings are migrated automatically, on first startup after
  upgrading, into the first pipeline revision (Alembic `0006_pipeline`) — a rule whose trigger was
  `mail.moved`/`mail.trashed`/`mail.deleted` cannot be migrated (the pipeline is triggered by
  message arrival only, see below) and is dropped with a startup warning naming it.
  `POST /api/mails/:id/feedback` and the MCP `submit_spam_feedback` tool are unaffected. `GET
  /api/rules` is gone; the equivalent read/observability surface is `GET /api/pipeline` (current
  definition) and `GET /api/runs` / `GET /api/mails/:id/runs` (one message's journey and why).
- **The pipeline reacts to message arrival only**, never to a folder move. The previous rules
  engine mapped every message *update* to a `mail.moved` trigger — including the update its own
  `move_to_spam` action had just made, since `origin` distinguishes PostIMAP from a consumer, not
  the classifier's own write from a user's. That made a move-triggered rule one edit away from
  looping on itself. Reacting only to `insert`/`origin=sync` removes the possibility rather than
  guarding against it; a folder move into or out of the junk folder is handled separately (below).
- **Spam feedback is recorded when a folder move contradicts the stored verdict, not by who moved
  it.** `spam/processor.py`'s `SpamEventProcessor` is now `SpamFeedbackListener`, and it no longer
  routes new mail to a verdict pipeline — only a folder-move update reaches it. Two bugs this
  fixes: the classifier's own move-to-junk effect used to log a "moved to spam" correction against
  the verdict it had just written (origin cannot tell "the app itself, right now" from "a user, a
  moment later"); and moving a message from Junk to Trash was recorded as "the classifier was
  wrong", when deleting spam is the ordinary outcome of a junk folder, not a correction. Excluded
  destination is trash; a verdict already agreeing with the move now produces no feedback row
  either way.
- **`VerdictRepository.has_ai_verdict_for_header` is gone**, replaced by
  `has_ai_verdict_for_msg_key(account_id, msg_key, from_addr)` — the never-classify-twice gate now
  keys on the durable identity (msg_key, §`verdicts.msg_key` below) plus sender, closing the same
  Message-ID-forgery bypass the durability index already closes at the database level.
### Added

- **OpenAI as a first-class `SpamAnalyst` provider**, selected the same way as `anthropic`/`fake` via `ai.provider`
- **`ai.reasoning_effort` setting**, validated against the selected provider's supported levels at write time rather than failing on the next inbound message
- **`core/structured_llm.py`:** the one place a classification or enrichment request leaves the process — provider client resolution, strict-schema dispatch, and full-jitter exponential backoff (1s base, 20s cap, 5 attempts) shared by the spam analyst and rule enrichment
- **`core/errors.ProviderUnavailableError`:** a narrow exception for "no API key configured", so callers degrade on exactly that instead of a bare except swallowing a real request bug
- **`core/encryption.py` / `settings/credentials.py`:** AES-256-GCM provider key storage, decrypted fresh on every call rather than cached, so rotating a key or setting `ENCRYPTION_KEY` for the first time takes effect on the next call
- **`verdicts.msg_key`:** the durable identity a verdict is keyed on — the message's RFC
  `Message-ID` header when present, otherwise a hash of its envelope. A message with no header
  used to skip the never-reclassify gate entirely and be reclassified on every resync; the hash
  fallback closes that. `verdicts.from_addr` is recorded alongside it and folded into the same
  partial unique index, so a message forging another's `Message-ID` cannot inherit its verdict.
- **`queue/` package:** a Postgres-native work-queue engine, parameterised by table rather than by
  what it queues — claim with `FOR UPDATE SKIP LOCKED`, a lease reclaimed by an advisory-locked
  reconciliation timer, heartbeat, full-jitter backoff, a persisted named circuit breaker
  (`closed`/`open`/`suspended`, the last requiring an explicit probe to clear), a `NOTIFY`-based
  wakeup with a poll fallback, and a supervisor that reconciles a live worker count without a
  restart. `attempts` increments at claim rather than at failure, so a row that kills its worker

- **`GET/PATCH /api/queues`:** lists and changes a registered queue's state (`running`/`paused`),
  concurrency and batch size. A `PATCH` raising concurrency above what the database connection
  pool can actually support is rejected with `400` rather than silently serialising workers.
- **`queue_state` / `circuit_breakers` tables:** the persisted, restart-surviving half of the
  queue engine's operator state and circuit breaker health.
- **Folder creation and deletion.** `POST /api/accounts/:id/folders` creates a folder, joining onto
  a `parent_id`'s path with the account's own separator when given (IMAP has no parent concept);
  `DELETE /api/folders/:id` deletes one, refused outright for INBOX rather than dead-lettered
  later. Both require PostIMAP service_version >= 1.3.0, checked at request time the same way
  account deletion checks for >= 1.0.1. A small "Manage folders" dialog in the sidebar is the UI:
  create by name (optionally nested under an existing folder), delete with an explicit
  confirmation naming how many messages the deletion destroys on the mail server
- **Reopening and editing a draft.** Clicking a draft now opens it in the composer instead of the
  reading pane, with recipients, subject, body and reply threading restored. Saving or sending
  inserts a new outbox row naming the draft's `messages.id` via `replaces_message_id`, so PostIMAP
  appends the replacement and removes the superseded draft as one operation rather than an
  expunge-then-create with no ordering between them -- sending a reopened draft leaves no draft
  copy behind either. Requires PostIMAP service_version >= 1.4.0
- **`pipeline/` package:** the message pipeline runner. A stage returns declarative *effects*
  rather than writing SQL or a success flag — `MatchStage` (a rule, generalised) and
  `ClassifyStage` (spam/ham, one model call) ship today; a stage that raises `StageMisconfigured`
  fails the run permanently and names itself in `pipeline_runs.failed_stage`, `StageTransient`
  retries with full-jitter backoff, and `StageThrottled`/`StageUnavailable` suspend the run
  without burning one of its attempts and share the same per-provider circuit breaker
  `queue/circuit.py` already provides. Every effect (`Move`, `Trash`, `Expunge`, `SetFlags`,
  `Keywords`, `Tag`, `RecordVerdict`, `Notify`) is applied through a guarded UPDATE — the rowcount
  is the only thing believed, and a guard that fails records `not applied: message gone` in the
  run's trace instead of reporting success for a write that did nothing (`rules/executor.py`'s
  `move_to` handler used to do exactly that). `pipeline_runs` is the `pipeline` queue registered
  with `queue/manager.py`; `pipeline_revisions` is the append-only pipeline definition history,
  current = `max(revision)`.
- **`pipeline/enqueue.py`:** a live run is enqueued on `message`/`insert` with `origin = "sync"`
  only — never on an update, which is what stops the pipeline from ever reacting to its own
  writes. `pipeline_folder_state` records MailVerdict's own watermark (the timestamp a folder's
  first full sync completed, since `folders.initial_sync_done` is a boolean with no timestamp),
  and a reconciliation pass on a shared advisory lock finds live-eligible mail a listener
  reconnect missed. Reconciliation's SQL anti-join is on `message_id`, not the durable `msg_key` —
  computing the header's content-hash fallback in SQL would mean a second, driftable definition of
  the same key `database/msg_key.py` already owns — so the anti-join is a cheap pre-filter, the
  real dedup is `msg_key`'s unique constraint at insert time, and a conflict there (a UIDVALIDITY
  resync assigning a new row to already-run mail) repoints the existing run's `message_id` rather
  than being dropped, which is what makes the anti-join converge on the next pass instead of
  reselecting the same message forever.
- **`GET /api/runs`, `GET /api/runs/:id`, `POST /api/runs/:id/retry`, `GET
  /api/mails/:id/runs`:** the pipeline's observability surface — every run's trace, filterable by
  status/account, and re-queueing a failed one.
- **Semantic search.** Every message gets a `text-embedding-3-small` vector (subject, sender and
  the first 2000 characters of the body, HTML-stripped when there is no plain-text body; envelope
  only when the body was never fetched at all), stored in `message_embeddings` and indexed with
  pgvector's HNSW. `GET /api/embeddings/search` and the MCP tool `semantic_search_mail` find
  messages by meaning rather than exact words, alongside the existing full-text search rather than
  replacing it. `message_embeddings` is a queue in its own right (`queue/`): a backfill sweep is a
  self-advancing set-difference batch rather than a cursor, so it is resumable with no state and
  immune to a UIDVALIDITY resync recreating every message's id — an embedding row is keyed on the
  same durable `(account_id, msg_key)` identity as `verdicts`, joined back to `messages` at read
  time. `GET /api/embeddings/status` (and the MCP tool `get_semantic_status`) report coverage —
  encoded/pending/failed against the currently configured model — so a partial corpus is visible
  rather than inferred from search quietly returning less than expected. A model change is an
  ordinary re-embed, not a migration: old vectors are kept, coverage for the new model starts at
  zero and rises. New `semantic` settings category: `provider`, `model`, `content_chars`,
  `batch_size`, `concurrency`. Registers as the `embeddings` named queue, so start/stop/concurrency
  go through the existing `GET/PATCH /api/queues/embeddings`.
- **Embedding strictly precedes the pipeline.** `pipeline/enqueue.py`'s live-arrival handler now
  enqueues a `message_embeddings` row, never a `pipeline_runs` row directly; only that embedding
  reaching a terminal state — `done` *or* `failed` — opens the pipeline queue for it, in the same
  transaction as the write that moved it there (`EmbeddingRepository.write_result` /
  `.fail`, via `pipeline.enqueue.enqueue_pipeline_run_if_live_eligible`). A message whose embedding
  can never succeed is not stranded: reaching `failed` opens the gate exactly as `done` does, so it
  is still classified, just with no neighbour hints — the classify stage's trace says so.
  Reconciliation's gap-recovery pass now requires the same terminal embedding before enqueuing a
  run, so a listener reconnect cannot bypass the gate either.
- **Neighbour hints for `classify`, off by default** (`settings.semantic.neighbor_hints_enabled`,
  `neighbor_k`, `neighbor_min_similarity`). `pipeline/neighbors.py`'s `NeighborService` finds the
  k nearest past messages carrying a *human* label — an explicit user correction
  (`verdicts.source = 'user_feedback'`), or the folder a message currently sits in — and excludes
  every AI/rule verdict from the pool outright, so the classifier's own past verdicts can never
  become evidence for the next one (a wrong first verdict would otherwise compound: every
  near-identical future message sees a spam neighbour, agrees, and becomes another spam neighbour,
  which reads as consistency rather than the loop it is). Junk-folder membership and inbox
  membership are treated asymmetrically — Junk is strong spam evidence, sitting in the inbox is
  weak not-spam evidence, since a message may simply not have been dealt with yet — both stated
  explicitly in the prompt rather than collapsed into one label. No near-duplicate short-circuit:
  a close match is always still a hint, never a reason to skip the model call.
- **The pipeline configuration API**, `api/pipeline.py` — read/replace the whole definition
  (`GET`/`PUT /api/pipeline`, `PUT` carrying an optional `base_revision` for optimistic
  concurrency, `409` on a stale one), per-stage `POST`/`PATCH`/`DELETE /api/pipeline/stages` and
  `POST /api/pipeline/stages/reorder`, `GET /api/pipeline/stage-types` (each registered type's
  JSON schema, generated from the same Pydantic model `registry.build_stage` validates against, so
  it cannot drift from what a write actually accepts), `GET/POST /api/pipeline/revisions` history
  and restore, `GET /api/pipeline/health` (per-stage folder resolution against every account it
  applies to), and `POST /api/pipeline/test` / `POST /api/pipeline/stages/:id/test` — a dry run of
  the whole pipeline or one stage against an existing message, applying nothing. Validation is
  split on purpose: an unknown stage type, unknown effect, unknown condition type or duplicate
  stage name can never become valid later and are rejected outright (`400`, every problem
  collected at once); a folder reference that does not currently resolve is *accepted*, since
  folders appear asynchronously as PostIMAP discovers them — it is reported as a warning on every
  document response and stays queryable afterwards through the health endpoint.
### Changed

- `RetryConfig.delay_for_attempt` uses full jitter (a uniform draw over `[0, cap]`) instead of a fixed exponential value; default `retry` settings changed to 5 attempts, 1s base, 20s cap
- The `classify` stage builds far more signal into the model call than the old spam analyst did:
  alongside sender/recipient/subject/body it now sends the relationship between the From header
  and everything around it — envelope sender (Return-Path) vs. From, Reply-To vs. From, and
  whether a display name embeds a different address — stated as facts, never pre-judged into a
  score. The model still gets no tools and one call per message.
- **PostIMAP pinned to 1.5.0**, which adds consumer-driven folder creation and deletion, durable sync notifications, per-folder IMAP push, a draft-replace column, and a per-folder backfill total that gives an initial sync a denominator. Insert grants are now column-level rather than table-level, so writing a PostIMAP-managed column is refused rather than silently accepted. The consumer contract version is unchanged, so every addition is additive.
- **PostgreSQL image now ships pgvector** (`pgvector/pgvector:pg18`), in both compose files and the test container. The stock image carries no `vector` extension, and pgvector is not a trusted extension so it cannot be added by an unprivileged role at runtime. Same major version and data directory as before, so an existing volume mounts unchanged. Deployments supplying their own PostgreSQL must provide the extension; on Kubernetes that means a vector-enabled image.
### Fixed
- **Folders that finished syncing before the pipeline existed now classify new mail.** The watermark separating historical mail from live mail is written from the single event that fires when a folder's first sync completes, so a folder already synced never received one — and without it every message arriving there was embedded and then found ineligible, so it was never classified, silently and permanently. The upgrade backfills those folders at upgrade time, which keeps existing mail historical and makes everything arriving afterwards live.
- **Remote content in CSS is blocked too.** Image blocking covered `img src` and the `background` attribute, but a `style` attribute reaches the network through `background-image`, `background`, `list-style-image`, `border-image`, `content` and `cursor` — so a tracking pixel written as CSS loaded on open, defeating the feature against any sender who knows that. Every `url()` is now neutralised rather than a list of properties being enumerated, the rest of the declaration is left intact so layout survives, `cid:` references are untouched, and allowing a sender restores the original without reviving a non-http scheme.
- **Outbox validation:** a malformed field now answers `422` and a supersede target that does not resolve answers `404`. Both previously reached the client as `500` — the body is validated by hand because the endpoint accepts JSON and multipart, so nothing was turning a validation error into a response, and the supersede column carries a foreign key whose violation was uncaught.

- **Verdict durability gate no longer skipped for headerless mail:** the partial unique index
  moved from `(account_id, message_id_hdr)` to `(account_id, msg_key, coalesce(from_addr, ''))`.
- **Contract-owned columns are no longer written.** `outbox.status`, `outbox.attempts` and `accounts.state` are managed by PostIMAP, but the ORM models carried Python-side defaults for them, which SQLAlchemy sends on every insert regardless of what the calling code specifies. The tables carry table-level insert grants, so those writes were accepted rather than refused, and only matched PostIMAP's own initial values by coincidence — a divergence would have left outbox rows the processor never claims, so mail would have stopped sending with nothing reporting an error.
- **Account health:** an account in `error` that has completed a full sync before is shown as `Retrying` rather than as a failure, since PostIMAP retries it unboundedly and it recovers on its own. Only an account that has never once synced is presented as needing attention.
- **Spam prompt:** removed the description of a `neighbors` input that is never sent, so the classifier is no longer instructed to weigh context it does not receive.
- `Outbox.status` and `Outbox.attempts` carried Python-side ORM defaults sent explicitly on every
  INSERT, even though neither column has an INSERT grant under the restricted `postimap_app` role
  -- every other pg-layer test connects as the database owner, where the extra columns are simply
  accepted, so the failure was invisible outside a deployment actually running under the granted
  role. `Folder.id`/`total_count`/`unread_count`/`initial_sync_done` carried the same latent defect,
  unexercised until this release's own folder-insert path. All five now use
  `server_default=FetchedValue()`, the pattern `Outbox.next_retry_at` already used
- **A rule move to a folder that no longer resolves used to succeed silently** (`rules/executor.py`
  logged a warning, returned, and the caller recorded `success=True` anyway). A `match` stage's
  `Move` effect now raises `StageMisconfigured` when its target does not resolve, which fails the
  run permanently with the unresolved reference named in `pipeline_runs.last_error` instead of
  quietly leaving the message unreachable.
- **Building rule/classify context no longer selects whole `Attachment` rows (including the
  `bytea` blob) to find out whether a message has attachments and what kind.** The pipeline's
  `MessageView` loader selects `content_type` only, and the message row itself never selects
  `raw_source`. At pipeline concurrency, the old query was an out-of-memory pod restart waiting to
  happen on any large-attachment mailbox, and it would have looked like anything but its cause.
- **A message with no `Message-ID` header used to be reclassified on every resync.** The old
  never-classify-twice gate checked `msg.message_id and await has_ai_verdict_for_header(...)`, so
  an absent header skipped the check entirely rather than falling through to it. The `classify`
  stage now gates on `verdicts.msg_key` (the header, or a content hash when there is none), which
  is defined and non-null for every message.
- **`body_contains` used to never match an HTML-only message.** The old rule context only
  populated `body_text`; `MessageView` strips `body_html` to text with `nh3` when `body_text` is
  NULL, so a `match` stage's conditions see the same body a newsletter's recipient does.
- **A stage that fails to *construct* — a `match` stage's config naming an unknown effect kind,
  most notably — used to raise an unhandled exception out of `registry.build_stage` instead of
  `StageMisconfigured`.** The pipeline runner has no handler for that, so it fell into the generic
  retry path and was retried up to `max_attempts` times before failing, exactly the noisy,
  hides-the-real-problem behaviour `StageMisconfigured` exists to avoid. `build_stage` now wraps
  construction in the same try/except as config validation.

## [1.0.0] - 2026-08-30

### Breaking Changes

- **PostIMAP integration:** Entire IMAP sync layer (~4,700 LOC) replaced with the PostIMAP microservice — MailVerdict is now a pure PostgreSQL application, targeting PostIMAP's versioned contract (`contract_version` 1, service `1.1.0`)
- **`Mail` → `Message`:** Model renamed, table `mails` → `messages`
- **`uid` → `imap_uid`:** IMAP UID field renamed across entire stack; now nullable, `NULL` meaning an optimistic move is pending (surfaced in the API as `pending_sync`)
- **`is_read` → `is_seen`:** Read state field renamed across entire stack
- **DB-centric architecture:** API layer is now pure DB reads/writes — zero IMAP imports
- **`is_deleted` → `expunged_at`:** Message soft-delete moved from a boolean to a nullable timestamp, matching PostIMAP's own column rename
- **Fresh Alembic migration:** All migration history squashed to a single v1 baseline against an empty database (no upgrade path from pre-contract schemas — PostIMAP re-syncs from IMAP)
- **Folder counts:** Maintained by PostIMAP's own triggers
- **Semantic search removed:** Qdrant, OpenAI embeddings, and the `mode=semantic` search/MCP option are gone; full-text search (`websearch_to_tsquery('simple', ...)`) covers v1
- **LLM provider switched to Anthropic:** `openai` and `qdrant-client` dependencies dropped, `anthropic` added; default model `claude-haiku-4-5`, configurable via the `ai` settings category
- **Credential handling:** MailVerdict no longer encrypts anything — it always writes the contract's plaintext format (`0x00` prefix byte); PostIMAP encrypts credentials itself when its own key is configured. `MAIL_VERDICT_ENCRYPTION_KEY` and the `core/encryption.py` module are gone
- **Owned tables carry no foreign keys onto PostIMAP tables:** the consumer database role has no `REFERENCES` grant, and verdict history must outlive PostIMAP's retention purge of expunged messages
- **Config loader rewritten:** pydantic-validated, `${VAR}` placeholder resolution, arbitrary-depth `MAIL_VERDICT_<SECTION>_<KEY>` env overrides, fail-fast on missing required values (no more silently defaulted `cors_origins`)

### Added

- **`postimap/` package:** the only module that knows the PostIMAP contract — `contract.py` (version handshake), `listener.py` (LISTEN `postimap_events`, typed event parsing, reconnect/keepalive), `commands.py` (`postimap_commands` sync requests), `actions.py` (every contract write in one place)
- **Threading:** `messages.thread_id` is now mirrored and indexed; `GET /api/accounts/:id/messages?threaded=true` returns one row per conversation (latest message, `thread_count`, `unread_in_thread`), and `GET /api/messages/:id/thread` returns every message in the conversation across folders, ascending
- **Attachment streaming:** `GET /api/messages/:id/attachments/:attachment_id` streams from `attachments.data` with content type and a download disposition; inline `cid:` references in message HTML are rewritten to this endpoint
- **Outbox:** `POST /api/outbox` sends or drafts a message (JSON, or multipart when attachments are present) and `GET /api/outbox` lists outbox rows for the send/draft status view — inserting into `outbox` is the only way this application originates mail
- **Bulk actions:** `POST /api/accounts/:id/messages/bulk-action` applies one action to many messages, by an explicit id list or by a server-resolved scope (folder + read/unread filter + exclusions) — replaces the deleted server-side selection state
- **Folder preferences:** `PATCH /api/folders/:id/prefs` consolidates visibility, display name, unified name, and special-use override into the folder's one write surface
- **Read-time HTML sanitization:** `GET /api/messages/:id` now runs the nh3 sanitizer on `body_html` at read time — PostIMAP owns message inserts, so store-time sanitization is no longer possible
- **Search snippets:** `GET /api/search` highlights matches via `ts_headline` against the same coalesced subject/sender/body text the generated `search_vector` column indexes on
- **MCP tools:** `get_thread`, `list_mails`, `mark_mail`, `submit_spam_feedback`, `send_mail`, `draft_mail` — `send_mail` is the first capability letting an MCP client actually send mail through the outbox
- **Passive "moved out of junk" feedback:** `postimap_events` carries `old_folder_id` on a `folder_id`-changing update (PostIMAP 1.1.0); `spam/processor.py` records a correction when a move's source folder was junk and its destination isn't, the same path the explicit not-spam control takes. Only fires for moves made inside this application — a move made in another mail client is an expunge in the source plus a separate insert in the destination, not a `folder_id` change, so it carries no `old_folder_id` and no signal fires; documented in `spam/processor.py`'s module docstring
- **`is_truncated` surfaced:** oversized messages (over `storage.max_message_bytes`) are now distinguishable in the API
- **Verdict durability gate:** partial unique index on `(account_id, message_id_hdr) WHERE source = 'ai'` — an AI verdict is never reissued for the same message header, surviving both retention purge and a UIDVALIDITY resync
- **`FakeSpamAnalyst`:** deterministic, keyword-driven `SpamAnalyst` implementation for tests and API-key-free local development
- **`tests/setup/`:** testcontainers-based test infrastructure — container runtime bootstrap (rootless podman or a standard Docker/DinD socket, fails loudly with the fix command otherwise), pinned image tags, session-scoped Postgres + PostIMAP fixtures
- **`tests/e2e/`:** end-to-end scenarios against the full stack with the application in-process — account onboarding (including the assertion that a first sync of an existing mailbox produces zero verdicts), mail actions with one case asserted all the way onto the real IMAP server, and the compose flow with delivery asserted against a real SMTP sink
- **`tests/pg/`:** integration tests against a real Postgres + PostIMAP — Alembic migrating cleanly next to PostIMAP's own schema, the contract-version gate (pass and mismatch), and `postimap/actions.py`'s SQL round-tripping (move, expunge, flags, the verdict durability gate)
- **CI:** `lint`, `unit`, and `pg` jobs, with a `pg` job running the new testcontainers suite

### Changed

- All mail actions are direct SQL `UPDATE`s through `postimap/actions.py` — PostIMAP's own triggers propagate them to IMAP
- `postimap/listener.py` replaces the old custom `mv_message_notify` trigger — MailVerdict never installs DDL on a table it doesn't own
- Folder-changing actions (`api/mails.py`, `rules/executor.py`, `api/mcp_tools.py`) use the contract's `folder_id + imap_uid = NULL` move instead of a random negative `imap_uid` sentinel
- `verdicts` gained `account_id` and `message_id_hdr`; `create_verdict` now takes `account_id` and drops the removed `neighbor_ids` field
- Search config switched from `'english'` to `'simple'` to match `search_vector`'s own tsvector config
- `account_prefs.folder_mapping` dropped — `folders.special_use`, overridable per-folder via `folder_prefs.special_use_override`, is now the single source of truth
- `folder_prefs.subscribed` dropped (unused concept)
- Account update moved from `PUT` to `PATCH /api/accounts/:id`
- `GET /api/search` drops the per-result `score`/`source` fields and the response's `mode` field (fulltext is the only mode); results carry a `snippet` instead

### Fixed

- Account deletion (`DELETE /api/accounts/:id`) is a real, working `DELETE FROM accounts` — PostIMAP 1.0.1 added the grant, so no interim "disable instead" workaround was needed
- SQLAlchemy's default `Enum` column type persists a Python enum member's `.name`, not its `.value` — a raw-SQL predicate written against the value (like the verdict durability index's `source = 'ai'`) silently never matched. All three enum columns (`VerdictSource`, `TagSource`, `ImageExceptionType`) now explicitly persist `.value`
- Alembic's `env.py` called `asyncio.run()` internally, which fails when migrations are triggered from code already inside a running event loop (the pg-layer test fixtures); the pg-layer harness now offloads the migration call to a worker thread
- `postimap_info`'s actual primary key is `singleton` (a `BOOLEAN CHECK`), not `id` — the mapped model was wrong
- `outbox.next_retry_at` is `NOT NULL` with a server-side default in the real schema; the mapped column had neither, so SQLAlchemy sent an explicit `NULL` on every insert and every send/draft failed with a constraint violation. Caught by a pg-layer test seeding a real outbox row, not by unit tests against a mocked session
- The SSE endpoint answered 404, so every live update was dead. Starlette matches routes in registration order and a `Mount` claims every path beneath it, so registering `/api/events` after mounting `/api` left it unreachable and the interface sat on "Reconnecting..." indefinitely. A test now asserts the ordering
- Postgres 18 requires its volume mounted at `/var/lib/postgresql`, not the pre-18 `/var/lib/postgresql/data`; with the old path it refuses to start and reports the mount as unused, so neither the development nor the production stack came up
- The development app container never started: podman-compose places services in a pod and rejects `userns_mode` alongside it
- The development stack could not sync at all — its throwaway mail server presents a self-signed certificate, which PostIMAP correctly rejects by default. Certificate validation is disabled for development only; production validates
- **The `/mcp` endpoint had no authentication.** The API key is enforced per-router inside the mounted API application, and a mount is an ASGI boundary, so it never ran for the separately mounted MCP application — which exposes reading, searching, moving and sending mail. Now wrapped in ASGI middleware that checks the same key
- Editing an account failed with a permission error on any deployment using the restricted database role: the update path sent `imap_host`, `imap_port` and `imap_user`, which the contract permits only on insert. They are no longer updatable, and the interface says so — changing an IMAP host means removing the account and adding it again
- Readiness latched false permanently when PostIMAP had not yet created its contract row, so a pod installed alongside PostIMAP never became Ready. Readiness now re-checks until confirmed
- The event listener never reconnected once its connection closed. The keepalive skipped its probe on a closed connection instead of treating it as the reason to reconnect, so live updates, classification and rules all stopped silently until the process was restarted. Reconnection now retries with backoff
- Outbox status notifications could never fire: the event carried only the changed column names, never the status itself. The event now carries the row's current status and kind
- `mail.updated` refreshed nothing, because the event names the row `id` and the client read `message_id`. Open messages and conversations now refresh, and the conversation view is invalidated alongside the message
- `folder_prefs.special_use_override` was applied when listing folders but ignored when resolving trash, archive and junk, so on servers that do not advertise SPECIAL-USE the folder tree looked correct while every action failed. `account_prefs.spam_enabled` was documented as a gate but never read, leaving the per-account toggle inert
- The keyword-based analyst existed but nothing could select it, so spam classification required a live API key. The `ai` settings category now takes a `provider` of `anthropic` or `fake`
- An open server-sent-events stream blocked graceful shutdown indefinitely, so a process with a browser attached never exited on its own. Shutdown is now bounded
- `pytest -m pg` and `-m e2e` selected nothing: a `pytestmark` in a directory's own conftest does not apply to sibling modules, so the marker the docstrings described was never actually attached. Tests are now marked by the directory they live in

### Removed

- `sync/` directory (14 files, ~4,700 LOC) — replaced by PostIMAP microservice
- `jobs/` directory (3 files) — PostIMAP manages account state
- `sync_utils.py`, `folder_utils.py` — no longer needed
- `SyncQueue`, `SyncAudit`, `JobState` models
- `imap-tools` dependency
- Direct IMAP connection management (SyncEngine, SyncManager, SyncConnector)
- IDLE watchers (`sync/idle.py`), sync trackers (`sync/tracker.py`)
- OutboundProcessor (`sync/outbound.py`), ActionPropagator (`sync/actions.py`)
- `jobs.py` API endpoint (job state managed by PostIMAP)
- `semantic/` package, Qdrant service (compose + config), `core/openai_provider.py`
- `core/encryption.py`, `core/jsonb.py` (PostIMAP's jsonb columns are native jsonb now, no double-encoding to work around)
- `database/pg_listener.py` (replaced by `postimap/listener.py`)
- `api/selection.py` and its schemas (server-side per-account selection state; client-held selection + a bulk-action endpoint replaces it), `api/jmap.py` (placeholder)
- `devenv/` CLI, `docker/stalwart-*.toml`, `compose.test.yaml`, the Stalwart-based `tests/e2e/` suite and its container manager — replaced by the testcontainers-based `tests/setup/` and `tests/pg/`
- Committed E2E screenshots and result artifacts
- `POST /api/accounts/:id/test-connection` — there are no IMAP imports in this codebase; `state`/`state_error` on the account row is the connectivity truth surface
- `GET`/`PUT /api/accounts/:id/folder-mapping` and `POST .../folder-mapping/auto-detect` — `folders.special_use` plus the per-folder override was already the single source of truth these read from
- `PATCH .../folders/:id/visibility` and `PUT .../folders/:id/unified-name` — folded into `PATCH /api/folders/:id/prefs`

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
