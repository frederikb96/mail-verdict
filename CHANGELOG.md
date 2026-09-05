# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Search can be scoped to exactly the folders and fields you mean, and remembers the choice --
  the query text included, so opening a result and pressing Back returns to the same search.
  Opening it offers a folder picker (every folder selected by default, with a select-all; there
  is deliberately no way to deselect down to zero, which would search unscoped rather than
  nothing) and toggles for subject/from/to/body -- both enforced by the query itself, not filtered
  afterward, so a scoped search stays scoped across every page. Matching is fuzzy rather than a
  plain substring test (a typo still finds the message; pg_trgm's `word_similarity`, left out for
  an `@` token since two unrelated addresses sharing a domain score higher on it than a genuine
  typo does), results are always newest first with no sort control, and the list virtualizes and
  pages as you scroll rather than loading a mailbox's worth of results up front. A switch turns on
  semantic search instead -- the folder picker stays, the field toggles disappear, since there's
  one embedding per message and nothing to scope by field
- The compose dialog, reply box and draft editor share a rich-text editor: a small toolbar plus
  Markdown input rules, so typing `# ` makes a heading and `**bold**` makes bold while typing.
  Pasted formatted content keeps its formatting instead of arriving as literal HTML source, and a
  reply or forward embeds the original message as a collapsible, correctly rendered quote rather
  than a lossy line-by-line text dump. The editor scrolls inside its own bounded box instead of
  growing the dialog or the reply box without limit, and every composer surface now has a close
  control that asks whether to save a draft or discard when there is unsaved work -- previously
  the reply box had no way out at all, and the compose dialog's own close discarded silently
- Compose can send an HTML body alongside the plain-text one: `POST /api/outbox` sanitises
  `body_html` for safe sending (a small, mail-client-safe tag vocabulary; no class or style
  attribute survives from the input) before it reaches the outbox row, and requires `body_text`
  alongside it. `GET /api/messages/:id/quote` turns a message's raw body into the shape the
  composer renders locally for a reply, forward or reopened draft, for sending -- a `cid:` or
  other locally-meaningful reference is dropped rather than left broken, and a remote image is
  rewritten to the same privacy placeholder the reading pane uses (restored only once its sender
  is allowlisted), so quoting a message never fetches its images without consent. Sending
  restores whatever placeholder remains, so the quote a recipient sees still carries the original
  image regardless of this account's own allowlist
- Replying now sends from whichever of the account's identities the original message was
  addressed to, rather than always the starred default; a fresh compose still uses the default. A
  sending identity is selectable in the composer whenever an account has more than one
- `tests/setup/large_mailbox.py`: bulk-seeds a mailbox of any size directly into the messages
  mirror, for tests that need mailbox scale (virtualized scrolling, bulk selection) rather than
  mail content. Opt-in -- nothing runs it unless a test calls it -- and fast enough to be usable:
  a thousand messages in about 1.5s, fourteen thousand in 25-30s, one bulk INSERT rather than a
  loop over individual round trips
- `GET /api/accounts/{account_id}/messages/selection`: mints a "select all matching" snapshot --
  an instant and a count from one statement, so the two can never disagree. A bulk action's
  `scope` now carries that instant back as `snapshot_at` (required), so mail arriving after a
  selection was agreed to is never swept into a destructive action the user never saw
- A bulk action may now name `ids` and `scope` together, acting on their union -- a predicate
  selection plus a row outside it the user ticked by hand. Naming the same id in both `ids` and
  `scope.exclude_ids` is rejected rather than picking a winner silently
- Message list rows carry `mirrored_at` (when the row entered the local mirror), the field a
  selection snapshot compares against
- A mail row's hover controls now float over the row instead of reserving space in its layout --
  the sender, subject and preview keep the row's full width whether or not the pointer is
  anywhere near it. Keyboard focus on a row reveals the same controls
- Selecting mail is now a predicate (a "select all matching" scope, minted server-side) plus
  explicit ids layered on top of it, rather than only an enumerated id set -- checking a folder's
  checkbox no longer requires loading every message in it first, and a selection stays exact
  across a scroll that unmounts and remounts rows. Shift-click extends a range from the last
  plain or ctrl-click, ctrl/cmd-click toggles a single row, and a plain click on a row's text
  abandons the selection and opens that message
- With more than one message selected, the reading pane is replaced by a bulk panel offering
  read/unread, star, archive, junk, trash and move-to-folder; a destructive action against a
  "select all" scope confirms with the count first, since it cannot be undone the way an explicit
  selection's toast can
- A row's Junk control now moves the message to the Junk folder unconditionally; correcting the
  model's spam verdict is its own control, since the two are different actions that happened to
  share one icon before
- Hovering a folder in the sidebar replaces its unread count with a menu offering mark-all-read
  and emptying the folder; emptying confirms with the message count first, since it destroys
  every message in it on the server with no undo. Renaming is deliberately not offered -- IMAP's
  rename also renames every child folder, so it cannot be a single-row update
- The reading pane's own action row now also carries star, download as .eml, mark read/unread
  and a verdict-correction control alongside archive, junk and trash
- Dragging a multi-message selection onto a sidebar folder now moves the whole selection in one
  request rather than looping a move per message
