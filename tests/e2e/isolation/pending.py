"""
Every route and MCP tool not yet classified by an area module.

Each entry here answers whatever any signed-in caller asks, whoever owns the
data -- the state of the whole application before it knew more than one user.
An area made per-user deletes its entries here and adds its cases to a module
of its own beside this one; the harness fails on an entry naming something
the app no longer serves, so this list can only shrink. Grouped by the module
defining the endpoint.
"""

from __future__ import annotations

PENDING_ROUTES: frozenset[str] = frozenset({
    # api/alerts.py
    "GET /api/alerts",
    "GET /api/alerts/badge",
    "POST /api/alerts/dismiss-all",
    "POST /api/alerts/lookup",
    "GET /api/alerts/native-push",
    "GET /api/alerts/subscriptions",
    "POST /api/alerts/subscriptions",
    "POST /api/alerts/subscriptions/native",
    "DELETE /api/alerts/subscriptions/{subscription_id}",
    "PATCH /api/alerts/subscriptions/{subscription_id}",
    "POST /api/alerts/subscriptions/{subscription_id}/test",
    "GET /api/alerts/unseen-count",
    "GET /api/alerts/vapid-public-key",
    "POST /api/alerts/{alert_id}/dismiss",
    # api/embeddings.py
    "GET /api/embeddings/search",
    # api/events.py
    "GET /api/events",
    # api/search.py
    "GET /api/search",
    # api/unified.py
    "PUT /api/accounts/{account_id}/emoji",
    "GET /api/unified/folder-order",
    "PUT /api/unified/folder-order",
    "GET /api/unified/folders",
    "GET /api/unified/mails",
    "POST /api/unified/views",
    "DELETE /api/unified/views/{view_id}",
    "PATCH /api/unified/views/{view_id}",
})

PENDING_TOOLS: frozenset[str] = frozenset({
    # api/mcp_tools.py
    "draft_mail",
    "get_mail",
    "get_semantic_status",
    "get_stats",
    "get_thread",
    "get_verdict",
    "list_accounts",
    "list_folders",
    "list_mails",
    "mark_mail",
    "move_mail",
    "reply_mail",
    "search_mail",
    "semantic_search_mail",
    "send_mail",
    "submit_spam_feedback",
    "tag_mail",
})
