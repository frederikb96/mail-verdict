"""
NeighborService against a real Postgres: proves the property the whole
module exists for -- an AI verdict never enters the neighbour pool, no
matter how similar or how close in time -- plus the label priority,
folder-based asymmetry, and the ordinary similarity/k/floor mechanics.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import EMBEDDING_DIMENSIONS
from mail_verdict.pipeline.neighbors import NeighborService

_imap_uid_counter = itertools.count(1)


def _unique_model() -> str:
    return f"model-{uuid.uuid4().hex[:8]}"


def _vector(seed: float) -> list[float]:
    """A deterministic vector, closer to [1, 0, 0, ...] as seed -> 1."""
    return [seed] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


async def _seed_account_and_folder(
    session: AsyncSession, *, special_use: str | None = None, imap_name: str = "INBOX",
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, :imap_name, :special_use)"
        ),
        {
            "id": folder_id, "account_id": account_id, "imap_name": imap_name,
            "special_use": special_use,
        },
    )
    return account_id, folder_id


async def _seed_message_with_embedding(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID, model: str,
    vector: list[float], subject: str = "Hello", from_addr: str = "sender@example.com",
) -> tuple[uuid.UUID, str]:
    """Insert a message plus its 'done' embedding. Returns (mail_id, msg_key)."""
    mail_id = uuid.uuid4()
    msg_key = f"sha256:{uuid.uuid4().hex}"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, NULL, :from_addr, "
            ":subject, 'Body.', :received_at, 1024)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "from_addr": from_addr, "subject": subject,
            "received_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    )
    await session.execute(
        text(
            "INSERT INTO message_embeddings "
            "(account_id, msg_key, message_id, model, status, embedding) "
            "VALUES (:a, :k, :m, :model, 'done', :vector)"
        ),
        {"a": account_id, "k": msg_key, "m": mail_id, "model": model, "vector": str(vector)},
    )
    return mail_id, msg_key


async def _record_verdict(
    session: AsyncSession, *, account_id: uuid.UUID, mail_id: uuid.UUID, msg_key: str,
    is_spam: bool, source: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO verdicts (mail_id, account_id, msg_key, is_spam, source, reasoning) "
            "VALUES (:mail_id, :account_id, :msg_key, :is_spam, :source, 'test')"
        ),
        {
            "mail_id": mail_id, "account_id": account_id, "msg_key": msg_key,
            "is_spam": is_spam, "source": source,
        },
    )


@pytest.mark.asyncio
async def test_ai_verdicts_never_enter_the_neighbor_pool(migrated_db: DatabaseConnection) -> None:
    """The core anti-feedback-loop property: a near-identical neighbour
    carrying only an AI verdict (no user correction, no folder signal)
    must not be returned at all."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=folder_id, model=model,
            vector=_vector(1.0), subject="query",
        )
        neighbor_id, neighbor_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=folder_id, model=model,
            vector=_vector(0.999), subject="near-identical, AI-only verdict",
        )
        await _record_verdict(
            session, account_id=account_id, mail_id=neighbor_id, msg_key=neighbor_key,
            is_spam=True, source="ai",
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=5, min_similarity=0.0)
    assert hints == []


