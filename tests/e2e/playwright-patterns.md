# Playwright-Local MCP Patterns for MailVerdict

Reusable browser automation patterns using the `playwright-local` MCP server tools.
All examples target the test container at `http://10.69.243.241:18080` (host IP for cross-container access)
or `http://127.0.0.1:18080` (localhost).

## Tool Overview

| Tool | Purpose |
|---|---|
| `browser_navigate` | Navigate to a URL |
| `browser_snapshot` | Capture accessibility tree (preferred over screenshots for interaction) |
| `browser_take_screenshot` | Visual screenshot (PNG/JPEG) |
| `browser_click` | Click an element by ref |
| `browser_fill_form` | Fill multiple form fields at once |
| `browser_press_key` | Press keyboard keys |
| `browser_wait_for` | Wait for time, text appearance, or text disappearance |
| `browser_evaluate` | Run arbitrary JavaScript on page |
| `browser_run_code` | Run Playwright code snippets |
| `browser_select_option` | Select dropdown values |
| `browser_hover` | Hover over elements |
| `browser_drag` | Drag and drop between elements |

---

## Navigation Patterns

### Navigate to Pages

```bash
# Main mail view (three-pane layout)
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/"

# Accounts management
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/accounts"

# Settings
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/settings"

# Search
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/search"
```

### Navigate via Sidebar Links

Sidebar links have stable roles and names. Use `browser_snapshot` first to get refs, then click:

```bash
# Take snapshot to get current refs
mcp-call playwright-local browser_snapshot

# Click sidebar navigation links (refs change per page load)
mcp-call playwright-local browser_click --element "Search link" --ref "<ref>"
mcp-call playwright-local browser_click --element "Accounts link" --ref "<ref>"
mcp-call playwright-local browser_click --element "Settings link" --ref "<ref>"
```

Sidebar link refs can also be found by their accessible name in the snapshot:
- `link "Search"` with `/url: /search`
- `link "Accounts"` with `/url: /accounts`
- `link "Settings"` with `/url: /settings`

### Wait for Page Load

After navigation, confirm the page loaded by checking for key elements:

```bash
# Wait for specific heading text to appear
mcp-call playwright-local browser_wait_for --text "Accounts"
mcp-call playwright-local browser_wait_for --text "Settings"
mcp-call playwright-local browser_wait_for --text "Search"

# For the main mail view, wait for the sidebar account selector
mcp-call playwright-local browser_wait_for --text "Select Account"
# or if an account is selected:
mcp-call playwright-local browser_wait_for --text "alice"
```

---

## Element Selection

### The Ref System

Every interactive element in a `browser_snapshot` output has a `ref` attribute (e.g., `ref=e42`).
These refs are **session-local and ephemeral** -- they change after every navigation or DOM update.
Always take a fresh snapshot before interacting.

### Snapshot-Then-Act Pattern

This is the fundamental pattern: **snapshot first, act with refs second**.

```bash
# Step 1: snapshot
mcp-call playwright-local browser_snapshot

# Step 2: find the ref you need in the YAML output, then act
mcp-call playwright-local browser_click --element "descriptive name" --ref "e42"
```

### Sidebar Elements

**Account selector dropdown:**
```yaml
# Snapshot shows:
button "Select Account" [ref=e9]    # No account selected
button "alice" [ref=e9]             # Account "alice" selected
```

```bash
# Open account dropdown
mcp-call playwright-local browser_click --element "Select Account button" --ref "e9"

# After opening, menu items appear:
# menu "Select Account" > menuitem "Unified View" [ref=e76]
# menu "Select Account" > menuitem "alice" [ref=e77]
mcp-call playwright-local browser_click --element "alice account" --ref "e77"
```

**Folder list** (visible when account selected, in the sidebar under "Folders" heading):
```yaml
# Folders appear as list items with icons:
generic "Folders"
list
  listitem > link "Inbox" [ref=eXX]
  listitem > link "Sent" [ref=eXX]
  listitem > link "Drafts" [ref=eXX]
  listitem > link "Junk Email" [ref=eXX]
  listitem > link "Trash" [ref=eXX]
```

### Mail List Items

Mail items in the main view appear as list items with subject, sender, and date.
They are rendered in a virtual scroll list (virtua VList), so only visible items have refs.

```bash
# After selecting an account + folder, snapshot to see mails
mcp-call playwright-local browser_snapshot

# Click a mail item (typically a generic element with subject text)
mcp-call playwright-local browser_click --element "mail item" --ref "eXX"
```

### Action Buttons

**Account page actions** (per-account card):
```yaml
button "Sync" [ref=eXX]     # Trigger manual sync
button "Test" [ref=eXX]     # Test IMAP/SMTP connection
button "Edit" [ref=eXX]     # Open edit dialog
button "Delete" [ref=eXX]   # Delete account
```

