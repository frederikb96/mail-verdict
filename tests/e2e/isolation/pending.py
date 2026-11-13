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

PENDING_ROUTES: frozenset[str] = frozenset()

PENDING_TOOLS: frozenset[str] = frozenset()