@pytest.mark.asyncio
async def test_user_correction_wins_over_folder_placement(migrated_db: DatabaseConnection) -> None:
    """A message sitting in Junk but explicitly corrected to not-spam is
    reported as not-spam, from the correction, not the folder."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, junk_id = await _seed_account_and_folder(
            session, special_use="junk", imap_name="Junk",
        )
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=junk_id, model=model, vector=_vector(1.0),
        )
        neighbor_id, neighbor_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=junk_id, model=model, vector=_vector(0.99),
        )
        await _record_verdict(
            session, account_id=account_id, mail_id=neighbor_id, msg_key=neighbor_key,
            is_spam=False, source="user_feedback",
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=5, min_similarity=0.0)
    assert len(hints) == 1
    assert hints[0].is_spam is False
    assert hints[0].label_source == "user_correction"


@pytest.mark.asyncio
async def test_junk_and_inbox_folders_label_asymmetrically(migrated_db: DatabaseConnection) -> None:
    """No user correction on either neighbour: Junk membership labels
    spam, inbox membership labels not-spam, each tagged with its own
    (asymmetric) evidence string."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_folder(session, special_use="inbox")
        _, junk_id = await _seed_account_and_folder(session, special_use="junk", imap_name="Junk")
        # both folders belong to the same account already created above
        await session.execute(
            text("UPDATE folders SET account_id = :a WHERE id = :f"),
            {"a": account_id, "f": junk_id},
        )
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=inbox_id, model=model, vector=_vector(1.0),
        )
        _, junk_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=junk_id, model=model, vector=_vector(0.98),
            subject="junk neighbor",
        )
        _, inbox_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=inbox_id, model=model, vector=_vector(0.97),
            subject="inbox neighbor",
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=5, min_similarity=0.0)
    by_subject = {h.subject: h for h in hints}
    assert by_subject["junk neighbor"].is_spam is True
    assert by_subject["junk neighbor"].label_source == "junk_folder"
    assert by_subject["inbox neighbor"].is_spam is False
    assert by_subject["inbox neighbor"].label_source == "inbox_folder"


@pytest.mark.asyncio
async def test_unlabelled_folder_is_excluded(migrated_db: DatabaseConnection) -> None:
    """A neighbour with no user correction and sitting in neither Junk
    nor the inbox carries no human label and must not be returned."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, custom_id = await _seed_account_and_folder(
            session, special_use=None, imap_name="Projects",
        )
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=custom_id, model=model, vector=_vector(1.0),
        )
        await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=custom_id, model=model, vector=_vector(0.99),
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=5, min_similarity=0.0)
    assert hints == []


@pytest.mark.asyncio
async def test_min_similarity_floor_and_k_and_ordering(migrated_db: DatabaseConnection) -> None:
    """A neighbour below the floor is dropped; results are ordered nearest
    first and capped at k."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_folder(session, special_use="inbox")
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=inbox_id, model=model, vector=_vector(1.0),
        )
        for i, seed in enumerate((0.99, 0.95, 0.90)):
            await _seed_message_with_embedding(
                session, account_id=account_id, folder_id=inbox_id, model=model,
                vector=_vector(seed), subject=f"neighbor-{i}",
            )
        # An orthogonal vector: cosine similarity ~0, well below any floor.
        far_vector = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
        await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=inbox_id, model=model,
            vector=far_vector, subject="far-away",
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=2, min_similarity=0.5)
    assert len(hints) == 2
    assert hints[0].similarity >= hints[1].similarity
    assert all(h.subject != "far-away" for h in hints)


@pytest.mark.asyncio
async def test_sent_folder_neighbor_is_excluded_even_with_a_user_correction(
    migrated_db: DatabaseConnection,
) -> None:
    """Sent mail is never useful spam-neighbour evidence, whatever label
    it happens to carry."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_folder(session, special_use="inbox")
        _, sent_id = await _seed_account_and_folder(session, special_use="sent", imap_name="Sent")
        await session.execute(
            text("UPDATE folders SET account_id = :a WHERE id = :f"),
            {"a": account_id, "f": sent_id},
        )
        query_id, query_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=inbox_id, model=model, vector=_vector(1.0),
        )
        sent_mail_id, sent_key = await _seed_message_with_embedding(
            session, account_id=account_id, folder_id=sent_id, model=model, vector=_vector(0.99),
        )
        await _record_verdict(
            session, account_id=account_id, mail_id=sent_mail_id, msg_key=sent_key,
            is_spam=True, source="user_feedback",
        )
        await session.commit()

    service = NeighborService(migrated_db, account_id)
    hints = await service.hints_for(msg_key=query_key, model=model, k=5, min_similarity=0.0)
    assert hints == []
