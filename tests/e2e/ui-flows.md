# UI Test Flows

Comprehensive browser-automation test flows for MailVerdict UI.

**Base URL:** `http://127.0.0.1:18080` (test containers via compose.test.yaml)  
**Mobile breakpoint:** `< 768px` viewport width  
**Test accounts:** Alice (`alice@test.local`), Bob (`bob@test.local`)

---

## Page Load & Initial State

### Flow 1: Fresh Load — No Accounts

**Setup:** Clean database, no accounts registered.  
**Steps:**
1. Navigate to `/`
2. Sidebar renders with `SidebarTrigger` button in the top bar and a `ConnectionIndicator` (green/yellow/red dot)
3. Sidebar header shows "Select Account" text and a `ChevronDown` icon (the account dropdown trigger)
4. Sidebar folder group label shows "Folders"
5. Sidebar shows empty state text: "Select an account to view folders"
6. Main content area (mail list) shows empty state with `InboxIcon` (h-12 w-12 opacity-50) and text "Select an account to view messages"
7. Reading pane shows empty state with `Mail` icon (h-16 w-16 opacity-30) and text "Select a message to read"
8. Sidebar footer contains three nav links: "Search" (`/search`), "Accounts" (`/accounts`), "Settings" (`/settings`)

**Screenshots:** Full page layout, sidebar footer nav, mail list empty state

---

### Flow 2: Fresh Load — With Seeded Account

**Setup:** Alice account registered and synced (ACTIVE state), inbox contains mails from E2E seeding.  
**Steps:**
1. Navigate to `/`
2. Sidebar header auto-selects Alice account — displays account name "alice" with optional emoji or `Mail` icon
3. Sidebar folder list populates with folders (Inbox, Sent, Trash, Junk, Drafts at minimum) — each folder has an icon matching its `special_use` type (Inbox icon, Send icon, Trash2 icon, etc.)
4. Inbox folder is auto-selected (highlighted with `isActive` state via `data-active` attribute on `SidebarMenuButton`)
5. Folders with unread messages show a `Badge` (variant="secondary") with unread count
6. Mail list loads with skeleton placeholders (8 skeleton rows with rounded-full avatar + text skeletons), then actual mail items appear
7. Each mail item shows: sender avatar (initials in `AvatarFallback`), sender name, relative date, subject line (truncated via `truncate` class), optional snippet (via `line-clamp-1`)
8. Unread mails have: blue dot (h-2 w-2 rounded-full bg-blue-500), bold sender name (`font-semibold`), subtle background (`bg-accent/20`)
9. Desktop layout: mail list panel (w-[400px]) on left, reading pane on right separated by `border-r`
10. Connection indicator in top bar shows green dot with "Connected" text (on `sm:` screens)

**Screenshots:** Full desktop layout with mail list, sidebar with folders and counts, individual mail item detail

---

### Flow 3: Fresh Load — Loading States

**Setup:** Alice account registered, app starting up (or slow network simulated).  
**Steps:**
1. Navigate to `/`
2. While folders are loading: sidebar shows "Loading folders..." text with a spinning `RefreshCw` icon (h-3 w-3 animate-spin)
3. While mail list is loading: 8 skeleton placeholder rows visible, each with `Skeleton` components (rounded-full h-8 w-8 for avatar, h-4 w-32 for name, h-3 w-48 for subject, h-3 w-64 for snippet)
4. Once loaded: skeletons are replaced with actual mail items
5. Accounts page (`/accounts`) shows loading skeletons: one h-8 w-48 for title, three h-48 w-full for account cards
6. Settings page (`/settings`) shows loading skeletons: h-8 w-48 for title, h-64 w-full for content

**Screenshots:** Mail list skeleton state, sidebar loading state

---

## Account Management

### Flow 4: Add New Account