**Account page collapsible sections:**
```yaml
button "Folder Assignment" [ref=eXX]
button "Folder Order & Visibility" [ref=eXX]
button "IMAP IDLE" [ref=eXX]
button "Image Exceptions" [ref=eXX]
button "Unified View Names" [ref=eXX]
```

**Top bar:**
```yaml
button "Toggle Sidebar" [ref=eXX]           # Collapse/expand sidebar
button "Connection status: Connected"        # SSE connection indicator
button "Connection status: Disconnected"     # When SSE is down
```

---

## Form Interaction

### Fill Form Fields

`browser_fill_form` requires each field to have `name`, `type`, `ref`, and `value`.

Supported types: `textbox`, `checkbox`, `radio`, `combobox`, `slider`

**NOTE:** For `spinbutton` elements (number inputs like ports), use type `textbox` -- the tool maps
spinbuttons automatically.

```bash
mcp-call playwright-local browser_fill_form --fields '[
  {"name": "Account Name", "type": "textbox", "ref": "e83", "value": "alice"},
  {"name": "IMAP Host", "type": "textbox", "ref": "e87", "value": "stalwart"},
  {"name": "IMAP Port", "type": "textbox", "ref": "e90", "value": "1143"},
  {"name": "IMAP User", "type": "textbox", "ref": "e94", "value": "alice@test.local"},
  {"name": "IMAP Password", "type": "textbox", "ref": "e97", "value": "testpass123"},
  {"name": "SMTP Host", "type": "textbox", "ref": "e101", "value": "stalwart"},
  {"name": "SMTP Port", "type": "textbox", "ref": "e104", "value": "2525"},
  {"name": "SMTP User", "type": "textbox", "ref": "e108", "value": "alice@test.local"},
  {"name": "SMTP Password", "type": "textbox", "ref": "e111", "value": "testpass123"}
]'
```

The tool generates Playwright code like:
```js
await page.getByRole('textbox', { name: 'Account Name' }).fill('alice');
await page.getByRole('spinbutton', { name: 'IMAP Port' }).fill('1143');
```

### Toggle Checkboxes / Switches

Checkboxes and switches appear in snapshots as:
```yaml
switch [checked] [ref=eXX]     # Toggle switch (on)
checkbox [checked] [ref=eXX]   # Underlying checkbox
```

Click the switch or checkbox to toggle:
```bash
mcp-call playwright-local browser_click --element "Enable spam detection" --ref "eXX"
```

### Settings Page Tabs

Settings uses a tablist with category tabs:
```yaml
tablist
  tab "AI" [selected] [ref=eXX]
  tab "Spam" [ref=eXX]
  tab "Sync" [ref=eXX]
  tab "Retry" [ref=eXX]
  tab "Rules" [ref=eXX]
```

```bash
# Switch to Spam settings tab
mcp-call playwright-local browser_click --element "Spam tab" --ref "eXX"
```

### Search Input

```yaml
textbox "Search messages..." [ref=eXX]
button "Fulltext" [ref=eXX]     # Search mode toggle
button "Semantic" [ref=eXX]     # Search mode toggle
```

```bash
# Type in search box
mcp-call playwright-local browser_fill_form --fields '[
  {"name": "Search messages...", "type": "textbox", "ref": "eXX", "value": "invoice"}
]'

# Switch search mode
mcp-call playwright-local browser_click --element "Semantic search mode" --ref "eXX"
```

---

## Wait Strategies

### Wait for Time

```bash
# Wait N seconds (useful after triggering async operations)
mcp-call playwright-local browser_wait_for --time 3
```

### Wait for Text Appearance

```bash
# Wait for success message after connection test
mcp-call playwright-local browser_wait_for --text "Connection successful"

# Wait for account status change
mcp-call playwright-local browser_wait_for --text "Active"

# Wait for SSE connection
mcp-call playwright-local browser_wait_for --text "Connected"
```

### Wait for Text Disappearance

```bash
# Wait for loading indicator to vanish
mcp-call playwright-local browser_wait_for --textGone "Loading..."

# Wait for dialog to close
mcp-call playwright-local browser_wait_for --textGone "Add Account"

# Wait for "Disconnected" to change to "Connected"
mcp-call playwright-local browser_wait_for --textGone "Disconnected"
```

### After-Action Waits

| Action | What to Wait For |
|---|---|
| Navigate to page | `--text "Accounts"` (heading text) |
| Create account | `--textGone "Add Account"` (dialog closes) then `--text "Created"` (status badge) |
| Test connection | `--text "Connection successful"` |
| Trigger sync | `--text "Syncing"` then `--text "Active"` |
| Click mail | Wait for reading pane content (message body text) |
| Search | `--textGone "Enter at least 2 characters"` then result text |
| SSE reconnect | `--textGone "Disconnected"` |

