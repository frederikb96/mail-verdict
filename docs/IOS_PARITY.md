# iOS parity

[mail-verdict-ios](https://github.com/frederikberg/mail-verdict-ios) is a native iPhone client for
this server. It mirrors two things this repository owns: the API contract (hand-written Swift
models, checked in its own tests against [`api-contract/`](api-contract/)) and the web UI's
behaviour, which is the reference for what the app does. Nothing regenerates the Swift side. This
file is how a change here reaches a decision about the phone without anyone reading the app's code
first.

It lives in the tracked tree so it is present in every checkout and every worktree, at the moment
someone is editing one of the files it names.

## Does this concern iOS

Answer in order, stop at the first match.

- Did the change touch any of these? **No** → stop, nothing below applies.
  - `src/mail_verdict/api/schemas.py`
  - a route in `src/mail_verdict/api/*.py`
  - `docs/api-contract/*`
  - `SSE_EVENT_TYPES` or an SSE payload
  - `src/mail_verdict/push/envelope.py`
  - a `settings/defaults.py` key the app exposes
  - a screen flow under `ui/src/app/` or `ui/src/components/` in the areas the app covers: mail
    list, thread and reader, composer, search, folders, accounts, settings
- Look the area up in the table below. **Absent from the table, or marked `n/a`** → stop, it was
  already decided this stays web-only.
- **Marked `ported` or `partial`** → does completing or maintaining the port need a file under
  the app target (`MailVerdict/` — views, or anything needing an Apple framework)? **No** → port it
  now, in `MailVerdictKit`, which builds and tests on Linux. **Yes** → append it to
  [`mail-verdict-ios/PORTING.md`](https://github.com/frederikberg/mail-verdict-ios/blob/main/PORTING.md).

**One exception to the deferral above, always**: a field, an enum case or a `CodingKey` is never
deferred. It costs minutes and compiles on Linux; clients drift because these get deferred anyway.
Only a view, a screen flow, or something needing Apple hardware to verify belongs in the backlog.

## Status vocabulary

- **ported** — the app has this, verified against the current shape.
- **partial** — the app has some of this; the gap is either fixed now (see the exception above)
  or named in `PORTING.md`.
- **absent** — the app should have this and does not yet.
- **n/a** — the app will never have this. A route existing is not itself a reason to port it.

A `ported` or `partial` row can still have work that only a device can verify — a gesture, a real
keyboard, a process relaunch. That backlog lives in
[mail-verdict-ios's `PORTING.md`](https://github.com/frederikberg/mail-verdict-ios/blob/main/PORTING.md),
not here.

## Verifying a row is still current

Every row names a `Synced at` sha — the commit of this repository its status was last checked
against. Check it with:

```bash
git log <synced-at-sha>..origin/main -- <anchor paths>
```

Nothing printed means the row is current. Commits printed name what to re-check; update the row's
status and sha once resolved.

## Parity table

Web anchors are relative to `ui/src/`.

| Area | Anchor | Status | Synced at | Notes |
|---|---|---|---|---|
| API contract | `docs/api-contract/openapi.json` | ported | `2e1eff6` | The app's contract tests diff every model's `CodingKeys` against this document and decode a minimal instance of each schema, on a schedule. |
| SSE events | `docs/api-contract/sse-events.json`; payloads in `docs/api.md`, `docs/architecture.md` | ported | `c4c6c69` | Names are checked both ways by the app's contract tests. Payloads are untyped on the server and mirrored by hand. A stream carries only the caller's own events; `alert.new` has `account_id: null` for an alert about the instance itself (admins only), and the app answers it by refetching like any other. The web's event-to-effect table (`hooks/use-sse.ts`) is the list the app mirrors; `pipeline.run_finished`, `pipeline.notify`, `pipeline.document_changed` and the `calendar.*`/`contact.*` events are explicit no-ops on the phone. `mail.updated` for a move carries `old_folder_id`, which the web uses to refresh only the lists showing either folder. The idle `ping` event is not in the list: both clients treat it as proof the stream is alive, the web also reconnects when a stream that has pinged goes silent. Both clients skip the event announcing a change their own answered action made, since the re-read after the answer covers it. |
| Sign-in and identity | `src/mail_verdict/auth/`, `src/mail_verdict/api/me.py`, `src/mail_verdict/api/auth_config.py`, `src/mail_verdict/api/sign_ins.py`; web `components/settings/categories/sign-ins.tsx` | absent | `46c44fb` | The server validates an OIDC token on every request. The app needs a sign-in from `GET /api/auth/config` (authorization code with PKCE), `MeResponse` and `AuthConfigResponse`, and the status rules: `401` means sign in again, `403` with detail `user_disabled` signs out too, any other `403` is an error message about that one request. Every request names the phone with `X-Device-Name` (at most 64 characters), the name its sign-in is listed under; a revoked sign-in is `401` with detail `device_revoked` (the app's "This device was signed out"), and a native token without a Zitadel session in its `jti` is `401` `device_unrecognised`. The access token must be short-lived: one issued for longer than the server's `auth.max_token_lifetime_seconds` is a `401`. The list of one's own sign-ins (`GET /api/me/sign-ins`, `SignInResponse`, `POST /api/me/sign-ins/{id}/revoke`) is a Settings screen on the web; on the phone the model is a field-level port and the screen a `PORTING.md` entry. |
| Admin: users and ownership | `src/mail_verdict/api/admin.py` | n/a | `46c44fb` | Admin screens stay on the web; the phone only needs `MeResponse.role` to hide admin settings. |
| Push envelope | `src/mail_verdict/push/envelope.py`, `tests/fixtures/push_envelope_v1.json` | ported | `6bc157c` | The app's `PushEnvelope` opens what the server seals; its tests use the fixture vector byte for byte. |
| Alerts, badge and native push | `src/mail_verdict/alerts/`, `src/mail_verdict/api/alerts.py`, `src/mail_verdict/api/notifications.py`; web `components/layout/notification-bell.tsx`, `hooks/use-alerts.ts`, `hooks/use-notifications.ts`, `hooks/use-push.ts`, `components/settings/categories/notifications.tsx` | partial | `c4c6c69` | Alerts and devices are per user on the server. `PushSubscriptionResponse.transport` is `webpush` or `native` (a phone, iPhone or Android; formerly `apns`): the app's `Transport` enum needs the `native` case, a field-level port that is never deferred. A new admin-only alert `kind`, `account_unowned`, arrives through the list and push like `provider_unavailable`. The gap: the app still registers at `https://mailverdict-push.frederikberg.net` without naming its app, which the shared relay accepts as `mailverdict`; it should register at `https://push.frederikberg.net` with `app: "mailverdict"` (the relay's `POST /v1/register`), a `MailVerdictKit` change. End-to-end delivery to a locked phone, and banners withdrawn or cleared by a later push, are device-only checks. |
| Mail list | `components/mail/mail-list.tsx`, `components/mail/mail-list-item.tsx`, `components/mail/bulk-panel.tsx`, `hooks/use-mails.ts`, `lib/mail-list-window.ts`, `lib/mail-intents.ts`, `lib/intent-ledger.ts`, `lib/intent-drainer.ts`, `hooks/use-mail-intents.ts` | partial | `6bc157c` | The web takes every mail action through a persisted intent ledger projected over lists, reader and counts, sends it with `idempotency_key` and the folder guards (`expected_folder_id`, `expected_folder_ids`, and the list page's `as_of` as `expand_threads_through`), and undoes it guarded to `folder_id` / `target_folder_id` (architecture.md, "Acting on mail from the browser"). The fields are never deferred; the ledger, held/pending/failed states, resending an unanswered action before undoing or discarding it, rulings that move nothing, and undo are the app's to mirror. |
| Thread and reader | `components/mail/reading-pane.tsx`, `components/mail/thread-message.tsx`, `components/mail/email-renderer.tsx` | ported | `d432253` | Message HTML is sanitised server-side; the reader needs no server change. Calendar invitations render with Accept, Tentative and Decline. A tapped or detected phone number hands off to Phone or Messages. `MessageDetail.isGlacier`/`originFolderName` are modeled, `MessageActionSet` hides the spam toggle on a glaciered message, and the provenance banner and refused-action wording are ported; a real device pass is still outstanding (mail-verdict-ios `PORTING.md`). |
| Composer and outbox | `components/mail/compose-form.tsx`, `components/mail/reply-box.tsx`, `components/mail/draft-editor.tsx`, `components/mail/undo-send-banner.tsx`, `components/contacts/recipient-field.tsx` | ported | `6bc157c` | Contacts reach the app only as recipient autocomplete. |
| Search | `components/search/search-page.tsx` | partial | `36ac3f4` | Every mode and filter chip is ported. The web's time picker (presets, a slider overlay over the last 52 weeks, a custom day range) is the reference; semantic results arrive in tiers (`tier` 1 then 2, a "Less likely" divider before the first tier 2). In semantic mode the text search runs too and shows at once, muted, under a static "Searching by meaning…" line until the tiered list replaces it; a `503` (`semantic_unavailable`) from the semantic endpoint keeps the text list, un-muted, with a muted "Semantic search is unavailable right now" line. Every search field waits 300 ms after typing, and a time-window change with the same text re-fires the semantic search only (the server keeps the query vector). mail-verdict-ios implements all of this ([#39](https://github.com/frederikberg/mail-verdict-ios/pull/39)). |
| Spam review | `components/mail/spam-review-page.tsx` | ported | `6bc157c` | |
| Folders and unified views | `components/layout/app-sidebar.tsx`, `components/sidebar/`, `hooks/use-unified-view.ts` | partial | `ff870f3` | `pipeline_enabled` on `FolderResponse`, `FolderOrderItem` and `FolderPrefsUpdate` (whether a folder's mail is classified and embedded) is not modeled by the app yet; a field is never deferred, so adding it to the Swift models is the open item. When no custom order is saved, the app gives Inbox, Drafts, Sent, Archive, Junk and Trash a fixed lead position ahead of the alphabetical rest, matching the web. `FolderResponse.kind`/`FolderOrderItem.kind` are modeled; the Mailboxes sidebar row and MovePicker target for the glacier are ported, needing a device pass (mail-verdict-ios `PORTING.md`). |
| Accounts | `components/accounts/accounts-page.tsx`, `components/accounts/identities-section.tsx`, `lib/account-presets.ts` | partial | `ff870f3` | The add-account form's Gmail and iCloud presets (`lib/account-presets.ts`: servers, sign-in names, app-password pages) and the per-folder AI switch in Folder Order & Visibility are not in the app yet; the presets are a screen flow and go to `PORTING.md`. Folder Order & Visibility and Account Order's drag reorder are device-only checks. `glacierEnabled`/`glacierFolderId`/`glacierAutoDays` are modeled with the same leave/clear/set request shape the two retention fields use, and the account form's switch and days field are ported (mail-verdict-ios `PORTING.md`). |
| Settings subset | `components/settings/categories/`, `components/settings/settings-shell.tsx` | partial | `a1f5cdb` | Each AI tab carries a one-line note on which mail its task reads; that is ported. The administrator's Providers & keys category (the provider keys, the health of every task's breaker, from `GET /api/settings/providers`) and the usage export under System are not: the phone has no screen for that route. |
| Settings categories | `src/mail_verdict/settings/defaults.py` | partial | `a1f5cdb` | Manual: `GET /api/settings/{category}` returns untyped objects the contract snapshot cannot describe. The web renders them with a generic type-introspecting form; the app mirrors it with a generic SwiftUI form. The `assistant` category, `orders`' `decide_*`/`write_*`/`read_*` keys (with `read_below_confidence`) and `semantic.query_instructions` have their labels and category entries. A user who is not an admin reads and writes only the keys `GET /api/settings/schema` tags `user`, and an admin category answers `403` for them, so the category list hides the admin ones from `MeResponse.role` (the app needs the schema models and that gating). |
| Calendar | `components/calendar/` | n/a | `6bc157c` | This row is the standalone calendar screens (agenda, event editing), which the app does not have. The calendar invitation card that appears inside a message is ported, under Thread and reader above. |
| Contacts screens | `components/contacts/` | n/a | `6bc157c` | Except the recipient field, under Composer. |
| Rule assistant | `src/mail_verdict/api/pipeline.py` (`POST /pipeline/assistant`), `src/mail_verdict/rules/assistant.py`; web `components/mail/add-rule-dialog.tsx`, entry in `components/mail/reading-pane.tsx` | ported | `fdc1355` | One sentence about the open mail becomes one proposed change to the rules, touching any number of rules (added, changed, moved, removed); Accept is an ordinary `PUT /pipeline` with the proposal's `enabled`, `stages` and `base_revision`. Every request is independent: nothing is stored between them. Options-menu item, a sheet listing each affected rule with its line diff (Now and Proposed text from a server without one), the preview line, Accept and Decline through the document route, 409 handling, and the instruction kept editable after any answer with Run again sending it as a new request are ported; device-only checks are in mail-verdict-ios `PORTING.md`. |
| AI pipeline and admin | `app/pipeline/`, `app/admin/`, `components/pipeline/processing-page.tsx`, `components/settings/categories/admin-*.tsx`, `components/settings/secrets-card.tsx`; `src/mail_verdict/api/secrets.py`, `src/mail_verdict/api/webhooks.py` | n/a | `d610c80` | No admin or pipeline controls on the phone: secrets, webhook deliveries, the Users page and the queue tiles have no screen there. A pipeline document is per user (`GET /api/pipeline` answers the caller's own), and `GET /api/admin/pipeline/overview` is admin-only. The one visible effect is new alert `kind`s, `webhook_failed` and `provider_unavailable`, arriving through the alert list and push like `outbox_stalled`. |
| Rules page | `app/rules/`, `components/rules/`, `lib/rule-summary.ts`, `lib/rule-form.ts`; `src/mail_verdict/api/pipeline.py` | absent | `608a9a6` | The caller's own pipeline document as a list: Spam, Filing, Forwarding and Orders & tickets groups, each rule a one-line sentence from `describeRule` (`lib/rule-summary.ts`: every condition and effect key, fixed sentences for the classify and orders steps; port it with the same cases), a switch, move up or down, delete behind a confirm, a warning from `GET /pipeline/health`, pause all (`PUT /pipeline` with `enabled`), history, and per-account spam and orders switches (`PATCH /accounts/{id}`). The form editor is web-only for now: a phone edits the sentence through the assistant or the JSON of a rule, and a rule the form cannot show opens as JSON on the web too. |
| Web Push | `public/sw.js`, `hooks/use-push.ts` (the VAPID half) | n/a | `6bc157c` | Native push replaces it on the phone. |
| Default mail app, `mailto:` | `components/layout/protocol-handler.tsx` | n/a | `6bc157c` | Needs Apple's mail-client entitlement. |
| Keyboard shortcuts | `lib/shortcuts.ts`, `lib/shortcut-bindings.ts`, `app/shortcuts/`, `app/settings/shortcuts/`, `components/settings/shortcuts-page.tsx`, `components/layout/shortcuts-overlay.tsx` | n/a | `56421da` | Bindings for a hardware keyboard, stored per browser; the phone has no keyboard to configure. |
| Orders & tickets | `src/mail_verdict/api/orders.py`, `docs/api-contract/*`; web `app/orders/`, `components/orders/`, `hooks/use-orders.ts` | partial | `a1f5cdb` | The list, detail, removing a mail from its order, rewriting a summary, deleting an order and the "Look through recent mail…" catch-up are ported, and so is the wallet: the kind filter (All, Orders, Tickets) with Open and Favorites views, the tickets timeline around today with its Today button (`sort=event`, `timeline_date`), key facts with tracking links, fullscreen codes (`GET /api/orders/{id}/codes/{fact_id}.png`), the documents list (`download_url`, `method`, `barcode_count`), adding a file (`POST /api/orders/files`, polled at `GET /api/orders/files/{file_id}`; the server accepts any type and stores one it cannot read with method `none`) and deleting one, the kind override in `PATCH`, and an offline copy of upcoming tickets. Favorites, the filter field, sealing, and closing or reopening by hand are ported: the row's long-press menu and the detail's Options menu share one action list, and a row swipes from the left to close or reopen and from the right to favorite. Moving a mail to another order and merging two orders are not ported, so neither menu offers them. Everything that needs a device to verify (the timeline's centring, code brightness, offline mode, the file pickers, the gestures) is in mail-verdict-ios `PORTING.md`. |