**Setup:** App running, navigate to accounts page.  
**Steps:**
1. Navigate to `/accounts`
2. Page title "Accounts" (h1, text-2xl font-semibold) is visible
3. Click "Add Account" button (contains Plus icon + text "Add Account") — opens a Dialog
4. Dialog title shows "Add Account"
5. Form contains fields: Account Name (required), IMAP Host (required), IMAP Port (required, default 993), IMAP User (required), IMAP Password, SMTP Host, SMTP Port (placeholder "587"), SMTP User, SMTP Password, Sync Lookback (default 180), "Enable spam detection" checkbox
6. Fill in: name="bob", imap_host="stalwart", imap_port=1143, imap_user="bob@test.local", imap_password="testpass123", smtp_host="stalwart", smtp_port=2525
7. Click "Create" button
8. Dialog closes
9. New account card appears with: name "bob", state badge "Created" (variant="outline"), IMAP and SMTP connection details displayed in a 2-column grid
10. Account card shows action buttons: "Sync" (with RefreshCw icon), "Test" (with Plug icon), "Edit" (with Pencil icon), "Delete" (with Trash2 icon, text-destructive class)

**Screenshots:** Account form dialog, new account card

---

### Flow 5: Edit Account & Test Connection

**Setup:** Bob account exists from Flow 4.  
**Steps:**
1. Navigate to `/accounts`
2. On Bob's account card, click "Edit" button
3. Dialog opens with title "Edit Account"
4. Form is pre-filled with existing values (name="bob", imap_host="stalwart", etc.)
5. Password fields show placeholder "(unchanged)" instead of actual passwords
6. Change account name to "Bob Mail"
7. Click "Update" button — dialog closes, card title updates to "Bob Mail"
8. Click "Test" button on Bob's card — button shows Loader2 spinner (animate-spin) while pending
9. On success: green text "Connection successful" appears below action buttons
10. On failure: red text "Connection failed: [error message]" appears (class text-destructive)

**Screenshots:** Edit dialog with pre-filled fields, test connection success/failure states

---

### Flow 6: Account Card Collapsible Sections

**Setup:** Alice account exists and is ACTIVE.  
**Steps:**
1. Navigate to `/accounts`
2. Alice's account card shows collapsible sections below the action buttons (separated by border-t): "Folder Assignment", "Folder Order & Visibility", "IMAP IDLE", "Image Exceptions", "Unified View Names"
3. Each section has a trigger button with: icon (FolderInput/GripVertical/Radio/ImageOff/Layers), label text, ChevronDown indicator that rotates 180deg when open (`[data-panel-open]>svg:last-child]:rotate-180`)
4. Click "Folder Assignment" — panel expands, showing FolderAssignment component
5. Click "Folder Assignment" again — panel collapses
6. Click "Folder Order & Visibility" — panel expands, showing FolderOrder component with drag handles
7. Click "Image Exceptions" — panel expands, showing ImageExceptionsList component
8. Click "Unified View Names" — panel expands, showing UnifiedNames component with EmojiPicker

**Screenshots:** Expanded folder assignment, folder order with drag handles

---

### Flow 7: Switch Between Accounts

**Setup:** Two accounts (Alice, Bob) registered and synced.  
**Steps:**
1. Navigate to `/`
2. Sidebar header shows Alice account name in the dropdown trigger
3. Click the account dropdown trigger (SidebarMenuButton with ChevronDown)
4. Dropdown opens showing: "Unified View" item (with Layers icon), Alice item (with emoji or UserCircle icon + "current" label), Bob item (with emoji or UserCircle icon)
5. Click Bob — dropdown closes
6. Sidebar header updates to show "Bob" (or "Bob Mail")
7. Folder list refreshes to show Bob's folders
8. Previously selected folder ID is cleared (selectedFolderId set to null)
9. Inbox auto-selects for Bob's account
10. Mail list refreshes to show Bob's inbox messages
11. Selected mail is cleared (reading pane shows "Select a message to read")

**Screenshots:** Account dropdown open, sidebar after switch

---

## Folder Navigation

### Flow 8: Click Different Folders

**Setup:** Alice account with inbox containing messages, other folders may be empty.  
**Steps:**
1. Navigate to `/` — inbox is auto-selected
2. Mail list shows inbox messages
3. Click "Sent" folder in sidebar — the Sent folder button gets `isActive` state, Inbox loses it
4. Mail list refreshes: if Sent has messages, they appear; if empty, shows "No messages in this folder" text with InboxIcon
5. Selected mail is cleared (selectedMailId set to null via handleFolderSelect)
6. Click "Trash" folder — similar refresh behavior
7. Click "INBOX" folder — returns to inbox messages
8. Verify folder icons match special_use: Inbox icon for inbox, Send icon for sent, Trash2 icon for trash, AlertTriangle for junk, FileEdit for drafts, Archive icon for archive, generic Folder icon for custom folders