---

## Screenshot Patterns

### Full Viewport

```bash
mcp-call playwright-local browser_take_screenshot \
  --type "png" \
  --filename "screenshots/accounts-page.png"
```

### Full Page (scrollable)

```bash
mcp-call playwright-local browser_take_screenshot \
  --type "png" \
  --filename "screenshots/settings-full.png" \
  --fullPage true
```

### Element Screenshot

Requires both `element` (description) and `ref` from a snapshot:

```bash
mcp-call playwright-local browser_take_screenshot \
  --type "png" \
  --element "Account card" \
  --ref "e55" \
  --filename "screenshots/account-card.png"
```

### File Naming Convention

```
screenshots/<page>-<context>.png
screenshots/mail-inbox-list.png
screenshots/accounts-add-dialog.png
screenshots/settings-spam-tab.png
screenshots/search-results.png
```

**NOTE:** The filename path is relative to playwright-local's working directory, not the project root.
Use absolute paths (`/tmp/screenshots/...`) for predictable output locations.

---

## Obstacle Clearing

### No Cookie Banners

MailVerdict is a self-hosted app with no cookie consent overlays.

### Dialogs

Dialogs (e.g., Add Account, Edit Account, Delete confirmation) are rendered as `dialog` elements
in the accessibility tree. Close them with:

```bash
# Click the Close button (X icon in top-right)
mcp-call playwright-local browser_click --element "Close dialog" --ref "eXX"

# Or press Escape
mcp-call playwright-local browser_press_key --key "Escape"

# Or click Cancel button
mcp-call playwright-local browser_click --element "Cancel button" --ref "eXX"
```

### Connection Status

The SSE connection indicator (`Connected`/`Disconnected`) appears in the top bar.
A "Disconnected" state does NOT block interaction -- it just means SSE events aren't flowing.
Wait for reconnection:

```bash
mcp-call playwright-local browser_wait_for --text "Connected"
```

### Stale API Key Banner

If the OpenAI API key is not configured, a banner may appear at the top. Set it via the API
or through the Settings UI before testing AI features.

---

## Common Sequences

### Create an Account

```bash
# 1. Navigate to accounts
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/accounts"
mcp-call playwright-local browser_wait_for --text "Accounts"

# 2. Open add dialog
mcp-call playwright-local browser_snapshot
# Find "Add Account" button ref
mcp-call playwright-local browser_click --element "Add Account" --ref "eXX"
mcp-call playwright-local browser_wait_for --text "Account Name"

# 3. Fill form
mcp-call playwright-local browser_snapshot
# Get refs for all form fields, then:
mcp-call playwright-local browser_fill_form --fields '[...]'

# 4. Submit
mcp-call playwright-local browser_click --element "Create" --ref "eXX"

# 5. Verify
mcp-call playwright-local browser_wait_for --text "Created"
```

### Test Account Connection

```bash
# From the accounts page with account card visible:
mcp-call playwright-local browser_snapshot
# Find "Test" button ref
mcp-call playwright-local browser_click --element "Test" --ref "eXX"
mcp-call playwright-local browser_wait_for --text "Connection successful"
```

### Select Account and Folder

```bash
# 1. Open account dropdown
mcp-call playwright-local browser_snapshot
mcp-call playwright-local browser_click --element "account selector" --ref "eXX"

# 2. Select account from menu
mcp-call playwright-local browser_snapshot
mcp-call playwright-local browser_click --element "alice" --ref "eXX"

# 3. Wait for folders to load
mcp-call playwright-local browser_wait_for --text "Inbox"

# 4. Click a folder
mcp-call playwright-local browser_snapshot
mcp-call playwright-local browser_click --element "Inbox" --ref "eXX"
```

### Read a Mail

```bash
# After selecting account + folder:
mcp-call playwright-local browser_snapshot
# Find mail item ref in the list
mcp-call playwright-local browser_click --element "mail item" --ref "eXX"

# Wait for reading pane to populate
mcp-call playwright-local browser_wait_for --time 1
mcp-call playwright-local browser_snapshot
# Reading pane now shows message content
```

### Search for Messages

```bash
# 1. Navigate to search
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/search"
mcp-call playwright-local browser_wait_for --text "Search"

# 2. Enter search query
mcp-call playwright-local browser_snapshot
mcp-call playwright-local browser_fill_form --fields '[
  {"name": "Search messages...", "type": "textbox", "ref": "eXX", "value": "invoice"}
]'

# 3. Wait for results
mcp-call playwright-local browser_wait_for --textGone "Enter at least 2 characters"
mcp-call playwright-local browser_wait_for --time 2

# 4. View results
mcp-call playwright-local browser_snapshot
# Click a result
mcp-call playwright-local browser_click --element "search result" --ref "eXX"
```

