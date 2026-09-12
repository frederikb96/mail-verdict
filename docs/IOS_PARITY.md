# iOS parity

[mail-verdict-ios](https://github.com/frederikb96/mail-verdict-ios) is a native iPhone client for
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
  [`mail-verdict-ios/PORTING.md`](https://github.com/frederikb96/mail-verdict-ios/blob/main/PORTING.md).

**One exception to the deferral above, always**: a field, an enum case or a `CodingKey` is never
deferred. It costs minutes and compiles on Linux; clients drift because these get deferred anyway.
Only a view, a screen flow, or something needing Apple hardware to verify belongs in the backlog.

## Status vocabulary

- **ported** — the app has this, verified against the current shape.
- **partial** — the app has some of this; the gap is either fixed now (see the exception above)
  or named in `PORTING.md`.
- **absent** — the app should have this and does not yet.
- **n/a** — the app will never have this. A route existing is not itself a reason to port it.

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
| API contract | `docs/api-contract/openapi.json` | absent | `4e14a6f` | The app's contract tests diff every model's `CodingKeys` against this document and decode a minimal instance of each schema. |
| SSE events | `docs/api-contract/sse-events.json`; payloads in `docs/api.md`, `docs/architecture.md` | absent | `4e14a6f` | Names are checked both ways by the app's contract tests. Payloads are untyped on the server and mirrored by hand. The web's event-to-effect table (`hooks/use-sse.ts`) is the list the app mirrors; `pipeline.run_finished`, `pipeline.notify`, `pipeline.document_changed` and the `calendar.*`/`contact.*` events are explicit no-ops on the phone. |
| Push envelope | `src/mail_verdict/push/envelope.py`, `tests/fixtures/push_envelope_v1.json` | absent | `4e14a6f` | The app's `PushEnvelope` opens what the server seals; its tests use the fixture vector byte for byte. |
| Alerts, badge and native push | `src/mail_verdict/alerts/`, `src/mail_verdict/api/alerts.py`, `src/mail_verdict/api/notifications.py`; web `components/layout/notification-bell.tsx`, `hooks/use-alerts.ts`, `hooks/use-notifications.ts`, `hooks/use-push.ts`, `components/settings/alert-settings.tsx` | absent | `4e14a6f` | |
| Mail list | `components/mail/mail-list.tsx`, `components/mail/mail-list-item.tsx`, `components/mail/bulk-panel.tsx`, `hooks/use-mails.ts`, `lib/mail-list-window.ts` | absent | `4e14a6f` | |
| Thread and reader | `components/mail/reading-pane.tsx`, `components/mail/thread-message.tsx`, `components/mail/email-renderer.tsx` | absent | `4e14a6f` | Message HTML is sanitised server-side; the reader needs no server change. |
| Composer and outbox | `components/mail/compose-form.tsx`, `components/mail/reply-box.tsx`, `components/mail/draft-editor.tsx`, `components/mail/undo-send-banner.tsx`, `components/contacts/recipient-field.tsx` | absent | `4e14a6f` | Contacts reach the app only as recipient autocomplete. |
| Search | `components/search/search-page.tsx` | absent | `4e14a6f` | |
| Spam review | `components/mail/spam-review-page.tsx` | absent | `4e14a6f` | |
| Folders and unified views | `components/layout/app-sidebar.tsx`, `components/sidebar/`, `hooks/use-unified-view.ts` | absent | `4e14a6f` | |
| Accounts | `components/accounts/accounts-page.tsx`, `components/accounts/identities-section.tsx` | absent | `4e14a6f` | |
| Settings subset | `components/settings/settings-page.tsx` | absent | `4e14a6f` | |
| Settings categories | `src/mail_verdict/settings/defaults.py` | absent | `4e14a6f` | Manual: `GET /api/settings/{category}` returns untyped objects the contract snapshot cannot describe. The web renders them with a generic type-introspecting form. |
| Calendar | `components/calendar/` | n/a | `4e14a6f` | |
| Contacts screens | `components/contacts/` | n/a | `4e14a6f` | Except the recipient field, under Composer. |
| AI pipeline and admin | `app/pipeline/`, `components/settings/settings-page.tsx` (AI & automation) | n/a | `4e14a6f` | |
| Web Push | `public/sw.js`, `hooks/use-push.ts` (the VAPID half) | n/a | `4e14a6f` | Native push replaces it on the phone. |
| Default mail app, `mailto:` | `components/layout/protocol-handler.tsx` | n/a | `4e14a6f` | Needs Apple's mail-client entitlement. |