**Screenshots:** Each folder selected state, empty folder display

---

### Flow 9: Folder Unread Counts Update via SSE

**Setup:** Alice account synced, inbox has unread messages.  
**Steps:**
1. Navigate to `/` — observe Inbox folder shows unread Badge with count (e.g., "3")
2. Badge styling: variant="secondary", class "ml-auto h-5 min-w-5 justify-center px-1 text-xs"
3. Click a mail in the list — it gets marked as read via API (is_read becomes true)
4. After SSE event propagates: unread dot disappears from that mail item, inbox badge count decrements
5. If all mails read, badge disappears entirely (badge only renders when unread_count > 0)

**Screenshots:** Badge before and after reading mail

---

### Flow 10: Navigate from Non-Mail Pages to Folder

**Setup:** Alice account synced with mails.  
**Steps:**
1. Navigate to `/settings`
2. Click a folder in the sidebar (e.g., "INBOX")
3. Page navigates to `/` (router.push("/") in handleFolderSelect)
4. Mail list shows inbox messages
5. Navigate to `/search`
6. Click "Sent" folder in sidebar
7. Page navigates to `/` with Sent folder selected
8. Navigate to `/accounts`
9. Click "Trash" folder — returns to `/` with Trash selected

**Screenshots:** Navigation from settings to mail view

---

## Mail Interactions

### Flow 11: Select Mail — Reading Pane Content

**Setup:** Alice account, inbox has mails (including one with HTML body and attachments).  
**Steps:**
1. Navigate to `/` — mail list visible, reading pane shows "Select a message to read"
2. Click a mail item in the list
3. Mail item gets selected state: `bg-accent border-l-2 border-l-primary` classes
4. Reading pane loading: shows skeleton (h-8 w-3/4 for subject, h-4 w-1/2, h-4 w-1/3, then h-64 w-full for body)
5. Reading pane loaded, header section (border-b p-4) shows:
   - Subject (h2, text-lg font-semibold)
   - Action buttons row: mark read/unread (MailOpen/MailIcon toggle), star/flag (Star), delete (Trash2) — each is Button variant="ghost" size="icon" className="h-8 w-8"
   - Sender name (font-medium) and email in angle brackets (text-muted-foreground)
   - "To:" line with recipient addresses
   - Date in full format (text-xs text-muted-foreground)
6. Auth badges row: DKIM/SPF/DMARC badges (green with ShieldCheck or red with ShieldX) + tag badges (variant="outline")
7. Email body rendered in Shadow DOM (via EmailRenderer): HTML content or plain text fallback with `pre` wrapping
8. If body not yet synced: shows spinning indicator "Loading message body..."
9. If attachments present: bottom section (border-t p-4) with Paperclip icon, count text, attachment chips showing filename (truncated max-w-40), size, and Download link icon

**Screenshots:** Full reading pane with all sections, auth badges, attachment chips

---

### Flow 12: Mail List Hover Actions

**Setup:** Alice account with multiple mails in inbox.  
**Steps:**
1. Navigate to `/`, select inbox
2. Hover over a mail item — hover actions appear (hidden by default, shown via `group-hover:flex`): Star, Archive, Spam (Ban), Delete (Trash2) buttons
3. Each hover action button: `rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors`
4. Avatar is hidden on hover (replaced by checkbox area via `group-hover:hidden`)
5. Click Star button on a mail — star icon changes to filled yellow (`fill-yellow-400 text-yellow-400`)
6. When not hovering, starred mails show a permanent Star icon (fill-yellow-400) on the right side
7. Click Archive button — mail disappears from inbox (moved to archive folder)
8. Click Spam (Ban) button — mail disappears from inbox (moved to junk folder)
9. Click Delete (Trash2) button — mail disappears from inbox (moved to trash folder)

**Screenshots:** Hover state with action buttons, starred mail indicator

---

### Flow 13: Reading Pane Actions

