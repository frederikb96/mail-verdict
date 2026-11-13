# Features

Everything MailVerdict does, area by area. The [README](../README.md) has the overview;
[architecture.md](architecture.md) has how each part works underneath.

- [Mail](#mail)
- [Compose](#compose)
- [Actions and the keyboard](#actions-and-the-keyboard)
- [Search](#search)
- [Spam verdicts](#spam-verdicts)
- [Orders and tickets](#orders-and-tickets)
- [Rules](#rules)
- [Glacier storage](#glacier-storage)
- [Notifications and alerts](#notifications-and-alerts)
- [Calendars and contacts](#calendars-and-contacts)
- [Privacy](#privacy)
- [MCP server](#mcp-server)
- [Installable](#installable)
- [Keyboard shortcuts](#keyboard-shortcuts)

## Mail

- Multiple accounts, a folder tree with live counts, conversation threading, and a virtualized
  list that stays smooth on large mailboxes.
- **Unified views:** any set of folders, across accounts, merged into one list with an icon of its
  own in the sidebar. One folder can sit in several views.
- A conversation reads newest first. Any earlier message in it can be made the open one, so the
  toolbar and the reply act on that message instead of the newest. An open conversation updates
  live: a reply arriving, or your own sent reply, shows up in it without reopening it.
- A message header's sender and recipients copy their addresses on a click, each alone or a whole
  line comma-separated.
- Folders can be created and deleted. Renaming and re-nesting are an IMAP limitation and not
  implemented ([Known limitations](../README.md#known-limitations-deliberately)).
- A quick filter narrows the open folder or view by subject, sender or recipient. Its rows carry
  the same actions an ordinary row does, and a toggle beside it shows only unread mail.
- Opening a reply collapses the quoted original behind a "Show quoted text" control. Ctrl+F (or a
  control beside the message's other icons) searches and highlights matches inside the open
  message, which the browser's own find cannot reach.
- An image or PDF attachment opens full screen, rendered rather than downloaded. The download
  control stays for that and for anything the preview does not recognise.

## Compose

- A rich-text editor with Markdown input rules, tables, checklists and pasted images. Send, reply,
  reply-all, forward and drafts, with attachments. The panel resizes by dragging its top edge or
  expands to fill the window.
- A reply or forward embeds the original as a collapsible quote, and replies thread correctly.
- **Sending identity:** chosen for you — whichever address the original arrived at, or the
  account's starred default for a fresh message. It is shown, and changeable, in one From control
  spanning every account, which always names the exact address (`Name <address>`), not just the
  account.
- Reopening a draft continues editing it in place, and sending one leaves no draft behind.
- **Send exactly once:** pressing Send takes the message out of the composer at once and sends it
  exactly once — the server refuses a repeat whatever the browser does — and a failure brings the
  composer back as it was.
- **Undo send:** a send can be undone for a few seconds after pressing Send. It is held durably on
  the server rather than in the browser, and Undo reopens the composer with everything that was
  written, attachments and pasted images included.
- Any message can be downloaded as a raw `.eml` file, or its link copied. The link opens that
  message wherever it has been filed since.

## Actions and the keyboard

- Read/unread, flag, archive, trash, permanent delete (with confirmation, since it is
  irreversible), keywords, drag-and-drop moves (a long press selects instead of dragging on a
  touch device) and a type-to-filter "Move to..." picker in the reading pane.
- Bulk actions over a selection or a whole folder, threaded or not.
- **Keyboard:** `j`/`k` and the arrow keys move the reading pane between messages, `c` composes,
  `Shift+R` replies, `a` replies all, `f` forwards, `v` moves the open message, `e` archives,
  `Delete` or `#` trashes, `!` marks spam, `r` toggles read and unread, `s` stars, and Ctrl+Z
  (Cmd+Z on a Mac) takes back the last few actions one by one. Whatever takes the open message out
  of the list opens the next one in the direction you were already going. With more than one
  message ticked, archive, trash, spam, read, unread, star and move act on the whole selection,
  the same as the bulk toolbar's buttons. The rest of the keyboard is under
  [Keyboard shortcuts](#keyboard-shortcuts).
- **Offline-tolerant:** every action shows at once and survives a flaky or absent connection. It
  waits, retries and is sent exactly once, even across a reload, with a marker on the row while
  the server has not confirmed it and a Retry when the server refuses. Sent late, or undone, an
  action never pulls a message out of a folder it was filed into meanwhile, and one left unsent
  for an hour asks before it is sent at all.
- Archiving a message, or filing it as spam, marks it read as it moves. Mail in Archive or Trash
  stays read even when another client put it there; a setting turns this off for Archive and Junk.
- **Retention:** an account can have a Trash retention and a Junk retention, each in days and set
  independently. Mail sitting in either that long is permanently removed on its own.

## Search

- A field in the top header on every page (`/` focuses it) jumps straight to a result.
- **Text search** is scoped to whichever accounts, folders and fields (subject, from, to, body)
  you pick. Results rank by whether the word itself matched rather than merely started a longer
  one, then by where the match lands, newest first within that. A typo-tolerant fallback catches a
  query the primary match misses entirely.
- **Semantic search** runs over an embedding of every message, for finding mail by meaning rather
  than exact words: the likely hits first, then a "Less likely" group, each newest first, with
  nothing to tune. Text matches show at once and the list by meaning replaces them when it
  arrives.
- A clock button narrows either search to a time window: today, since yesterday, the last 7 or 30
  days, a slider over the last 52 weeks, or a custom range of days.
- Both remember the account and folder scope you chose, independent of whichever account the
  sidebar shows. Opening a result lands on that message in its folder wherever it sits, however far
  back.

## Spam verdicts

- Each new message is classified by a language model, with the reasoning visible and a correction
  loop when it gets one wrong.
- A review screen lists every message currently called spam with no ruling yet, across every
  account and folder including Junk, for confirming or correcting them singly or in bulk.
- Historical mail is never classified, and nothing is classified twice.

## Orders and tickets

- Mail about a purchase, ticket or booking — confirmation, invoice, payment, shipping, carrier
  notices, delivery, return, refund, customer-service conversation — is bundled into one entry
  with an AI-written title, status and summary, across every account of yours that has the
  feature switched on, in a screen of its own. Entries and files are each user's own: two people
  receiving mail about the same purchase get two entries, and nobody sees another's.
  An admin giving an account to someone else moves its entries along, or takes its mail out of
  entries that also hold mail of the previous owner's other accounts.
- A carrier notice or payment receipt with no order number of its own still finds the right entry
  by merchant, timing and what is already in flight. A mail is attached to an existing entry or
  opens a new one, never split between two afterwards. A shipment number that only a carrier's
  tracking link carries is read from the link.
- When the bundling goes wrong, a mail can be moved to another entry or removed, two entries
  merged, or the summary rewritten by hand.
- Each entry is a ticket, an order or something else (a subscription, a claim), set by the model
  and overridable by hand.
- **Attachments are read too:** the text of PDFs (scans by OCR), Wallet passes, calendar files,
  e-invoices and images, and every QR, Aztec or bar code, which the entry shows as an image to
  scan. A photo OCR cannot read, such as a photographed ticket, is transcribed by a vision model
  when one is set. A file can be added to an entry directly, or opened as a new one, without any
  mail.
- Entries can be marked as favorites, narrowed by a typo-tolerant text filter, closed or reopened
  by hand, and sealed so the agent adds no more mail to them. An open entry nothing is expected of
  any more is closed automatically, a few days (7 by default) after the date the model estimates
  it is over, or 30 days after its last mail when it has no such date; both are settings.
- The same actions sit in the detail's menu and in a menu on each row (right-click or long
  press). On a touch screen a row swipes: right to left for favorite, left to right for closed or
  open.
- The screen shows all entries, only orders or only tickets, with Open and Favorites toggles on
  top; the choice is remembered. Tickets form a timeline around a today line, later events above
  and earlier ones below, with a Today button to jump back to it. A ticket's codes open fullscreen
  on a white page for scanning.
- Recent mail can be looked through after switching the feature on for an account.

## Rules

- Every user has their own rules, which run only on mail of their own accounts. A new user starts
  with spam classification, moving spam to Junk and marking it read, and order bundling. An
  administrator sees how many rules each user has and whether they are paused, never what they
  say.
- Conditions over incoming mail with actions that move, tag, flag or delete it, or send it to a
  webhook.
- **The Rules page** (sidebar, `Ctrl+Alt+0`) lists a user's rules grouped by purpose -- Spam,
  Filing, Forwarding, Orders & tickets -- each as a sentence ("If sender domain **shop.example** and
  has a **pdf** attachment → move to **Receipts**, mark read") with its accounts, a switch and a
  menu (edit, duplicate, move up or down, test with a mail, delete). Rules reorder by dragging or
  from the menu, a filter narrows the list, and a rule whose target folder does not exist shows a
  warning on its row. Spam detection and orders & tickets are switched per account at the top of
  the page. The menu beside the buttons tests every rule against a recent mail, opens the history
  of earlier versions and pauses or resumes all rules.
- **The rule editor** is a form of conditions (sender, subject, body, header, attachment, size,
  folder, tag, spam verdict; all or any, each negatable, one group level) and actions (move to a
  folder, mark read or unread, star, keyword, tag, trash, delete permanently, notify), with the
  accounts it applies to and a stop switch. "Edit as JSON" is the advanced view of the same rule;
  a rule the form cannot show exactly (a webhook, an unusual condition) opens as JSON instead of
  being simplified. A rule that deletes permanently asks for a confirmation when saved.
- **Webhooks:** the message's raw source is posted to a URL, with a header that can take its value
  from a named secret stored encrypted in the database (set under Admin → Secrets, never shown again). A
  delivery is queued, retried when the receiver is unavailable, never repeated once it succeeded,
  and raises an alert when it gives up. Mail that predates a rule can be sent once, oldest first,
  through the API. Only an administrator may write a webhook rule.
- **Rule assistant:** from an open mail, the "Add rule" icon -- or "Describe a rule" on the Rules
  page, which first asks for a recent mail the rule is about -- takes a sentence ("these should go to
  Newsletter too", "split this rule by sender") and proposes one change to the rules, which may
  add, change, reorder or remove several rules at once. It shows the explanation, each affected
  rule as it is and as it would be, and what the touched rules would have caught among the last
  100 mails, for Accept or Decline as a whole, with each rule also written as a sentence. Nothing
  is kept, and it never proposes permanent deletion or a webhook.

## Glacier storage

- A per-account place mail can be moved to where it leaves the mail server for good and lives on
  only in MailVerdict's own database. It is listed, searched and included in unified views like
  any folder, since moving into or out of it is the ordinary move action.
- Read, star, keyword, reply and forward keep working, since none of them need the server.
- Moving a message back to a real folder restores it there with its original flags and, for the
  overwhelming majority of mail, its original date. The date is derived from the message's own
  `Date` header rather than the server's original arrival stamp, so the two can diverge for a
  message whose header was wrong or missing.
- Turning a glacier off, or deleting its account, is refused while it still holds anything.
- A number of days set on the account moves its archived mail in automatically, in small paced
  batches, once a message has sat there that long.

## Notifications and alerts

- **Write failures:** a durable, acknowledgeable record of any write that never reached the mail
  server, including a send that never left, surfaced with the reason and a live update the moment
  it happens. A message still waiting on its way out long after it should have gone raises an
  alert of its own, which clears itself once the message is sent or the send is undone. These are
  system notifications: always counted on the bell, and listed with the write failures.
- **New mail:** raises a bell with a durable, dismissable list, and — once notifications are turned
  on for a device — a system notification that reaches it even with no MailVerdict page open,
  using the browser's own Web Push. Which folders alert is a per-device setting; every registered
  device can be reviewed and removed under Settings → Notifications.
- Web Push requires a home-screen install on iOS and iPadOS, and stops the moment the browser
  itself is quit on a desktop. The durable list is what a closed tab or a declined permission falls
  back to.
- An alert resolves itself once its mail is read anywhere, and a setting keeps new-mail alerts out
  of the bell's badge so it counts only system notifications. Calendar reminders are not delivered
  as alerts.
- Everything here is per user: each person sees and dismisses only the alerts of their own
  accounts, their devices are theirs alone, and a phone or browser someone else signs in on moves
  to them. An AI provider outage is an alert for the admins.

## Calendars and contacts

- CalDAV and CardDAV servers are mirrored the same way mail is: calendars with recurring events,
  RSVPs and per-occurrence editing, and address books with compose autocomplete.
- An emailed invitation is parsed and offered for import on its own, and a reply goes back over
  the identity's own outbox rather than the server's scheduling engine.
- Every user sees only the CalDAV and CardDAV accounts they added: their calendars, events,
  address books and contacts, and an identity-to-calendar mapping of their own. An invitation
  that arrives in someone's mailbox is imported only into that user's calendars.
- An event can carry any number of reminders and say whether it makes you free or busy. A
  calendar's own default reminder can override or switch off the global one.
- Dates and times are entered and shown day-first in a 24-hour clock, in an app-owned control
  rather than the browser's own.

## Privacy

- **Remote images** are blocked by default, with a per-sender and per-domain allowlist.
- **Avatars:** a sender's avatar shows their address-book photo when one exists, and initials
  otherwise. An embedded photo is served from MailVerdict's own endpoint, never a third-party
  request, and a remote one follows the same allowlist as any other remote image. There is never a
  lookup against an unrelated third party.
- **Message HTML**, including a sender's own stylesheet, is sanitized on the server and rendered in
  an isolated shadow root: every escaping declaration dropped, every remote reference gated behind
  the image allowlist. Opening a message from a sender that is not allowlisted makes no request
  anywhere, and a read-receipt request is never answered. An allowlisted sender's images and
  backgrounds load, while active content (frames, scripts, media, forms) and anything that escapes
  the message's box stay blocked for everyone.
- **Dark mode:** in the dark theme a message opens dark when it declares its own dark-mode support,
  with the sender's own dark styles applied, or when its own colours read safely on dark. In the
  light theme every message opens light. A toggle in its header always overrides either default.
- Hovering a link shows where it leads.
- **What reaches the model provider.** The image block does not extend to the message body itself.
  A newly arrived message's subject, sender and a truncated body go to the configured model
  provider twice: once for spam classification and once for the embedding behind semantic search.
  A third time for orders and tickets, on an account with that feature on and a mail that passes
  its first filter. This is a deliberate design choice, not something a setting turns off. The
  rule assistant sends the open mail's excerpt, the rule set, and the sender and subject of a
  handful of other mails to the model provider each time it is used, and only then. Which
  provider each task uses is a setting ([Configuration](../README.md#configuration)).

## MCP server

Connect an MCP client at `/mcp` and let it search, read, organise and send mail — including
replying to or forwarding a message by its id, saved as a draft for you to review and send, with
attachments — and read, create, edit and delete calendar events and contacts. It wraps a curated
subset of the REST API ([api.md](api.md)) as typed tools.

## Installable

A browser that supports it offers to install the site, which then runs in its own window with its
own icon. The installed application registers as a `mailto:` handler, so it can be the machine's
mail client: a `mailto:` link opens the composer filled in from the link.

## Settings

- Each setting is either the instance's or the user's own. Admins set the machinery: models and
  providers, queues and their retry mechanics, secrets, webhooks, and the pipeline's and orders'
  worker settings. Everyone sets their own mail behaviour (marking mail read as it is filed,
  the bell badge, how long a new-mail alert waits for the rules), calendar defaults, undo-send,
  the language, first filter and automatic closing of orders, and the order their accounts are
  listed in. A user who has not changed one gets its default.
- An admin's own preferences are theirs alone; changing them changes nothing for anyone else.
- Anyone else is refused the machinery outright.
- Settings shows one category at a time, each at an address of its own: Appearance, Mail (filing,
  undo send, how orders are summarised and closed), Unified views, Notifications, Calendar,
  Accounts and Keyboard shortcuts. On a phone the list of categories is a screen of its own.
- Folders are chosen with one picker, for a unified view and for the folders that raise a
  notification: a filter, a switch hiding system folders (Drafts, Sent, Trash, Junk and folders
  the sidebar hides), and a collapsible group per account whose checkbox ticks everything it lists.
- *Admin* is a sidebar entry for administrators only. It holds the people and the machinery: Users
  (role, accounts by name, last seen; make or remove admin, disable or enable sign-in, rename,
  pre-create a user, reassign a mail or calendar account to another user -- never anyone's mail),
  Models (one row per
  AI task with provider, model, reasoning effort and token budget), Providers & keys, Processing
  (queues as compact tiles with their controls in a menu, failed runs, and each user's rule set as
  counts only), Secrets and System (usage export, semantic index coverage, queue tuning). Anyone else who opens
  an Admin address is told it is for administrators.

## Keyboard shortcuts

- From any page, `Ctrl+Alt+1` to `Ctrl+Alt+9` jump to mail, calendar, contacts, orders & tickets,
  spam review, the search page, accounts, admin processing (administrators only) and settings,
  and `Ctrl+Alt+0` to the rules. `/`
  focuses search, `Ctrl+B` shows or hides the sidebar, and `?` lists every shortcut.
- The calendar has its own (`t` today, `d`/`w`/`m`/`a` views, `n` new event, `j`/`k` and the
  arrows for the next and previous period), and orders & tickets step through the list with
  `j`/`k`. The mail list's are under [Actions and the keyboard](#actions-and-the-keyboard).
- Settings → Keyboard shortcuts rebinds, switches off or resets any of them, kept per browser. A
  chord already in use offers to take it over; keys the browser or text editing owns are refused.
- Letters follow what is printed on the key, whatever the layout, so `z` is the key labelled Z on
  a German keyboard too; `#`, `!`, `?` and `/` match the character typed, with or without Shift.
  In a text field, typed characters and editor chords such as `Ctrl+B` stay with the field, and a
  `Ctrl+Alt` chord is ignored when it is AltGr typing a character there.