- A row action in threaded mode names its scope (the thread's latest message) in its tooltip,
  rather than leaving it to guess at now that bulk selection sits beside it
- A predicate-based bulk action ("select all") or a folder-wide menu action shows a heads-up that
  it may take a while on a large folder -- the write itself is one statement over however many
  rows match, resolved server-side before the request returns
- A provider API key can now be entered, replaced and cleared from Settings -- a masked field per
  provider with its own Save and Clear, rather than only through the API directly
- A message can be switched to a dark canvas from a toggle in its header, remembered per message
  across reopening it

### Changed

- A contact carries a list of websites rather than a single one. `url` on a contact is now `urls`
  in the API, the MCP tools and the response schema — a breaking change for anything reading that
  field
- Settings groups into labelled sections -- Appearance, Mail, Calendar, and AI & automation --
  instead of one unlabelled scroll of cards. Everything calendar-related lives together (the
  invitations panel and the event-duration setting used to sit on opposite sides of the page);
  the AI & automation tabs are AI, Semantic search, Retry and Pipeline, matching the categories
  the server actually has, with a note pointing at the pipeline page for spam detection and rules
- The calendar invitations panel replaces its one-column-per-linked-calendar table with a chip
  picker per identity, so the number of calendars in an account no longer decides the width of
  the page

### Removed

- `GET /api/health/live`. Liveness is answered by a plain socket on its own port instead, so a
  deployment whose probe names that path must drop it and let the chart's own liveness probe
  apply -- otherwise the probe fails every check after upgrading and the pod restart-loops

### Fixed

- The mail list holds the reader's scroll position when a message leaves the folder from
  somewhere above them (a classification run, a rule, a drag) while they are scrolled deep in --
  previously only mail arriving at the very top was compensated, so any other row count change
  above the reader tugged the view by roughly one row
- A bulk action spanning accounts in the unified view now acts on every account the selection
  touches, not silently on whichever one the interface happened to have selected
- An SSE-driven mail list refresh patches the affected row(s) directly instead of invalidating
  and refetching every already-loaded page -- a folder scrolled deep no longer turns one arriving
  or changed message into hundreds of requests
- Live-update events (a message arriving, changing or leaving a folder) are collected and applied
  in bounded batches rather than one at a time -- a bulk action fires one such event per affected
  row, so a whole-folder action no longer turns a single click into thousands of individual
  requests. A bulk action's own completion refresh now resets the affected list instead of
  replaying every page it had loaded, for the same reason
- A bulk mark-read/unmark-flag no longer reports every requested message as affected when some
  already carried the requested flag -- only rows that actually changed count
- A message's `<title>`, `<head>`, or similar document-structure markup no longer renders as
  visible copy at the top of the message. Stripping such a tag kept its text and hoisted it into
  the body -- correct for a stray inline tag, wrong for one that was never meant to be read
- An image with no sizing of its own, or one declaring an implausible size, is now bounded to the
  reading pane in both dimensions rather than only the one a plain `max-width` already caught
- The navigation rail has a Mail entry between Search and Calendar, so there is a way back to the
  mail view from anywhere else in the application
- The account/folder tree in the sidebar only renders on the mail view. It previously rendered
  underneath the contacts view's own list panel as well, where it served no purpose
- Opening a contact whose birthday is a partial or missing date (a year-less birthday is a real
  vCard shape, not a malformed one) no longer crashes the whole page. The card now renders
  everything it can and stays quiet about a birthday it cannot confidently parse
- The liveness probe answers from a plain background socket on its own port, independent of the
  application's event loop. A handler that blocks that loop (heavy concurrent load, a bug) no
  longer risks the pod being restarted for merely being busy — only a genuinely dead or hung
  process fails it now
- Readiness (`/api/health`) checks the database again, and reports what it saw. A pod that had
  lost its database entirely -- a rotated password, Postgres down, a pool permanently exhausted --
  stayed Ready and kept taking traffic it could only answer with errors. The check is bounded by
  `server.readiness_timeout_seconds`, and a timeout counts as busy rather than broken, so a merely
  loaded pod stays in the Service; only an explicit failure takes it out. The response carries a
  `database` field naming which of the three cases applied
- The calendar month view no longer costs several seconds per request regardless of how much it
  returns, and no longer holds up every other request while it works. A long-running recurring
  series (a daily reminder set up years ago is all it takes) was re-walked from its own start
  once per probe by the window-narrowing guard against pathological expansion, multiplying an
  ordinary series' cost by the number of probes; a single bounded call now pays that walk once.
  Expanding every visible calendar's recurring series is also run off the event loop, so a slow
  month view no longer starves every other request sharing the process — including the
  liveness check, which is what pulled the pod out of service
- Unchecking a calendar in the sidebar actually hides its events now, and checking one shows
  them again immediately. The checkbox itself already wrote the change; nothing told the events
  query to refetch under the new visibility, so the previously-cached month kept rendering as if
  nothing had happened
- The sidebar's calendar checkbox is tinted with the calendar's own colour rather than always
  the theme's primary colour — it renders as a plain button, not a native input, so `accent-color`
  never had any effect on it
- The manage-calendars dialog is a compact list instead of every calendar permanently showing
  all twelve palette colours as a row of swatches, which pushed the name into an ellipsis and
  forced the dialog to scroll sideways as well as down. Each calendar now has a single colour
  swatch that opens the palette in a popover
- A calendar can now be hidden from the sidebar and the event editor entirely (in the manage
  dialog), independent of the sidebar's own per-view visibility checkbox — two levels rather
  than one flag serving both
- The event editor's Calendar picker offers only enabled calendars, so it is not as long as the
  full list a deployment with many synced collections would otherwise show
- The week view's header shows the week number in brackets after the date range, the way the
  month view's own gutter already does
- Collapsing the sidebar rail on the calendar route no longer squeezes the mini-month grid and
  the per-calendar checkbox list into the icon-width rail, where the month's rows overlapped and
  the checkboxes lost their labels. That content now hides while the rail is collapsed and
  reappears immediately on expanding it
- A settings category the interface expects but the server didn't return now explains that the
  interface and the server may disagree on which categories exist, rather than saying nothing
  more than "No settings available for this category"
- Reopening a saved reply or forward draft carries its plain-text quote forward again -- it
  previously came back empty, so the two MIME parts of a resaved draft disagreed, and sending an
  untouched draft (nothing retyped) could fail outright since the text part had nothing in it at
  all while the HTML part still carried the quote
- A table pasted into the composer flattens to one paragraph per row with its cells kept apart,
  rather than run together with nothing between them
- The composer's To/Cc/Bcc field keeps its accessible name once it holds a chip -- its visible
  placeholder disappears at that point (correctly, next to a chip it would read oddly), which
  previously took the field's only name with it
- Double-clicking Send no longer queues the same message twice. The button's own disabled state
  was reacting to react-query's isPending a render late, which two clicks landing before that
  render both slipped past; the submission itself is now guarded directly
- Trashing (or archiving, marking spam, or otherwise moving out of its folder) any message in the
  conversation a reply or forward is in progress against -- not only the exact one it quotes, but
  any older message in the same thread the reader has expanded -- no longer discards that reply
  with no prompt. The action itself still goes through -- and remains undoable exactly as before
  -- but the reply box underneath it is no longer unmounted along with the reading pane's old
  content
- Search's folder picker only offers the account currently being searched -- picking a folder
  from another account produced a query that could never match, shown as an ordinary
  "No results found" rather than the account/folder mismatch it actually was
- Semantic search with no AI provider configured now says so, rather than showing the same
  empty state a genuine no-match search gives
- A recurring calendar series that takes too long to expand runs on its own bounded thread pool
  rather than the one every other background job shares, so a retried request against the same
  pathological object can no longer starve unrelated work by slowly exhausting shared workers
- A DAV account's "Synced" time no longer reads "Synced now ago" (or "Synced Yesterday ago") for
  a sync that just completed or completed yesterday
- A settings field's label reads as a sentence ("Default event duration minutes") rather than
  its raw key name

- A sender's avatar -- in an open thread, in the mail list, in the unified view and in search
  results -- now shows their address-book contact's photo, when one matches. An embedded photo
  renders with no request of its own; a remote photo URL only renders once the sender is on the
  same allowlist that already gates the message's own remote images. Every list reads its rows'
  photos from one bulk lookup per account rather than a request per row or per sender scrolled
  into view
- A contact photo far larger than any reasonable upload is rejected server-side rather than
  reaching the vCard and the CardDAV server it syncs to unbounded
- Editing a contact's photo or categories now removes every existing line of that kind rather
  than only the first -- a card carrying more than one (legal, and produced by some servers) kept
  the extras around after a replacement
- A contact whose birthday is a real but unrecognized value (a stray Feb 29 outside a leap year,
  for instance) says so in the detail view and the editor instead of silently disappearing;
  editing an unrelated field and saving leaves it exactly as it was
- A shift-click extending a range selection in the contacts list no longer also selects the
  row's own text as a side effect
- The contact detail's edit and delete buttons have accessible names

## [3.1.1] - 2026-09-03

### Fixed

- The month view no longer repeats an event. A recurring occurrence wider than the window slices
  the expansion probes in — an all-day event on the first of the month worst of all — was returned
  once per slice it overlapped, so a birthday could appear seven times in one day
- `is_exception` reports whether an occurrence is actually overridden by a stored `RECURRENCE-ID`
  component, rather than being true for every event including non-recurring ones
- HTML message bodies render on a light canvas whatever the application theme is. Mail sets only
  half of the background/colour pair and the dark theme supplied the other half, unreadably in
  both directions: a sender's white background under our light text, and a sender's dark text on
  our near-black background. Plain-text bodies, whose wrapper this application generates itself,
  still follow the theme

## [3.1.0] - 2026-09-03

### Added

- Trash, archive and spam offer an "Undo" on their success toast, both from a single row and
  from the bulk toolbar, moving the affected message(s) straight back to the folder they were
  in — a compensating action rather than a delayed commit, the same shape a failed mutation's
  own optimistic rollback already uses
- A `ui` test layer (`tests/ui/`, `pytest -m ui`) drives the application through a real browser
  with Playwright, reusing the same testcontainers world the `e2e` layer builds for itself, plus
  `scripts/devstack.py` for running an independent, compose-less development stack per checkout
- **Identities** — a mail account's addresses to send as. `GET`/`POST /api/identities`,
  `PATCH`/`DELETE /api/identities/{id}`; at most one default per account, enforced at the
  database level. `POST /api/outbox` and the MCP `send_mail`/`draft_mail` tools accept an
  `identity_id`, falling back to the account's default identity and then to `accounts.imap_user`
  when it has none — an account that never adopts this table behaves exactly as before
- **Calendars and contacts**, mirrored the same way mail is (requires PostIMAP >= 1.6.0):
  `GET`/`POST`/`PATCH`/`DELETE` on `/api/dav-accounts` and `/api/calendars`, `GET`/`PUT
  /api/calendar/links` for the identity-to-calendar mapping, `GET`/`POST`/`PATCH`/`DELETE` on
  `/api/calendar/events` with recurring-series expansion and per-occurrence editing, `POST
  /api/calendar/events/{id}/respond` for RSVPs, and `GET`/`POST`/`PATCH`/`DELETE` on
  `/api/contacts` plus `/api/contacts/search` for the compose autocomplete. Every write to the
  calendars/contacts tables PostIMAP mirrors goes through `postimap/actions.py`, proven against
  the real `postimap_app` role, not just an owner connection
- **Invitation intake** — an emailed `.ics` attachment becomes a calendar entry on its own:
  `calendar/intake.py` reacts to a message arriving the same way the spam feedback listener does,
  resolves the recipient identity from the ATTENDEE list (falling back to To/Cc), and imports,
  updates, cancels or records a reply against the calendar_intake table's never-classify-twice
  gate. A UID already held anywhere is always updated in place rather than duplicated — the
  hand-imported case, and what keeps two of an identity's own addresses invited to the same event
  from producing two copies. `GET`/`POST /api/calendar/invitations` reads a parsed invitation with
  its intake status and candidate calendars, and imports one manually (optionally linking the
  calendar to the identity), for anything intake left unlinked or that dead-lettered

- `POST /api/addressbooks` creates an address book on the server, mirroring the existing
  calendar create endpoint -- previously only reachable by calling the internal action directly,
  now a route any API client can use
- The development stack runs Radicale, a throwaway CalDAV/CardDAV server, alongside the
  mail server -- `scripts/seed_dev.py` seeds a calendar and an address book on it directly,
  so a fresh checkout shows real calendar data once a DAV account is added, the same as it
  already does for mail. `scripts/devstack.py`, the per-worktree equivalent, gained the same
  container and now seeds and links its own DAV account automatically, so a worktree has a
  real calendar to verify against without a manual step
- `tests/e2e/test_calendar_flow.py` and `tests/e2e/test_contacts_flow.py` drive the same
  real chain -- an object created through the API is verified to actually reach the server,
  and one added directly on the server is verified to reach the API after a sync. The `pg`
  layer's calendar and contact tests write PostIMAP's own parsed columns directly, since no
  live server ran in their world; these prove the assumption underneath that choice for the
  first time -- that a real sync produces those columns in that shape
- Every test/dev container this project's own tooling starts is now labelled with the PID and a
  liveness fingerprint of the process that started it, and every test session and
  `scripts/devstack.py` run sweeps and removes anything so labelled whose owner is confirmed
  dead -- never by age -- before starting anything of its own. Rootless Podman has no
  Ryuk-compatible reaper, so a run killed rather than exited cleanly previously left its
  containers running forever with nothing watching for them. `scripts/prune_orphaned_containers.py`
  runs the same sweep standalone
- The MCP server gains fifteen calendar and contact tools, mirroring the fifteen existing mail
  ones: `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`,
  `delete_event`, `respond_to_event`, `list_addressbooks`, `list_contacts`, `search_contacts`,
  `get_contact`, `create_contact`, `update_contact`, `delete_contact`. Each wraps the same
  `api/calendar_events.py`, `api/calendars.py` and `api/contacts.py` functions the REST endpoints
  call, so there is one definition of what creating or editing an event does. `create_event` and
  `update_event` accept a raw `RRULE` value, the full RFC 5545 vocabulary rather than a fixed
  preset, so an agent can express an interval, a weekday set, a count or an end date the same way
  the REST API already could
- Clicking a day in the month view opens it in the day view, and a week number down the left side
  of each row opens that week. Dragging inside the month grid itself is not offered -- it would
  need the pointer machinery to interact with the scroller's virtualisation, with no cheap way to
  be confident in it -- so this is how create, drag and resize become reachable from month view
  without dragging in it, since both already exist in day and week view
- `tests/ui/` gains browser-level coverage for an all-day event's exclusive end, invitation intake
  driven manually and automatically over real LMTP delivery (`tests/ui/test_calendar_invitations_ui.py`,
  new), editing a contact and finding it by a secondary email, and the phone layout's contacts
  page and month-view day tap. Two of the new tests describe correct behaviour this suite never
  asserted before and currently fail against real, unfixed defects rather than anything wrong with
  the test itself -- see `test_arriving_mail_holds_the_list_scroll_position` and
  `TestPhoneLayoutUi.test_contacts_page_has_an_add_control` in `tests/ui/test_mail_actions_ui.py`

### Fixed

- An action on a message already in its target folder (marking spam already in Junk as spam
  again, archiving something already in Archive, an explicit move onto the current folder, and
  the bulk variants of each) no longer strands the row mid-move. `move_message()` and
  `move_message_bulk()` wrote `imap_uid = NULL` unconditionally, and PostIMAP only enqueues a
  sync when the folder actually changes, so nothing ever cleared it and the row spun forever.
  Both writes now skip a message already in the target folder
- Dragging a message onto a folder moved it to the folder above the one under the pointer,
  because the default rectangle-intersection collision detection compared the dragged row's
  height against the shorter sidebar items. Drag-and-drop now uses pointer-position collision
  detection, and dropping a message back onto its current folder is a no-op rather than a request
- `scripts/seed_dev.py` stamps each delivery with the current date and a fresh Message-ID. The
  fixtures carry dates from the year they were written, so the corpus arrived older than
  `pipeline.live_max_age_days` and was never classified — the development stack came up with
  spam detection apparently dead. A repeated Message-ID had the same effect on a second run.
  `--keep-dates` delivers the fixtures verbatim
- Archiving a message returned "No archive folder found for this account" on any server that
  never advertises IMAP SPECIAL-USE, even with a folder literally named Archive present.
  Resolving trash/archive/junk/inbox now falls back to matching a well-known name for the role
  when no folder carries the flag
- The pipeline queue's worker loop never passed its own `max_attempts` setting down to
  `claim_batch`, so a row that crashed the worker process itself before ever reaching the
  retry-vs-fail decision was reclaimed and re-claimed forever instead of eventually stopping
- The dev image's `/api/health` check could report the container healthy for minutes after a
  file-watcher reload crashed at startup. `--reload` pre-binds the listening socket once and
  reuses it across every worker generation, so a dead worker still leaves 8080 accepting
  connections; a plain `curl -sf` with no timeout of its own can hang on a response that never
  comes rather than failing. The check now bounds curl with `--max-time` and checks more often,
  so a dead worker is reported unhealthy within seconds instead of minutes

- Switching to a second mail account no longer strands the message list on the previous
  account's folder. The folder list keeps the previous account's data on screen while the new
  account's own folders are still loading, and the sidebar's inbox auto-selection picked a
  folder from that stale, wrong-account data the moment it saw none selected yet -- so the
  account switcher and the sidebar's folder badges correctly showed the new account while the
  message list stayed on "No messages in this folder" until a folder was clicked by hand.
  Auto-selection now waits for the new account's own folder data before picking one
- Editing an event keeps the timezone it is bound to. An edit carries its times as instants, and
  those were written back with whatever offset they arrived with -- so a named zone was replaced
  by a bare UTC stamp, or by an invented offset name the object defined no timezone for. On a
  single event that only loses a label, since the moment is the same either way. On a repeating
  one it is not: every later occurrence resolves against that zone, so a weekly 09:00 meeting
  quietly became 08:00 from the end of October, after an edit that only meant to rename it
- A calendar event whose details cannot be loaded says so, with the reason, instead of showing a
  spinner that never stops
- An event created in the calendar is stored at the time that was entered. The editor sent a UTC
  instant together with the browser's own zone, and the API binds the reading it is given to the
  zone it is given -- so an event entered at 10:00 in Berlin was stored, and shown, as 08:00.
  Editing was never affected, since an edit sends no zone. A browser in UTC could not see it
- Clicking an event that carries a timezone opens it again. The identifier naming one occurrence
  carried no zone of its own, so resolving it back to a moment in time assumed UTC and looked for
  the occurrence a whole offset away from where it is -- the popover, the Edit and Delete
  controls, the Delete key and the MCP tools all reached that occurrence by exactly that
  identifier, and all of them got "not found" for any event a real CalDAV server stores. An
  occurrence identifier is now an absolute instant wherever it is read, and an exception this
  application writes names its occurrence in the series' own zone rather than in no zone at all
- Retyping a date in the event editor no longer replaces the calendar with an error screen.
  A single digit typed into the year segment left the field holding a value the control could
  not parse, and the error that followed reached the page's error boundary, taking the editor
  and everything typed into it with it
- A bulk action (archive, trash, spam, move) that the server reports as failed no longer shows a
  success toast with an Undo. The endpoint answers 200 even when it did nothing, carrying the
  reason in an `errors` field the bulk handler never read -- it now surfaces that failure the same
  way the single-row actions already did, and a response that moved fewer messages than requested
  (without failing outright) shows "N of M", not the full count
- Retry, pipeline and semantic settings whose defaults are whole-number floats (`base_delay_seconds:
  1.0`, say) can now be saved. Their form field renders as a plain number, and JSON has no way to
  write a literal `1.0` for the JS number `1` -- the API rejected the resulting int on every save of
  an untouched field. Settings now accept an int wherever a float is expected. A settings save that
  fails for any reason also now shows a toast naming why, rather than leaving the form showing the
  edited value with the old one still stored and nothing said about it
- Choosing a recipient suggestion by arrow-down-then-Enter adds that recipient again. Enter commits
  the highlighted item by clicking the DOM node the combobox finds at its own index, and that
  registry is only populated for an item that names its index -- ours never did, so the item
  highlighted correctly (arrow keys, `aria-activedescendant`) but Enter's click found nothing there,
  silently doing nothing while clearing the field's typed query on the way out
- The recipient field's suggestion list no longer opens with nothing in it. It appeared as soon
  as the field was clicked and stayed up for any typed text, covering the Subject field beneath
  it with a hint -- so a click aimed at Subject landed on the hint instead, and had to be made
  twice. The list now opens only when there is a contact to choose
- That also restores the rest of the compose form to screen readers while a recipient is being
  typed: an open suggestion list takes everything outside it out of the accessibility tree, which
  put Subject, the message body and both buttons out of reach for as long as the list was up --
  which, until now, was the whole time
- `test_compose_and_send_shows_a_toast_and_reaches_mailpit` asks for the account fixture it
  needs. It relied on another test in the module having created it first, so it failed when run
  on its own
- The event editor tells the truth about who it will mail. It asked whether an event had an
  organiser at all rather than whether that organiser is you, so an event you organise fell
  through as though it were someone else's -- its delete confirmation and its recurring-scope
  prompt both stayed silent about the cancellation going out, and saving an edit mailed every
  guest an update with no prompt at all. All three now compare the event's organiser against the
  identity its calendar is linked to, and name the guests before anything is sent
- The event editor's Calendar and Repeats controls show the calendar's name and the repeat's
  label rather than the raw identifier and the raw RRULE behind them
- The calendar renders correctly for a browser whose timezone or date differs from the machine
  that built the application. Pages are prerendered to static HTML at build time, so today's
  column, the current-time line and the date the whole view is anchored on were baked from the
  build machine's own clock -- a browser on a different day hydrated against markup for another
  one and React rebuilt the tree, losing the toolbar's click handlers on the way. The calendar
  view and its sidebar now render once the browser has them
- Opening New event before the calendar list has arrived no longer leaves Save disabled for
  good. The editor read its default calendar out of that query once, in a state initialiser and
  in an effect keyed on the sheet opening, neither of which runs again when the query resolves
  afterwards -- so on a slow load the calendar stayed empty with nothing said about why
- Dragging an event in the day or week grid no longer opens its popover on the values the drag
  has just replaced. Beginning a move captures the pointer on the chip, and a captured pointer
  retargets the click derived from the release back to that chip wherever it ends up, so every
  drag also fired the chip's own click handler. The click a real drag derives is dropped; a plain
  click still opens the popover
- Choosing an option from a dropdown in the event editor no longer closes the editor. The event
  popover dismissed itself on any pointer press outside its own DOM subtree, exempting the editor
  and the confirmation dialogs by name -- and a dropdown renders its options into a portal of its
  own, matching neither, so the press that chose a calendar or a repeat unmounted the popover and
  took the editor down with it before Save could be reached. The popover now leaves dismissal to
  whichever layer it has opened on top of itself
- Clicking an event in the day or week grid no longer writes to it. The drag hook committed a
  move on every pointer press-and-release with no check that anything moved, so a plain click
  bumped the event's version and truncated its stored seconds -- and for an organized event, sent
  an "Updated:" notice to every guest on every click. A release is only committed as a move when
  the position actually changed
- Dragging one occurrence of a recurring series in the time grid no longer silently moves the
  whole series. The drag never asked which occurrences the change applies to, so the master's
  start moved and every occurrence followed it, with the dragged occurrence itself vanishing from
  where it had been. A drag on a recurring occurrence now opens the same scope prompt an edit from
  the popover already asks
- Pressing and dragging on empty time-grid space now opens the event editor prefilled for that
  range instead of doing nothing, or, had the surface actually been hit, creating an untitled
  event with no editor to fill it in. The grid surface compared the pointer's target against
  itself by strict identity, so a press anywhere except its own four edges -- which is everywhere,
  since the hour lines are its children -- never started a create-drag at all
- The event editor now sends `tz` (the browser's own IANA zone) when creating a timed event, so a
  recurring series created from the interface keeps its wall-clock reading across a DST change
  instead of drifting an hour -- the API has honoured `tz` since it was added, but the editor
  never sent it
- An all-day event created from the editor is no longer stored zero-length. The editor's date
  fields show the *inclusive* last day (matching how a person picks an end date), converted to the
  exclusive `dtend` RFC 5545 requires only where the request is actually built; toggling All day
  now seeds that day from the browser's own local time rather than the instant's UTC date, which
  could be a day off late in the evening in a positive offset
- The editor now shows a recurring event's actual repeat, and can remove it. `GET
  /calendar/events` and `/calendar/events/{id}` gained an `rrule` field the editor reads to
  initialise the Repeats select -- previously every event read "Does not repeat" regardless, and
  choosing it sent nothing, so an existing series could never be turned back into a single event.
  Removing a repeat now sends `rrule: ""`, distinct from omitting the field entirely (which
  `ical.py` already read as "leave the existing rule alone")
- Editing an event from the interface now actually saves. The editor sent an `attendees` array on
  every update -- an empty one when there were none -- and the backend's own guard against
  changing attendees rejected it outright, so renaming, moving or retiming an event from the
  editor always came back a `422`; the attendees field is no longer editable there, matching the
  API. Every occurrence carries its own `recurrence_id` once expanded for display, recurring or
  not, so a non-recurring event's edit was also sent with a populated `recurrence_id` and no
  `scope`, which the backend rejects independently of the attendees fix -- `recurrence_id` is now
  only sent alongside `scope=this`. And clicking Save closed the event popover before the click
  could complete, because the popover treats any pointer press outside its own DOM subtree as a
  request to close, and the editor's own sheet renders into a portal outside that subtree
- An event created with a named timezone now carries the `VTIMEZONE` definition RFC 5545 requires
  alongside any `TZID` that references it. Previously only the reference was written -- this
  application resolves it against its own zone data regardless, so the gap round-tripped silently
  here, but the object went to a real CalDAV server and into invitation mail exactly as malformed,
  where another client may reject it or fall back to a different zone
- `scripts/devstack.py` now verifies its own teardown against the container runtime instead
  of trusting that its cleanup calls landed, and refuses to report a clean stop while any of
  its own containers are still running. A container whose own stop call raised previously
  unwound the rest of the stack without leaking visibly -- the process still exited zero,
  nothing printed a warning, and the container just sat there
- Creating an event with `tz` now actually binds `dtstart`/`dtend` to that named IANA zone --
  previously the field was declared on the request and silently discarded, so the stored event
  only ever carried a fixed UTC offset regardless of what `tz` said. The given wall-clock reading
  is kept; only the zone it resolves against changes, the same way `DTSTART;TZID=...` behaves on
  every other CalDAV client. `all_day` and an unrecognised zone name are both refused with `400`
  rather than silently ignored or accepted into a nonsensical object
- Editing an event's `attendees` or `tz` now answers `422` instead of a `200` that reports
  success while changing neither. `PATCH /calendar/events/{id}` never applied either field --
  a caller renaming the attendee list, or setting a timezone, got a confirmed write back with
  nothing actually different underneath. Changing who is invited needs its own `REQUEST`/`CANCEL`
  sends, the way create and delete already give attendees, and `tz` has no settled meaning apart
  from `dtstart`/`dtend`, which already carry the instant -- rejecting outright rules out the one
  shape that must never happen, the same way `scope=following` already is rather than silently
  treated as `scope=this`
- An RSVP whose outbox row has aged out of retention now reports `outbox_status: "unknown"`
  rather than `"pending"` -- the status an active, in-flight send uses. A genuinely pending row
  can become `"sent"` or `"failed"` on its own; a row that no longer exists never will, so
  reusing `"pending"` for it was a claim that stayed wrong forever once made, and the interface
  read it as "still sending" indefinitely. `api/invitations.py` carried an identical copy of the
  same resolution logic and the same bug; it now calls the one definition in
  `api/calendar_events.py` instead
- The month view's header now always names the month the grid beneath it is actually showing, and
  `Today` reliably returns to today. Both could land on a date years away from what was on screen
  after a resize (a viewport change, a sidebar toggle, the phone/desktop layout switching):
  measuring the viewport and correcting the scroll position for the new row height were split
  across two effects, one reading `rowHeight` from React state rather than from the value the
  other had just computed -- state a layout effect cannot see until a later render. The correction
  landed against the *old* row height, and by the time the *new* one actually rendered there was
  nothing left recorded to correct it with
- Delete/Backspace with an event selected now opens its delete confirmation. The key handler and
  the popover owning that confirmation were not siblings in the component tree, so the key press
  reached a callback nothing was ever wired to and silently did nothing
- The month view now reads only what its window could contain -- a SQL predicate on
  `dav_objects.dtstart`/`dtend`/`is_recurring` instead of parsing and expanding every object in
  every visible calendar on every request, which cost seconds of blocking, single-threaded CPU
  (stalling mail with it) against a calendar of a few thousand events. `dtend` is `COALESCE`d to
  `dtstart` in that predicate, since PostIMAP only ever writes `dtend` from an explicit `DTEND`
  property -- a `DURATION`-only event, or the canonical single-day all-day
  `DTSTART;VALUE=DATE` with neither, would otherwise vanish from the calendar entirely, silently
- Editing an event with attendees now sends a `REQUEST`, the way creating one sends a `REQUEST`
  and deleting one sends a `CANCEL`. An edit previously notified nobody, so moving a meeting's
  time left every attendee's own calendar silently wrong
- Correcting a wrong CalDAV password on an already-running DAV account now reconnects it, the
  same `is_active` bounce mail accounts already get. Previously the account kept retrying the old
  credential until something else restarted it, so fixing a mistyped app password appeared to do
  nothing
- A calendar edit the server rejected in favour of a newer copy now shows as such on the event,
  instead of being filtered out of the event-level error entirely. A `412` conflict means the
  server's version already overwrote the one just written -- the single case where an edit was
  silently discarded was previously the one case excluded from view
- Invitation import now records a `pending` intake row before writing the calendar object it
  describes, promoted only once that write lands. Previously the row was written with its
  terminal status first, so anything interrupting between the two left a row saying e.g.
  `imported` with no object behind it -- and the never-classify-twice gate then made that message
  permanently unprocessable, while the UI read the row as a completed import
- Automatic import into a read-only calendar is now refused the same way the manual "add to
  calendar" flow already was, instead of silently dead-lettering on the server
- Editing an event as an attendee (not this calendar's own event) no longer advances `SEQUENCE`.
  `SEQUENCE` is the organizer's own version counter; bumping it locally made the organizer's next
  genuine update to the same event lose to it as stale and get silently discarded
- An invitation carrying both a `text/calendar` and an `application/ics` part now always picks
  the same one, instead of whichever an unordered read happened to return
- The archive/junk/trash folder name fallback now matches a namespaced mailbox
  (`INBOX.Archive`, `INBOX/Archive`) by its last path segment instead of only the full name, and
  picks the same folder every time when more than one candidate name is present (`Junk` and
  `Spam` both existing, for instance) instead of whatever order the database happened to return
- Deleting an identity no longer destroys the RSVP history it left behind: `calendar_replies`
  un-links the identity (`ON DELETE SET NULL`) the same way `calendar_prefs` already does,
  instead of cascading the delete onto every reply that identity ever sent
- A data migration rewrites any `calendar_intake` row already stranded at `imported` with no
  object behind it (the shape a pre-`pending`-status build could produce) to `pending`, so it is
  retried rather than staying permanently unprocessable
- `POST /api/calendar/invitations/{id}/import` is now what confirms a `pending_review`
  REQUEST/CANCEL: a `CANCEL` is applied as a cancellation rather than merged into the event's
  fields the way a `REQUEST` is (a `.ics` carrying `METHOD:CANCEL` commonly has little more than
  the UID and `ORGANIZER`, and would otherwise blank out the event it means to cancel), and
  `calendar_id` is no longer required for that case — the target is the existing object itself.
  `GET` on the same message now describes what that confirmation would actually do rather than
  what the automatic listener's own narrower reachability scope alone would see, so the two no
  longer disagree about which object a confirmation targets
- Creating a contact from the interface now actually works. The default address book was read
  on first mount, before address books had loaded, so every create sent an empty
  `addressbook_id` and was rejected -- the sheet stayed open with no error at all. It is now
  resolved once the sheet opens, and a failed create shows why, the same way the event editor
  already does
- Deleting an event from its popover, when you organise it and it has guests, now names how
  many will be told a cancellation is being sent, instead of the generic "cannot be undone"
  warning while silently mailing them anyway. Organiser detection compared the event's
  `ORGANIZER` against nothing at all -- true only for a purely local event with none -- rather
  than against the calendar's own linked identity
- The identity, invitations and server selects in Manage calendars now show a name instead of
  a raw stored value: an account UUID, an identity UUID, or the intake enum's own member name
  (`none`). Every `Select` there rendered its value verbatim with no label lookup. The Server
  select also never actually applied a value chosen from a still-loading account list -- a
  `Select` decides once, on its first render, whether it is controlled, and treats `undefined`
  then as "never controlled", so setting a real id once the list arrived changed the state
  without the trigger ever showing it
- Today, after navigating away in month view, now reliably returns to today's own weekday
  instead of landing on some other week's Monday, and the toolbar, the month grid's own header
  and the sidebar mini-month all agree on which month that is. The scroll listener wrote
  whatever week was passing under the viewport's top edge back into the shared date during
  Today's own animated scroll, repeatedly, overwriting the anchor date it had just been given
  before the animation settled; the mini-month, seeded once from that anchor, never followed
  it again afterwards
- The recipient field's arrow-down-then-enter now commits the highlighted suggestion instead
  of the raw typed text. Enter always turned whatever was typed into a chip regardless of
  whether a suggestion was highlighted, so a keyboard selection silently queued a nonexistent
  address that only failed once sent. Text that doesn't parse as an address is now named in a
  toast rather than either silently accepted as one or silently dropped
- Manage folders no longer offers Delete on a special-use folder (Sent, Trash, Junk, Drafts) --
  only INBOX, absent from that list entirely, was ever protected there; the rest showed the
  same destructive control and confirmation as any folder created by hand
- Mail arriving above a scrolled-away reader no longer pushes every visible row down by one.
  The virtualized mail list never told `virtua` a prepend had landed, so the size cache it
  keeps per row index stayed aligned to the old positions once the new row shifted everything
  after it -- the list now recognises a genuine prepend (mail arriving) apart from a page
  appended at the tail (older mail paging in) or a different list entirely (a folder switch,
  a threading toggle), and only then hands the render to the library's own prepend-shift mode
- A contact can now be created from a phone-sized viewport. The contacts page's mobile branch
  rendered the contact list or its detail and nothing else -- the editor sheet was mounted
  underneath, but nothing on that branch ever opened it
- Dragging out a time range in the day or week grid now opens the editor prefilled with that
  exact range instead of a fixed one-hour block rounded to the next full hour. The grid computed
  the real dragged start and length, then passed only the bare date onward, discarding both
  before the editor ever saw them
- The compose dialog's From select now shows the account's name instead of its raw id, once more
  than one account exists -- the same one-line cause and fix as the other rendered-a-raw-value
  defects already listed here, only visible with two or more accounts, which every fixture up to
  now had only ever seeded one of
- A provider outage no longer crashes the worker that was supposed to contain it. The circuit
  breaker's own suspension log passed a field named `name`, which collides with a reserved
  attribute the logging module already sets on every record -- so the log call itself raised and
  took the calling worker down, every time a provider became unavailable

### Security

- An emailed `METHOD:REQUEST` or `CANCEL` naming a UID already held in the calendar is never
  applied automatically now, whoever it claims to be from. Matching the incoming `ORGANIZER`
  against the stored object's own was tried first and does not actually authenticate anything: a
  co-attendee of the real meeting already holds both the UID and the `ORGANIZER` address, since
  both are lines in the same `.ics` they themselves received, so equality only raises the price of
  forging a change, it does not stop one. Every such message becomes a `pending_review` intake row
  a person confirms by hand instead (`POST /api/calendar/invitations/{id}/import`, unchanged for
  everything else) — the confirmation card shows the address the message actually came from next
  to the organizer it claims, and says plainly what accepting would do, so a forgery is obvious to
  a person without needing any header authentication. Previously any sender who merely knew an
  event's UID could silently rewrite, move or cancel it by emailing a matching UID with a higher
  `SEQUENCE`. A `REPLY` still applies automatically, gated on the replying attendee actually being
  the message's own sender (`unauthorized` if not) — the harm a forged one can do is a wrong
  attendance mark, not a moved or cancelled meeting, and requiring a click per RSVP would make a
  real meeting's replies unusable. A `REQUEST` naming a UID nothing holds is a genuinely new
  invitation and keeps importing on its own; the worst it can do is calendar spam, same as any
  other unsolicited mail. The automatic listener's UID lookup is also scoped to DAV accounts
  reachable from the receiving mail account's own identities, rather than every DAV account in the
  database — a person explicitly importing or confirming one message by hand is unaffected, since
  that lookup was never the unauthenticated surface
- Automatic invitation import now requires the identity to actually be an ATTENDEE, rather than
  falling back to being a To/Cc recipient. Being addressed is not being invited, and the fallback
  was also the easiest way to get an RRULE past a spam filter into the calendar in the first
  place, since intake runs before the message is ever classified. The manual "add to calendar"
  flow is unaffected -- a person choosing to import a specific message needs no identity
  resolution to know which calendar to use
- A recurring event's expansion is bounded regardless of how its RRULE (or RDATE) is spelled: a
  window is walked in progressively wider slices, counting real occurrences as they come back from
  the expansion library, and refused the moment more than a few thousand have been produced —
  rather than asking the library for the whole window in one call, which previously ran
  synchronously on the same process as mail sync, SSE and health checks. A single emailed
  `FREQ=SECONDLY` invitation, or one whose `BYHOUR`/`BYMINUTE`/`BYSECOND` widen a coarser `FREQ`
  to the same effect, or a large `RDATE` list, could freeze and OOM-kill the whole application for
  as long as the event stayed in the calendar; an earlier version of this fix inspected the
  RRULE's own text before expanding and missed exactly those cases (and introduced its own crash
  on a malformed body with a repeated `RRULE` line) — this measures actual output instead, so it
  is immune to how the danger is spelled. Creating or editing an event through the API still
  refuses `FREQ=SECONDLY`/`MINUTELY` outright as a cheap first line, and looking up a single
  occurrence by `RECURRENCE-ID` searches a one-day window around that occurrence's own timestamp
  instead of a six-year span. The month view also no longer fails as a whole when one calendar
  object cannot be expanded or parsed -- that object is skipped, not every visible calendar

## [3.0.0] - 2026-08-30

Supersedes 2.0.0, which could not add an account at all on a deployment connected as the
consumer role the chart prescribes.

### Breaking Changes

- **`DELETE /folders/{id}` now requires a `confirm_message_count` query parameter** matching the
  folder's current message count. Deleting a folder destroys every message in it on the mail
  server, irreversibly, and the browser UI's own confirmation dialog was never a REST-layer
  guarantee — an API or MCP client got none. A call without the parameter (or with a stale count)
  is now a `409` naming the folder's actual message count instead of deleting outright; repeat the
  call with that number to confirm
- **The `spam` settings category is gone.** `GET`/`PUT /api/settings/spam` now `400`. Nothing
  outside the one-time `0006_pipeline` migration ever read it: whether spam detection runs is
  `account_prefs.spam_enabled` (`PATCH /api/accounts/{id}`), and auto-move-to-junk /
  auto-mark-read are an ordinary pipeline `match` stage an account's own pipeline document
  configures
- **`pipeline.enabled` and `ai.enrichment_model` settings are gone** — neither had a reader; the
  pipeline document's own `enabled` field is the one that gates the runner

### Security

- **A crafted `style` attribute could put an event handler on an element the sanitizer had
  already approved.** A CSS tokenizer recovering from an unterminated string echoes the malformed
  text back, quote and all, and that text was spliced into the rewritten `style` attribute
  unescaped — so the attribute ended early and everything after it became markup the tag and
  attribute allowlist never saw, `onerror=` included. The browser client happened to strip it in a
  later pass, but `GET /api/mails/{id}` hands `body_html` to any API consumer with no such pass.
  Values are now escaped as they are written into the attribute, and a declaration whose value
  failed to parse is dropped rather than serialized back out.

- **A message could cover the entire application and steal every click on it.** The server-side
  sanitizer tried to strip `position`, `z-index`, `transform` and similar CSS declarations by
  matching the property name with a plain string split, which is not how CSS is actually parsed —
  a comment between the name and the colon (`top/**/:0`) or a hex escape inside the name
  (`p\6fsition:fixed`) both parse as an ordinary declaration in every browser and both slipped
  through untouched. Wrapped in a link, a message using either trick covered the whole viewport at
  the top stacking layer and turned a click anywhere in the sidebar into a navigation the sender
  chose. The same class of bypass also let a CSS `url()` reach the network for tracking purposes
  despite remote-image blocking being on, via the identical comment/escape trick hiding the
  function name. CSS declarations and `url()` references are now identified by parsing the value
  with a real CSS tokenizer instead of matching text, and a vendor-prefixed property such as
  `-webkit-transform` is now caught under the name it is a variant of. Independently of the parser
  fix, the email pane's shadow host now establishes CSS containment (`contain: layout paint`), so
  any future declaration that slips past the sanitizer is still confined to the message's own box
  rather than the viewport.
- **No response carried a Content-Security-Policy, `X-Frame-Options`, or any other hardening
  header**, so the application could be framed by any site (it has no login of its own by design)
  and had no defense-in-depth against a sanitizer bypass reaching script execution. Every response
  now carries a CSP (`frame-ancestors 'none'`, a strict `script-src` built from the exact inline
  scripts the current UI build ships, and an intentionally permissive `img-src`/`style-src` for
  legitimate remote images and message styling), plus `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `Permissions-Policy` and
  `Strict-Transport-Security`.

### Fixed

- **Two queue concurrency changes arriving together could jointly claim more of the connection
  pool than it has**, starving the API of every connection — each read the committed budget before
  either wrote, so both saw room the other was about to take. The check and the write it authorises
  now happen in one transaction behind an advisory lock.

- **Upgrading a 1.0.0 database that had ever recorded a verdict failed outright.** Migration 0002's
  backfill sets `msg_key` and `from_addr` through a lightweight table construct that did not declare
  either column, so it refused to compile the moment there was a row to back-fill. Every test
  migrated an empty database straight to head, where the backfill loop never runs — the one shape
  in which a data migration cannot fail.
- **The same upgrade then aborted on the unique index it builds.** Duplicate AI verdicts are not an
  unlikely shape there: they are exactly what the 1.0.0 bug this migration closes was producing, so
  a deployment that needs the migration is the one most likely to carry them. Duplicates are now
  collapsed to the newest verdict per message and sender before the index is created.

- **Adding an account was impossible on the deployment the chart documents.** `accounts.state_error`
  and `accounts.capabilities` are PostIMAP's to write and carry no grant for a consumer, but neither
  was marked server-managed — and a nullable column with no default of any kind is still named in an
  ORM insert. So `POST /api/accounts` emitted them, and Postgres refused the insert for any
  connection holding `postimap_app` rather than owning the database. A development database connects
  as an owner, which ignores grants, so this worked everywhere except where it mattered. The
  restricted-grant sweep in `tests/pg/test_grant_boundary.py` now covers every write helper in
  `postimap/actions.py` — nine were absent, including this one — and fails if a helper is added
  without being added to it.

- **A message or folder change could arrive during the exact moment the live SSE stream
  finished replaying a reconnect, and never reach the browser again.** The stream's own bookmark
  was re-read from the live event counter right after handing events to the client rather than
  captured from what was actually delivered, so anything appended in that narrow window got an
  id the bookmark already considered seen — dropped permanently rather than delivered on the
  next tick, with nothing in the UI to say a change had gone missing. The live loop separately
  discarded a wake-up that arrived while it was mid-delivery instead of checking for it, which
  could delay (though not lose) a change by up to the keepalive interval.
- **`?account_id=` on the live-update stream matched nothing when the UUID was not lowercase.**
  The stream silently fell back to sending nothing but keepalives for that connection, with no
  error surfaced anywhere.
- **A bulk action over roughly 32,700+ messages (`POST .../messages/bulk-action`, by id list or by
  a whole-folder scope) returned a `500`.** Every batched write matched ids with an `IN (...)`
  list, which asyncpg refuses past 32767 bind parameters — exactly what "select all in this
  folder" or a large explicit id list sends on an ordinary long-lived mailbox. Matched with
  `= ANY(...)` (one array parameter regardless of list size) instead
- **`semantic.enabled`** (read by the embedding backfill reconciler) **had no entry in
  `SETTING_DEFAULTS`**, so it fell back to a value hardcoded in `embeddings/worker.py` and never
  appeared in `GET /api/settings/semantic`. It now has a proper default and is reported like every
  other setting. `semantic.concurrency` is gone instead — it had no reader
- **`POST /api/settings/import` skipped the type validation `PUT /api/settings/{category}` applies,**
  so `retry.max_retries: "banana"` was accepted and stored, surfacing only much later inside
  whatever worker first read it. Both endpoints now validate the same way and answer a type
  mismatch with a `400`; an import spanning multiple categories is all-or-nothing, rather than
  writing the categories before the bad one
- **An outbox attachment over `outbox.max_attachment_bytes` was read into memory whole before its
  size was checked,** despite the config file documenting the limit as enforced while the upload
  is being read. The read is now chunked and aborts as soon as the running total crosses the
  limit, which is what actually bounds memory for an oversized upload

- **A slow embedding call could get billed twice and push a good message toward `failed`.** The
  embedding queue claimed several messages at once under one shared lease, then processed them one
  at a time; a message waiting its turn could have its lease expire and be reclaimed by the same
  worker still busy with an earlier one, sending it back through the paid provider a second time
  and, if this repeated, toward a failure state with no error ever recorded — after which it was
  classified with no neighbour hints and nothing reported the degradation. The queue now claims one
  message at a time, the same way the pipeline queue already did.
- **Two background queues could each be configured within the database's own connection limit and
  still starve the web UI and API of every connection.** Each queue's concurrency was validated
  only against the pool's total capacity, never against what the other queue — or the HTTP requests
  sharing the same pool — already needed. Concurrency is now validated against every registered
  queue's combined demand, with a share of the pool reserved for requests
  (`database.reserved_for_requests` in `config/config.yaml`), and the ceiling reported by
  `GET /api/queues` reflects what is actually available to raise a queue to right now. Also applied
  on every startup, so a combination stored before this validation existed is clamped rather than
  applied as-is.
- **`setup_logging` cleared every handler on the root logger, including ones it did not install.**
  In a test session that takes pytest's own log capture with it, permanently, for every test that
  runs after the first one to boot the application — so an assertion on a log line silently stops
  asserting anything. It now replaces only the handler it installed itself.

### Removed

- **The embedding queue's per-request claim-batch-size control (`batch_size` on `GET`/`PATCH
  /api/queues`) is gone.** It never controlled the embedding queue's own claim size and had no
  effect on the pipeline queue at all — changing it did nothing observable. Every queue now always
  claims one row at a time, which is also what makes the fix above safe.


## [2.0.0] - 2026-08-30

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
- **Real-time sync per folder.** `PATCH /api/folders/{id}/prefs` takes `real_time`, asking PostIMAP to hold an IMAP connection open on that folder so changes arrive in seconds rather than on the sync interval, and the folder reports back on `idle_status`. Each watched folder costs one connection plus one for the account, and providers cap them per account — commonly around ten — so this is a budget to spend rather than a switch to leave on. Exhausting it is visible: the folder's status becomes `failed` and a notification is written. Folder responses also now carry `backfill_total`, the denominator that makes an initial sync's progress readable.

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
- **`docs/api.md`:** the REST API reference — worked examples from an empty instance to a
  configured one, an endpoint-group overview, and the two things about the API most likely to be
  assumed wrongly: that there is no authentication at all, and that a pipeline write behaves
  differently from every other write when it names a folder that does not exist yet.
- **A `/pipeline` page**, so the pipeline is reachable from the browser rather than curl-only: two
  queue cards (state, depth by status, a concurrency stepper, pause/resume, a red border and the
  reason when a circuit is open); the stage list (order, type, enabled toggle, drag-to-reorder,
  unresolved-folder warnings inline) with an add/edit dialog generated from `GET
  /api/pipeline/stage-types`'s JSON Schema — a scalar property gets a real input, anything with
  structure (a condition tree, an effect list) gets a JSON textarea, so a stage type registered
  later needs no UI change; a failures table with per-row and retry-all; a live tail of the last
  fifty runs; a dry-run tester for the whole pipeline or one stage; and revision history with
  restore. Rules live here now — the settings page's Spam tab lost the toggles that only ever fed
  the one-time migration into the pipeline's first revision, replaced with a note pointing here.
- **Forward.** A reply-box button alongside reply/reply-all, prefilling the subject and a quoted
  body and carrying the original's attachments along — downloaded and re-attached client-side,
  no backend change needed since a forward is an ordinary `POST /api/outbox`. Deliberately sets
  neither `in_reply_to` nor `references`: a forward starts a new thread with someone new rather
  than joining the sender's own conversation.
- **`GET /api/messages/:id/raw`:** downloads a message's stored RFC822 source as a `.eml` file.
  `409` when `raw_source` is NULL because the message exceeded `storage.max_message_bytes` and
  was never fetched from IMAP at all, distinct from `404` for a message that does not exist.
- **The notification centre.** PostIMAP's `sync_notifications` — a durable, acknowledgeable record
  of a write that never reached the server, including a send that never left — is now surfaced:
  `GET /accounts/:id/notifications`, `GET .../unacknowledged-count`, `POST .../:id/ack`, `POST
  .../ack-all`, a `notification.new` SSE event, and a bell in the sidebar with an unread badge.
  Requires PostIMAP service_version >= 1.3.0, gated the same way folder CRUD is.
- **A confirmation before permanently deleting a message.** The reading pane offers "Delete
  forever" only inside Trash (everywhere else, the trash button is the reversible move it already
  was), behind the same confirm-dialog pattern folder deletion already used — now shared as
  `ConfirmDialog` rather than duplicated.
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
- **Attachments on an outgoing message are bounded by count and by total size**, not only per file. Each is held whole in memory to be composed, so a per-file limit alone was satisfied by many files that together were not. Both limits, and the per-file one, now live in the configuration file rather than as a constant in the handler.
- **A message can no longer cover the application.** A shadow root isolates styles but does not create a containing block, so `position:fixed` in a message resolved against the viewport — a message could paint over the whole interface, and wrapped in a link the renderer's own click handling turned a click anywhere into a navigation the sender chose. Positioning, stacking and transform declarations are now dropped; ordinary layout is untouched, and allowing a sender's images cannot revive them.
- **A plain-text message can no longer run script.** The plain-text render path escaped angle brackets but not quotes, and the linkifier places a matched URL inside an `href` attribute — so a URL ending in a quote closed that attribute and everything after it was parsed as further attributes, including an event handler. No HTML part was needed and hovering the message was enough. Since the application has no authentication of its own, that script inherited the whole API. Quotes are escaped, the URL pattern stops at one, and the result goes through the same sanitizer the HTML path uses.
- **Blocking remote content no longer depends on how the sender quoted their attributes.** The rewrite ran against the raw message, so an unquoted `style` or `background` slipped a pattern written for quoted ones — silently, because the rewrite simply did not fire. Sanitizing first normalises every attribute to one form.
- **The single-page fallback no longer serves files from outside the build directory.** One of its two branches checked containment and the other did not; appending `.html` to an attacker-supplied path does not stop it climbing.
- **A body that is not JSON returns 400 rather than 500.** The earlier fix caught field validation but not the parse itself.
- **A deleted folder disappears from the sidebar.** Deletion tombstones a folder rather than removing the row, and the ordered listing the sidebar reads did not exclude tombstones while the folder list did — so a deleted folder vanished from settings and stayed in the sidebar permanently, clickable and long gone from the mail server. The cross-account unified listing had the same gap.
- **Sending mail works under the real database role.** `outbox.error`, `sent_message_id` and `sent_at` are PostIMAP-managed and carry no insert grant, but the ORM named them anyway — they had no default of any kind, so nothing marked them as server-managed. The development database connects as an owner, which bypasses grants entirely, so this only appeared against the restricted role a deployment actually uses: every send and every draft would have failed with a permission error.
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
- **Cursor pagination could repeat or skip a page.** The message list, threaded list and unified
  list all order by `received_at DESC, id DESC`; a cursor whose row had a NULL `received_at` (or
  whose previous page ended on one) either applied no filter at all — repeating the page it was
  meant to advance past — or excluded every remaining row, depending on which endpoint. Both
  followed from a plain `received_at < cursor` comparison, which SQL's three-valued logic treats
  as unknown, not false, whenever either side is NULL. `core/cursor.py`'s `after_cursor` builds
  the predicate PostgreSQL's own NULLS-FIRST-on-DESC placement actually needs.
- **A message list query pulled the full raw message and HTML body of every row.** `GET
  /accounts/:id/messages` and the unified equivalent `SELECT`ed the whole `Message` entity for a
  response that renders a sender, a subject and a 120-character snippet; `raw_source`,
  `raw_headers` and `body_html` are now deferred on every list query.
- **An attachment or `.eml` filename outside Latin-1 raised `UnicodeEncodeError` building the
  response**, turning a download into a `500`. `Content-Disposition` now carries both the plain
  ASCII-folded form and the RFC 5987 `filename*=UTF-8''...` form every current browser prefers.
- **A dropped SSE connection resumed blind.** A reconnect whose `Last-Event-ID` had fallen out of
  the in-memory ring buffer was told `connected` — the event a *fresh* connection gets, which
  nothing on the client treats as a reason to invalidate anything — instead of `resync`, the event
  `use-sse.ts` actually listens for. Everything that happened during the gap was silently missed
  until the next unrelated event landed. The server now sends `resync` in that case.
- **`folder_prefs.special_use_override` is now honoured by the pipeline itself, not only by the
  folder repository and the enqueue query.** The move-spam stage's `FolderResolver` and the
  runner's own `MessageView` folder load both matched the raw `folders.special_use` column only,
  so on a server that never advertises SPECIAL-USE, the default move-spam stage could never
  resolve a junk folder set only through the override — every spam message's run failed
  permanently, silently, with nothing to distinguish it from any other misconfiguration. Both now
  coalesce the override in, the same way `database/repository.py`'s `get_effective_special_use`
  already did.
- **Deleting INBOX no longer only checks the raw `special_use` column.** On a server without
  SPECIAL-USE, or a folder whose INBOX status is only known through
  `folder_prefs.special_use_override`, the guard never fired — deleting that folder destroyed the
  entire mailbox on the server, irreversibly. The check now also matches the case-insensitive
  `imap_name` IMAP mandates for every server, and the override.
- **A second message reusing the Message-ID of one already run no longer silently bypasses
  classification.** `pipeline_runs`' dedup key was `(account_id, msg_key, dedup_key)`, with no
  sender — the run-level dedup collapsed before `classify` ever reached the verdict table's own
  sender-scoped protection, so the second message got no run at all, and the conflicting insert
  repointed the first message's run row at the second message's id, making the first message's own
  run history disappear. `pipeline_runs` gains a `from_addr` column, folded into the same unique
  index the verdicts table already uses it in (Alembic `0008_pipeline_run_from_addr`).
- **A rule with a move-to-junk effect and no `RecordVerdict` of its own no longer manufactures a
  fake human correction.** The folder-move feedback listener recorded a `user_feedback` verdict
  whenever a move into junk did not already agree with `is_spam=true` — including when no verdict
  existed at all, which the listener's own reasoning had assumed could only happen for a genuine
  human drag. Any other stage with a move-to-junk effect (a "block this sender" rule, most
  obviously) produces that same no-verdict move for a reason that is not human, and there is no
  signal available here to tell the two apart — the classifier's own move-spam stage stays
  unaffected, since it always writes its verdict before its move commits. A correction is now
  recorded only when an existing verdict actually disagrees with the destination.
- **A condition leaf with more than one key silently evaluated only the first and ignored the
  rest**, `{"subject_contains": "x", "sender_domain": "y"}` reading as AND to anyone writing it and
  matching on `"x"` alone. Rejected outright at pipeline-write time now — `all` is the vocabulary's
  one way to combine conditions.
- **The `enrichment_tag` condition could never match.** Nothing in the codebase populates
  `MailContext.enrichment_tags`, so a stage configured with it silently never fired. Removed along
  with the dead `enrichment_tags` field, rather than left as a condition type nobody can make work.
- **A pipeline write's `base_revision` check could lose a concurrent writer's edit.** Reading the
  current revision and appending the new one were two separate round trips; two writers computing
  their edit against the same base both passed the check and both appended, with the later one
  silently winning — the exact race the `409` exists to prevent. `PipelineRevisionRepository
  .append()` now makes the check and the insert one transaction, serialized by an advisory lock.
- **The classifier's prompt fence did not hold against a message containing its own closing
  delimiter.** `json.dumps` escapes quotes and backslashes but not angle brackets, so a body
  containing the literal `</email_content>` could close the `<email_content>` fence the docstring
  named as the mitigation and inject text the model would read as outside the untrusted section.
  Every `<`/`>` in the embedded JSON is now replaced with its unicode escape before it is
  interpolated into the prompt, which is still valid JSON but can no longer spell the delimiter.
- **A message move can no longer target another account's folder.** Neither the single-message nor
  the bulk move endpoint checked that a client-supplied `target_folder_id` belonged to the
  message's own account; both now reject one that does not with `400`.
- **A bulk action's explicit id list was never checked against the path's account, and bulk
  expunge had no account check at all.** Both now resolve the supplied ids down to the ones that
  actually belong to the account and still exist, the same way a scope-resolved selection already
  did — an id from another account, or one already gone, is silently excluded rather than acted
  on.
- **A bulk action reported the number of ids it was asked to act on, not the number it actually
  changed.** `set_flags`, `set_keywords`, `move_message`, `move_to_trash`, `expunge` and their bulk
  equivalents in `postimap/actions.py` now return the row count their write actually touched, and
  `affected_count` reports that instead of `len(message_ids)` — a bulk action over ids some of
  which had already been expunged used to report success on all of them regardless.
- **Message detail and thread endpoints no longer pull the full raw message for every row.** The
  list endpoints already deferred `raw_source`/`raw_headers`; `GET /messages/:id` and `GET
  /messages/:id/thread` now do too, the latter having pulled it once per message in the whole
  conversation to produce about a kilobyte of JSON per row.
- **A suspended circuit breaker could never recover.** `try_probe` existed and was tested, but
  nothing in either worker loop ever called it — every path that could clear a breaker was gated
  behind `is_available()`, which stays `False` while suspended. A fresh install with no provider
  key configured, or a mistyped one, suspended the embedding breaker permanently: setting the key
  afterward through the settings API changed nothing, since embedding is what admits a message to
  the pipeline, classification stalled behind it too, and only a manual database update unstuck
  it. The embedding worker loop now calls `try_probe` itself when suspended, claiming exactly one
  row and letting its real provider call be the probe. `PATCH /api/queues/{name}` also accepts a
  `reset_circuit` flag for the operator's immediate manual recovery, rather than waiting out the
  probe interval.
- **The pipeline queue's circuit-breaker readout named a breaker nothing writes to.** The
  registration left `circuit_name` at the queue's own name, `"pipeline"`, while a classify stage's
  model calls trip a breaker named for the provider — and that name is the live `ai.provider`
  setting, so it cannot be captured once at registration either. A queue now registers either a
  fixed name or a resolver, and the pipeline registers one that follows the setting, so the
  reported circuit is the one a stalled queue is actually stalled behind.
- **The embedding queue's circuit-breaker readout was decorative.** Registering the queue left
  `circuit_name` at its default (the queue's own name, `"embeddings"`), while the worker's own
  `CircuitBreaker` writes to a breaker named for the provider (`"openai"`) — so the observability
  surface reported a breaker nobody ever tripped while the real one went unseen. Registration now
  names the same breaker the worker writes to.
- **A slow provider call could be reclaimed and re-run while the first call was still in
  flight.** Neither worker loop ever extended a claimed row's lease (`WorkQueue.heartbeat` had no
  production call site), and the OpenAI/Anthropic clients were constructed with no request timeout
  and the SDK's own default retries left on — so a call slower than its lease (30s for embeddings,
  120s for the pipeline) got its row reclaimed by the periodic reclaim pass and picked up by
  another worker mid-call. On the pipeline that duplicated a paid model call, and every reclaim
  also increments the row's attempt count, so a slow spell could drive a run to permanent failure
  while its original call was still going to succeed; on embeddings the worker that lost its lease
  computed a vector and discarded it. Both worker loops now extend their claimed row's lease on a
  fixed interval for as long as an item is being handled (`queue/worker_loop.py`'s
  `heartbeat_while`, reused by the pipeline runner's `default_worker_loop` and the embedding
  worker's own claim loop), and the OpenAI/Anthropic clients now carry a request timeout well
  inside the shorter of the two leases (20s and 60s respectively) with the SDK's own retries
  disabled in favour of the application's single, already-jittered retry layer.
- **A persistently retryable embedding failure retried forever, with no dead letter.** A
  connection drop, a 5xx or a timeout all refunded the row's attempt count and rescheduled it
  immediately (correct for a genuinely shared-resource throttle like a rate limit, where no queued
  item is at fault) — but the same uncapped path applied to a failure that keeps recurring for one
  specific payload, which never reached a terminal state and stalled that message's classification
  behind it forever. That class of error is now counted against the row's own attempts and capped
  by a new `semantic.max_attempts` setting, mirroring how the pipeline runner already caps a
  `StageTransient` failure — a rate limit alone stays uncapped.
- **A burst of PostIMAP events could spawn thousands of concurrent, unreferenced asyncio tasks.**
  The NOTIFY listener spawned one task per event per handler with no reference kept anywhere, so a
  sync burst ran unbounded concurrency against a connection pool of roughly fifteen, starving the
  HTTP handlers sharing it — and an unreferenced task is eligible for garbage collection mid-flight,
  which could silently drop the event it was dispatching. The listener now feeds a bounded queue
  drained by a fixed number of dispatch workers, so concurrency stays capped and every queued event
  is held by the queue itself until a worker picks it up.
- **A listener reconnect never told any client to resync.** `docs/architecture.md` documented this
  safeguard, but the listener's reconnect path only logged; the only `resync` event in the codebase
  came from a browser's own SSE reconnect replaying a stale `Last-Event-ID`, which never covers a
  browser whose SSE connection stayed up throughout the gap. Any NOTIFY fired while the listener's
  database connection was down is gone for good, so a client with a healthy stream could hold
  stale mail indefinitely. The listener now broadcasts a `resync` to every account once its
  connection recovers.

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