**Setup:** Alice account, a mail selected in reading pane.  
**Steps:**
1. In reading pane, current mail is flagged=false, is_read=true
2. Click Star button in header — Star icon changes to `fill-yellow-400 text-yellow-400`, mail is flagged on server
3. Click Star button again — Star reverts to default (unflag)
4. Click Mark Unread button (MailIcon) — button icon changes to MailOpen, mail's unread dot reappears in the list
5. Click Mark Read button (MailOpen) — button icon changes to MailIcon, unread dot disappears
6. Click Delete button (Trash2) — mail is moved to trash, next mail in list should be selected or reading pane shows empty state
7. If mail has blocked images: ImageBanner appears (bg-amber-500/10) with "Remote images blocked for privacy" and three buttons: "Load for this message", "Always from [email]" (with Shield icon), "Always from @[domain]" (with Shield icon)
8. Click "Load for this message" — images load for current view only (local state `loadImagesForMessage`)

**Screenshots:** Star toggle, read/unread toggle, image blocking banner

---

### Flow 14: Multi-Select and Bulk Actions

**Setup:** Alice account, inbox with 5+ mails.  
**Steps:**
1. Navigate to `/`, inbox selected
2. Hover over first mail — checkbox appears (replacing avatar) on the left side
3. Click the checkbox (not the mail row) — checkbox becomes checked, mail gets `bg-accent/70` background
4. BulkToolbar appears above mail list: shows Badge "1 selected" (variant="secondary"), action buttons (Move to dropdown, Archive, Star, Spam, Delete), and clear (X) button
5. Hover and click checkbox on second mail — "2 selected" badge, both mails highlighted
6. Click "Archive" in bulk toolbar — both mails disappear from inbox, BulkToolbar disappears (selection cleared)
7. Multi-select again: click checkbox on mail A, then shift-click checkbox on mail C — range selection (all mails between A and C become selected)
8. Click "Delete" in bulk toolbar — all selected mails moved to trash
9. Bulk "Move to" dropdown: click "Move to" → dropdown shows all folders from folderOrder, click target folder → mails move
10. Click X button in bulk toolbar — selection clears, toolbar disappears, checkboxes hidden again

**Screenshots:** Bulk toolbar with selection count, multi-selected mails, Move-to dropdown

---

## Search

### Flow 15: Fulltext Search

**Setup:** Alice account synced, inbox has mails with various subjects.  
**Steps:**
1. Navigate to `/search`
2. Page title "Search" (h1, text-2xl font-semibold) visible
3. Search input with SearchIcon on left (absolute positioned, pl-9), placeholder "Search messages..."
4. Mode toggle: two buttons in a bordered container (rounded-md border) — "Fulltext" (FileText icon, variant="secondary" = active) and "Semantic" (Brain icon, variant="ghost" = inactive)
5. Below input: empty state shows "Enter at least 2 characters to search" with SearchIcon (h-12 w-12 opacity-50)
6. Type a query (e.g., a known subject word) with 2+ characters
7. Loading state: 5 skeleton rows (h-20 w-full each)
8. Results appear: text "N result(s) for [query]" (text-sm text-muted-foreground), followed by result cards
9. Each result card (Card component): subject (truncate font-medium), relative date (ml-auto text-xs), sender name below (text-sm text-muted-foreground)
10. If no results: shows "No results found" with SearchIcon

**Screenshots:** Search with results, empty query state, no results state

---

### Flow 16: Semantic Search with Score

**Setup:** Alice account synced, embeddings indexed (requires OPENAI_API_KEY).  
**Steps:**
1. Navigate to `/search`
2. Click "Semantic" mode button — it gets variant="secondary", "Fulltext" becomes variant="ghost"
3. Type a natural language query (e.g., "meeting tomorrow")
4. Results load — each result card has an additional score badge on the right: `rounded bg-secondary px-1.5 py-0.5 text-xs` showing percentage (e.g., "87%")
5. Results are sorted by relevance score (highest first)

**Screenshots:** Semantic results with score badges

---

### Flow 17: Search Scope — Account vs Unified

**Setup:** Two accounts (Alice, Bob) with mails.  
**Steps:**
1. In sidebar, select Alice account
2. Navigate to `/search`
3. Search for a term — results only from Alice's account (searchAccountId filters to selectedAccountId)
4. Switch to Unified View via sidebar dropdown
5. Search same term — results from ALL accounts (searchAccountId is undefined)
6. Unified results may show more items than single-account search

**Screenshots:** Search results scoped to account vs all

