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
    # api/accounts.py
    "GET /api/accounts",
    "POST /api/accounts",
    "DELETE /api/accounts/{account_id}",
    "GET /api/accounts/{account_id}",
    "PATCH /api/accounts/{account_id}",
    "GET /api/accounts/{account_id}/folders",
    "POST /api/accounts/{account_id}/sync",
    "GET /api/accounts/{account_id}/sync-status",
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
    # api/calendar_events.py
    "GET /api/calendar/events",
    "POST /api/calendar/events",
    "DELETE /api/calendar/events/{object_id}",
    "GET /api/calendar/events/{object_id}",
    "PATCH /api/calendar/events/{object_id}",
    "POST /api/calendar/events/{object_id}/respond",
    # api/calendars.py
    "GET /api/addressbooks",
    "POST /api/addressbooks",
    "GET /api/calendar/links",
    "PUT /api/calendar/links",
    "GET /api/calendars",
    "POST /api/calendars",
    "DELETE /api/calendars/{collection_id}",
    "PATCH /api/calendars/{collection_id}",
    # api/contacts.py
    "GET /api/contacts",
    "POST /api/contacts",
    "GET /api/contacts/groups",
    "GET /api/contacts/photo-index",
    "GET /api/contacts/resolve",
    "GET /api/contacts/search",
    "DELETE /api/contacts/{contact_id}",
    "GET /api/contacts/{contact_id}",
    "PATCH /api/contacts/{contact_id}",
    "GET /api/contacts/{contact_id}/photo",
    # api/dav_accounts.py
    "GET /api/dav-accounts",
    "POST /api/dav-accounts",
    "DELETE /api/dav-accounts/{dav_account_id}",
    "GET /api/dav-accounts/{dav_account_id}",
    "PATCH /api/dav-accounts/{dav_account_id}",
    "POST /api/dav-accounts/{dav_account_id}/sync",
    # api/embeddings.py
    "GET /api/embeddings/search",
    # api/events.py
    "GET /api/events",
    # api/folder_management.py
    "GET /api/accounts/{account_id}/folder-order",
    "PUT /api/accounts/{account_id}/folder-order",
    "POST /api/accounts/{account_id}/folders",
    "DELETE /api/folders/{folder_id}",
    "PATCH /api/folders/{folder_id}/prefs",
    # api/identities.py
    "GET /api/identities",
    "POST /api/identities",
    "DELETE /api/identities/{identity_id}",
    "PATCH /api/identities/{identity_id}",
    # api/image_exceptions.py
    "GET /api/accounts/{account_id}/image-exceptions",
    "POST /api/accounts/{account_id}/image-exceptions",
    "GET /api/accounts/{account_id}/image-exceptions/check",
    "DELETE /api/accounts/{account_id}/image-exceptions/{exception_id}",
    # api/invitations.py
    "GET /api/calendar/invitations/{message_id}",
    "POST /api/calendar/invitations/{message_id}/import",
    # api/mails.py
    "GET /api/accounts/{account_id}/messages",
    "POST /api/accounts/{account_id}/messages/bulk-action",
    "GET /api/accounts/{account_id}/messages/selection",
    "GET /api/messages/{message_id}",
    "POST /api/messages/{message_id}/action",
    "GET /api/messages/{message_id}/attachments/{attachment_id}",
    "GET /api/messages/{message_id}/location",
    "GET /api/messages/{message_id}/quote",
    "GET /api/messages/{message_id}/raw",
    "GET /api/messages/{message_id}/thread",
    # api/notifications.py
    "GET /api/accounts/{account_id}/notifications",
    "POST /api/accounts/{account_id}/notifications/ack-all",
    "GET /api/accounts/{account_id}/notifications/unacknowledged-count",
    "POST /api/accounts/{account_id}/notifications/{notification_id}/ack",
    "GET /api/notifications",
    # api/orders.py
    "GET /api/orders",
    "POST /api/orders/catch-up",
    "POST /api/orders/files",
    "GET /api/orders/files/{file_id}",
    "DELETE /api/orders/{order_id}",
    "GET /api/orders/{order_id}",
    "PATCH /api/orders/{order_id}",
    "GET /api/orders/{order_id}/codes/{fact_id}.png",
    "DELETE /api/orders/{order_id}/files/{file_id}",
    "GET /api/orders/{order_id}/files/{file_id}",
    "POST /api/orders/{order_id}/mails/{mail_key}/detach",
    "POST /api/orders/{order_id}/merge",
    "POST /api/orders/{order_id}/rewrite",
    # api/outbox.py
    "GET /api/outbox",
    "POST /api/outbox",
    "GET /api/outbox/pending",
    "GET /api/outbox/pending/{pending_send_id}/attachments/{attachment_id}",
    "POST /api/outbox/pending/{pending_send_id}/cancel",
    # api/pipeline.py
    "GET /api/pipeline",
    "PUT /api/pipeline",
    "POST /api/pipeline/assistant",
    "GET /api/pipeline/health",
    "GET /api/pipeline/revisions",
    "POST /api/pipeline/revisions/{revision}/restore",
    "GET /api/pipeline/stage-types",
    "POST /api/pipeline/stages",
    "POST /api/pipeline/stages/reorder",
    "DELETE /api/pipeline/stages/{stage_id}",
    "PATCH /api/pipeline/stages/{stage_id}",
    "POST /api/pipeline/stages/{stage_id}/test",
    "POST /api/pipeline/test",
    # api/runs.py
    "GET /api/mails/{mail_id}/runs",
    "GET /api/runs",
    "GET /api/runs/{run_id}",
    "POST /api/runs/{run_id}/retry",
    # api/search.py
    "GET /api/search",
    # api/stats.py
    "GET /api/stats",
    # api/unified.py
    "PUT /api/accounts/{account_id}/emoji",
    "GET /api/unified/folder-order",
    "PUT /api/unified/folder-order",
    "GET /api/unified/folders",
    "GET /api/unified/mails",
    "POST /api/unified/views",
    "DELETE /api/unified/views/{view_id}",
    "PATCH /api/unified/views/{view_id}",
    # api/verdicts.py
    "POST /api/mails/{mail_id}/feedback",
    "GET /api/mails/{mail_id}/verdict",
    "GET /api/verdicts",
    "GET /api/verdicts/spam-review",
})

PENDING_TOOLS: frozenset[str] = frozenset({
    # api/mcp_tools.py
    "create_contact",
    "create_event",
    "delete_contact",
    "delete_event",
    "draft_mail",
    "get_contact",
    "get_event",
    "get_mail",
    "get_order",
    "get_semantic_status",
    "get_stats",
    "get_thread",
    "get_verdict",
    "list_accounts",
    "list_addressbooks",
    "list_calendars",
    "list_contacts",
    "list_events",
    "list_folders",
    "list_mails",
    "list_orders",
    "mark_mail",
    "move_mail",
    "reply_mail",
    "respond_to_event",
    "search_contacts",
    "search_mail",
    "semantic_search_mail",
    "send_mail",
    "submit_spam_feedback",
    "tag_mail",
    "update_contact",
    "update_event",
})
