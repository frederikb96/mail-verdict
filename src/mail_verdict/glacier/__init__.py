"""
The glacier: a per-account place a message can be moved to where it
leaves the mail server for good and lives on only in this database
(database/models.py's GlacierMessage/GlacierAttachment,
account_prefs.glacier_*).

`operations.py` is the write sequence -- copy, verify, expunge, and the
bookkeeping that confirms or withdraws an expunge and repairs what a
UIDVALIDITY resync or a split conversation leaves behind. `sweep.py` is
the periodic pass that runs it automatically over an account's archive.
`restore.py` is the reverse: moving a message back onto the server.
`rows.py` is the read-side glue every listing, search and unified-view
query that must span both `messages` and `glacier_messages` goes
through.

Every write to a PostIMAP-owned column goes through
`postimap/actions.py`, per the architecture rule; this package never
issues that SQL itself.
"""

from __future__ import annotations