### Switch Settings Tab

```bash
mcp-call playwright-local browser_navigate --url "http://10.69.243.241:18080/settings"
mcp-call playwright-local browser_wait_for --text "Settings"

mcp-call playwright-local browser_snapshot
# Click desired tab
mcp-call playwright-local browser_click --element "Spam tab" --ref "eXX"
mcp-call playwright-local browser_wait_for --time 1
mcp-call playwright-local browser_snapshot
```

### Toggle Sidebar

```bash
mcp-call playwright-local browser_snapshot
mcp-call playwright-local browser_click --element "Toggle Sidebar" --ref "eXX"
```

---

## Advanced Patterns

### Run Custom Playwright Code

For complex interactions not covered by individual tools:

```bash
mcp-call playwright-local browser_run_code --code 'async (page) => {
  await page.getByRole("button", { name: "Add Account" }).click();
  await page.getByRole("textbox", { name: "Account Name" }).fill("test-account");
  await page.getByRole("button", { name: "Create" }).click();
  return await page.title();
}'
```

### Evaluate JavaScript

For extracting data or checking state:

```bash
# Get page title
mcp-call playwright-local browser_evaluate --function '() => document.title'

# Get all visible text
mcp-call playwright-local browser_evaluate --function '() => document.body.innerText'

# Count mail items in the list
mcp-call playwright-local browser_evaluate --function '() => document.querySelectorAll("[data-mail-id]").length'

# Check if element exists
mcp-call playwright-local browser_evaluate --function '() => !!document.querySelector("dialog[open]")'
```

### Evaluate on a Specific Element

```bash
# Get text content of a specific element
mcp-call playwright-local browser_evaluate \
  --element "Account card" \
  --ref "eXX" \
  --function '(element) => element.textContent'
```

### Check Console Errors

```bash
# Get console messages (useful for debugging)
mcp-call playwright-local browser_console_messages --level "error"
```

### Check Network Requests

```bash
# See failed API calls
mcp-call playwright-local browser_network_requests --includeStatic false
```

---

## MailVerdict DOM Structure Reference

### Layout (Three-Pane)

```
root
  sidebar (generic)
    account-selector (list > listitem > button)
    folder-list (generic > list)
    nav-links (list > listitem > link)
  main
    top-bar (generic)
      toggle-sidebar (button)
      connection-indicator (button)
    content-area
      mail-list (left pane)
      reading-pane (right pane)
```

### Key Accessible Roles

| Element | Role | Name |
|---|---|---|
| Account dropdown | `button` | Account name or "Select Account" |
| Account menu | `menu` | "Select Account" |
| Sidebar nav | `link` | "Search", "Accounts", "Settings" |
| Settings tabs | `tab` | "AI", "Spam", "Sync", "Retry", "Rules" |
| Page headings | `heading` (level 1) | "Accounts", "Settings", "Search" |
| Form inputs | `textbox` / `spinbutton` | Field labels ("IMAP Host", etc.) |
| Dialogs | `dialog` | Dialog title ("Add Account", etc.) |
| Toggle switches | `switch` + `checkbox` | Associated label text |
| Search input | `textbox` | "Search messages..." |

### Account Card Structure

When an account exists on the `/accounts` page:

```yaml
generic               # Card container
  generic             # Header: emoji button + account name + status badge
  generic             # Body
    generic           #   Sync toggle row
    generic           #   Connection details (IMAP/SMTP/Spam info)
    generic           #   Action buttons (Sync, Test, Edit, Delete)
    generic           #   Collapsible sections (Folder Assignment, etc.)
```

### Add Account Dialog Fields

| Field | Role | Placeholder |
|---|---|---|
| Account Name | `textbox` | "My Email" |
| IMAP Host | `textbox` | "imap.example.com" |
| IMAP Port | `spinbutton` | "993" |
| IMAP User | `textbox` | "user@example.com" |
| IMAP Password | `textbox` | "" |
| SMTP Host | `textbox` | "smtp.example.com" |
| SMTP Port | `spinbutton` | (empty) |
| SMTP User | `textbox` | (empty) |
| SMTP Password | `textbox` | "" |
| Sync Lookback | `spinbutton` | "180" |
| Enable spam detection | `checkbox` | n/a |

Test values for Stalwart test server:
- IMAP Host: `stalwart`, Port: `1143`
- SMTP Host: `stalwart`, Port: `2525`
- User: `alice@test.local`, Password: `testpass123`