---

## Settings

### Flow 18: Settings Page — Theme & Categories

**Setup:** App running.  
**Steps:**
1. Navigate to `/settings`
2. Page title "Settings" (h1) visible
3. First card: "Appearance" (CardTitle text-base) with theme toggle — three buttons: Light (Sun icon), Dark (Moon icon), System (Monitor icon)
4. Active theme button has variant="default", others have variant="outline"
5. Click "Dark" — page theme switches to dark mode immediately, "Dark" button becomes variant="default"
6. Click "Light" — page switches to light mode
7. Click "System" — follows system preference
8. Below Appearance: UnifiedOrder card for cross-account folder ordering
9. Below that: Tabs component with 5 tabs: AI (Bot icon), Spam (ShieldAlert icon), Sync (RefreshCw icon), Retry (Repeat icon), Rules (FileCode icon)
10. Default tab is "ai" — shows CategorySettings with fields for AI configuration
11. Settings fields render by type: boolean → checkbox, number → number input, string → text input (or password for key/password/secret fields), object → Textarea with JSON
12. "api_key" field has "locked" Badge (variant="outline") and is disabled
13. Click "Spam" tab — shows spam-related settings
14. Modify a numeric value → "Save" button appears (with Save icon), click → saves via API, button disappears

**Screenshots:** Settings page with theme toggle, AI tab active, Save button appearing

---

## Unified View

### Flow 19: Switch to Unified View

**Setup:** Two accounts (Alice, Bob) registered, both synced, unified folder names configured.  
**Steps:**
1. Navigate to `/`
2. Click account dropdown in sidebar header
3. Click "Unified View" — dropdown closes
4. Sidebar header shows "Unified View" with Layers icon
5. Sidebar folder group label changes to "Unified Folders"
6. Unified folders appear (e.g., "Inbox", "Sent") — each with Layers icon, unified_name text, and aggregate unread badge
7. Click unified "Inbox" folder
8. Mail list shows merged mails from all accounts, sorted chronologically

**Screenshots:** Unified view sidebar, merged mail list

---

### Flow 20: Unified View — Emoji Identifiers

**Setup:** Unified view active, Alice has emoji set (e.g., "A"), Bob has emoji (e.g., "B").  
**Steps:**
1. In unified view, inbox selected
2. Mail items use `UnifiedMailItem` component (instead of `MailListItem`)
3. Each mail shows account emoji in two places:
   - Inline emoji badge: `span` with `text-xs title="Source account"` before the unread dot, within the content row
   - Avatar overlay: `span absolute -bottom-1 -right-1 text-xs` on the avatar (visible when not hovering)
4. Different mails show different emojis based on their source account
5. Hover over a unified mail item — same action buttons as regular view (Star, Archive, Spam, Delete) with an additional `mailAccountId` parameter passed to onAction

**Screenshots:** Unified mail items with emoji badges, mixed accounts

---

### Flow 21: Unified View — No Folders Configured

**Setup:** Unified view active, but no unified folder names assigned to any account folders.  
**Steps:**
1. Switch to Unified View via dropdown
2. Sidebar shows "No unified folders configured" text (px-4 py-3 text-sm text-muted-foreground)
3. No folder items in the sidebar
4. Mail list area behavior depends on folder selection state

**Screenshots:** Empty unified folders state

---

### Flow 22: Switch Back to Single Account

**Setup:** Currently in Unified View.  
**Steps:**
1. Click account dropdown
2. Dropdown shows "Unified View" with "current" label
3. Click Alice account
4. Sidebar header changes back to Alice's name/emoji
5. Folder group label changes back to "Folders"
6. Folder list shows Alice's individual folders (not unified folders)
7. Folder, mail selection, and unified folder atom all reset to null

**Screenshots:** Transition from unified to single account

---

## Mobile Viewport

### Flow 23: Mobile Layout — Sidebar as Sheet

**Setup:** Viewport width < 768px (mobile breakpoint), Alice account synced.  
**Steps:**
1. Navigate to `/` at mobile viewport (e.g., 375x812)
2. Sidebar is hidden (rendered as Sheet overlay, not inline)
3. Top bar shows SidebarTrigger button (hamburger icon)
4. Click SidebarTrigger — sidebar opens as Sheet overlay (SheetContent) from the left side
5. Sheet shows same content as desktop sidebar: account dropdown, folder list, footer nav links
6. Click a folder (e.g., "Inbox") — sidebar behavior depends on implementation (may close sheet via setOpenMobile)
7. Mail list is full-width (no reading pane visible on mobile)

