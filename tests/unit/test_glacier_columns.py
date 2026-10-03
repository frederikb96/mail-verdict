"""
glacier_messages is column-compatible with messages by design (see
glacier/rows.py): every read spanning both tables is a mechanical union
built on that guarantee. A hand-listed set of "the columns that must
match" stops being true the moment a column is added to Message and
nobody remembers to add it here too -- so this derives the expected set
from the model itself, the same discipline test_lock_keys.py and the pg
grant-boundary sweep already use for their own "must cover every X"
claims.
"""

from __future__ import annotations

from mail_verdict.database.models import GlacierMessage, Message


def test_every_message_column_exists_on_glacier_messages() -> None:
    message_columns = set(Message.__table__.columns.keys())
    glacier_columns = set(GlacierMessage.__table__.columns.keys())
    missing = message_columns - glacier_columns
    assert not missing, (
        "glacier_messages is missing columns messages has -- every union "
        "over both tables silently drops them:\n  " + "\n  ".join(sorted(missing))
    )


def test_shared_columns_have_the_same_type() -> None:
    message_columns = {c.name: c for c in Message.__table__.columns}
    glacier_columns = {c.name: c for c in GlacierMessage.__table__.columns}
    mismatched = []
    for name, msg_col in message_columns.items():
        glacier_col = glacier_columns[name]
        # Compare by Python type affinity rather than the exact SQLAlchemy
        # type instance -- TSVECTOR on the glacier side is a generated
        # column with no direct Python-side default, but it must still
        # decode to the same thing a query reads back.
        if type(msg_col.type) is not type(glacier_col.type):
            mismatched.append(
                f"{name}: messages={msg_col.type!r} glacier_messages={glacier_col.type!r}"
            )
    assert not mismatched, "\n  ".join(mismatched)