**Screenshots:** Mobile with sidebar closed, sidebar sheet open, full-width mail list

---

### Flow 24: Mobile — Mail List to Reading Pane Navigation

**Setup:** Mobile viewport, Alice account, inbox has mails.  
**Steps:**
1. At mobile viewport, navigate to `/`
2. Only mail list is visible (full screen, no reading pane — isMobile=true and selectedMailId=null)
3. Click a mail item — `selectedMailId` is set
4. View switches to reading pane (full screen): back button bar at top with `ArrowLeft` icon + "Back" text, reading pane below
5. Back button bar: `border-b px-2 py-1`, Button variant="ghost" size="sm" with gap-1 class
6. Reading pane shows full mail detail (same content as desktop reading pane)
7. Click "Back" button — returns to mail list (selectedMailId set to null), reading pane hidden

**Screenshots:** Mobile mail list, mobile reading pane with back button

---

### Flow 25: Mobile — Sidebar Navigation Between Pages

**Setup:** Mobile viewport.  
**Steps:**
1. At mobile viewport, open sidebar (click SidebarTrigger)
2. In sidebar footer, click "Accounts" link
3. Page navigates to `/accounts` — full-width accounts page visible
4. Open sidebar again, click "Settings"
5. Page navigates to `/settings` — full-width settings page
6. Open sidebar again, click "Search"
7. Page navigates to `/search` — full-width search page
8. Open sidebar again, click a folder — navigates to `/` with that folder selected

**Screenshots:** Mobile accounts page, mobile settings page

---

### Flow 26: Mobile — Scroll Behavior

**Setup:** Mobile viewport, Alice account, inbox has 20+ mails.  
**Steps:**
1. At mobile viewport, inbox selected
2. Mail list uses VList (virtual scrolling) — initially renders visible items only
3. Scroll down in mail list — new items render as they come into view (itemSize=76px each)
4. Near bottom (scrollSize - offset - viewportSize < 200): infinite scroll triggers `fetchNextPage` if `hasNextPage` is true
5. Loading spinner appears at bottom: Loader2 icon (h-4 w-4 animate-spin text-muted-foreground) centered in a py-3 container
6. Continue scrolling — more mails load until has_more is false

**Screenshots:** Mail list scrolling, bottom loading indicator

---

## Keyboard Shortcuts

### Flow 27: Keyboard Navigation & Actions

**Setup:** Desktop viewport, Alice account, inbox with mails, no input field focused.  
**Steps:**
1. Navigate to `/`, inbox selected, mails loaded
2. Press `j` key — focused mail index increments, focused mail gets `ring-2 ring-inset ring-ring` highlight, VList scrolls to keep focused item visible
3. Press `j` multiple times — focus moves down through the list
4. Press `k` key — focus moves up (index decremented, minimum 0)
5. Press `Enter` — focused mail opens in reading pane (selectedMailId set)
6. Press `Escape` — reading pane clears (selectedMailId set to null)
7. Press `s` — focused mail's star/flag toggles (flag if unflagged, unflag if flagged)
8. Press `e` — focused mail is archived (disappears from inbox)
9. Press `!` (shift+1) — focused mail marked as spam
10. Press `#` (shift+3) — focused mail deleted
11. Press `x` — focused mail's selection checkbox toggles (enters selection mode)
12. Press `r` — focused mail marked as read
13. Press `u` — focused mail marked as unread
14. While typing in search input (INPUT/TEXTAREA/SELECT elements): all shortcuts are suppressed (isEditableElement check)

**Screenshots:** Focused mail with ring highlight, keyboard action results

---

## Drag and Drop

### Flow 28: Drag Mail to Folder

**Setup:** Desktop viewport, Alice account, inbox with mails.  
**Steps:**
1. Navigate to `/`, inbox selected
2. Start dragging a mail item — requires 5px movement (PointerSensor activationConstraint)
3. Dragged item becomes semi-transparent (opacity: 0.5)
4. DragOverlay appears: a floating card with GripVertical icon + "1 message" text + shadow-lg
5. Drag over a folder in the sidebar — `DroppableFolder` activates: folder gets `ring-2 ring-primary/50 bg-primary/10` highlight
6. Drop on the folder — mail moves to that folder via API (disappears from current list)
7. If selection mode is active and dragged mail is in selection: DragOverlay shows "N messages" with Badge count, all selected mails move together on drop
8. If dropped outside a valid folder: nothing happens, mail returns to original position

**Screenshots:** Drag overlay, folder hover highlight, multi-drag badge

---

## Sync & Connection

### Flow 29: Sync Progress & SSE Connection

**Setup:** Alice account, trigger a manual sync.  
**Steps:**
1. Navigate to `/accounts`
2. On Alice's card, account state badge shows current state (e.g., "Active" with variant="secondary")
3. Click "Sync" button (RefreshCw icon) — triggers startJob mutation
4. SyncProgressBar appears: shows current folder name with index/total (e.g., "INBOX (1/5)"), synced/total count (e.g., "50/200")
5. Progress bar: outer div (h-1.5 bg-secondary rounded-full), inner div (bg-primary, width transitions via `transition-all duration-300`)
6. If errors occur during sync: error_message shown in text-xs text-destructive below the progress bar
7. While syncing: "Sync" button is hidden (canSync=false from SSE), "Cancel" button appears (canCancel=true)
8. Click "Cancel" — sync stops, button reverts
9. Connection indicator in top bar: green dot (bg-green-500) = connected, yellow pulsing dot (bg-yellow-500 animate-pulse) = reconnecting, red dot (bg-red-500) = disconnected
10. Tooltip on connection indicator: "SSE connection: [Connected/Reconnecting.../Disconnected]"

**Screenshots:** Sync progress bar active, connection indicator states

---

## Edge Cases

### Flow 30: Long Subject Lines & No-Subject Mails

**Setup:** Send test emails with: (a) extremely long subject (200+ chars), (b) no subject.  
**Steps:**
1. Navigate to inbox
2. Mail with long subject: subject text is truncated via `truncate` CSS class (text-overflow: ellipsis, white-space: nowrap, overflow: hidden) in the mail list item
3. In reading pane: subject displays fully in h2 (text-lg font-semibold leading-tight) — wraps to multiple lines
4. Mail with no subject: list shows "(no subject)" as the subject text — this is the fallback when `mail.subject` is null
5. Reading pane also shows "(no subject)" for the h2 subject line

**Screenshots:** Truncated long subject in list, full subject in reading pane, no-subject mail

---

### Flow 31: Empty Folder Display

**Setup:** Alice account, navigate to an empty folder (e.g., Drafts or a newly created custom folder).  
**Steps:**
1. Click a folder that has no messages
2. Mail list shows empty state: InboxIcon (h-12 w-12 opacity-50) centered, text "No messages in this folder" (text-sm)
3. Container: `flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground`
4. Reading pane remains in its current state (either showing previously selected mail or "Select a message to read")

**Screenshots:** Empty folder state

---

### Flow 32: Rapid Folder Switching

**Setup:** Alice account, multiple folders with mails.  
**Steps:**
1. Click Inbox — mail list starts loading
2. Immediately click Sent (before inbox mails fully render)
3. Mail list should show Sent folder mails (not stale Inbox data) — React Query's `useMailList` uses folderId as query key, so switching folders creates a new query
4. Selected mail resets to null on each folder switch (via handleFolderSelect)
5. Switch rapidly between 3-4 folders — final state should match the last clicked folder with correct mail list

**Screenshots:** Final state after rapid switching

---

### Flow 33: Body Not Yet Synced

**Setup:** Mail exists with headers_synced=true but body_synced=false (during two-phase sync).  
**Steps:**
1. Select a mail whose body hasn't been synced yet
2. Reading pane header renders normally (subject, sender, date, auth badges)
3. Below auth badges: spinning indicator appears — a small spinning circle (h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent) with text "Loading message body..."
4. EmailRenderer shows empty/placeholder content since body_html and body_text are null: italic text "No content available" in muted color
5. When body syncs (SSE event triggers React Query refetch): body content appears, spinning indicator disappears

**Screenshots:** Body loading indicator, before/after body sync
