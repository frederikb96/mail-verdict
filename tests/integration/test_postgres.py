"""
Integration tests: Postgres database operations.

Requires podman-compose.test.yaml Postgres container running on port 5433.

Tests CRUD for all models, full-text search, upsert on conflict,
cascade deletes, and account scoping.

Markers: @pytest.mark.integration
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from mail_verdict.config import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    Attachment,
    Base,
    Folder,
    Mail,
    SpecialUse,
    TagSource,
    VerdictSource,
)
from mail_verdict.database.repository import (
    AttachmentRepository,
    FolderRepository,
    MailRepository,
    TagRepository,
    VerdictRepository,
)

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module")
async def db() -> DatabaseConnection:
    """Create and initialize a test database connection."""
    config = DatabaseConfig(
        url=TEST_DB_URL,
        pool_size=5,
        max_overflow=2,
    )
    conn = DatabaseConnection(config)
    await conn.init()

    # Create all tables
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    yield conn

    # Drop all tables
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)

    await conn.close()


@pytest.fixture
async def account(db: DatabaseConnection) -> Account:
    """Create a fresh test account."""
    async with db.session() as session:
        acc = Account(
            name=f"test-{uuid.uuid4().hex[:8]}",
            imap_host="imap.test.com",
            imap_port=993,
            imap_user="testuser",
        )
        session.add(acc)
        await session.flush()
        await session.refresh(acc)
        return acc


@pytest.fixture
async def folder(db: DatabaseConnection, account: Account) -> Folder:
    """Create a test folder."""
    async with db.session() as session:
        f = Folder(
            account_id=account.id,
            imap_name="INBOX",
            display_name="Inbox",
            special_use=SpecialUse.INBOX,
            separator="/",
        )
        session.add(f)
        await session.flush()
        await session.refresh(f)
        return f


@pytest.fixture
async def mail(db: DatabaseConnection, account: Account, folder: Folder) -> Mail:
    """Create a test mail."""
    async with db.session() as session:
        m = Mail(
            account_id=account.id,
            folder_id=folder.id,
            uid=1001,
            message_id="<test-msg-1@example.com>",
            subject="Test Email Subject",
            from_addr="sender@example.com",
            to_addrs={"addr1": "recipient@example.com"},
            body_text="This is a test email body for full-text search testing.",
            received_at=datetime.now(timezone.utc),
            size_bytes=1024,
            is_read=False,
        )
        session.add(m)
        await session.flush()
        await session.refresh(m)
        return m


class TestAccountCRUD:
    """Test Account model CRUD operations."""

    async def test_create_account(self, db: DatabaseConnection) -> None:
        """Create an account and verify it persists."""
        async with db.session() as session:
            acc = Account(
                name=f"crud-test-{uuid.uuid4().hex[:8]}",
                imap_host="imap.example.com",
                imap_port=993,
                imap_user="user@example.com",
            )
            session.add(acc)
            await session.flush()
            assert acc.id is not None

        async with db.session() as session:
            result = await session.execute(select(Account).where(Account.id == acc.id))
            fetched = result.scalar_one()
            assert fetched.name == acc.name
            assert fetched.imap_host == "imap.example.com"

    async def test_unique_account_name(self, db: DatabaseConnection) -> None:
        """Verify account names are unique."""
        from sqlalchemy.exc import IntegrityError

        name = f"unique-{uuid.uuid4().hex[:8]}"
        async with db.session() as session:
            session.add(
                Account(
                    name=name,
                    imap_host="h1",
                    imap_port=993,
                    imap_user="u1",
                )
            )

        with pytest.raises(IntegrityError):
            async with db.session() as session:
                session.add(
                    Account(
                        name=name,
                        imap_host="h2",
                        imap_port=993,
                        imap_user="u2",
                    )
                )


class TestFolderOperations:
    """Test Folder model and FolderRepository."""

    async def test_upsert_folder_creates(self, db: DatabaseConnection, account: Account) -> None:
        """Verify upsert creates a new folder."""
        repo = FolderRepository(db)
        folder = await repo.upsert_folder(
            account_id=account.id,
            imap_name="Sent",
            display_name="Sent Items",
            special_use="sent",
        )
        assert folder.id is not None
        assert folder.imap_name == "Sent"
        assert folder.special_use == SpecialUse.SENT

    async def test_upsert_folder_updates(self, db: DatabaseConnection, account: Account) -> None:
        """Verify upsert updates existing folder on conflict."""
        repo = FolderRepository(db)
        f1 = await repo.upsert_folder(
            account_id=account.id,
            imap_name="TestFolder",
            display_name="First Name",
        )
        f2 = await repo.upsert_folder(
            account_id=account.id,
            imap_name="TestFolder",
            display_name="Updated Name",
        )
        assert f1.id == f2.id
        assert f2.display_name == "Updated Name"

    async def test_update_sync_state(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Verify updating sync state fields."""
        repo = FolderRepository(db)
        ok = await repo.update_state(
            folder.id,
            uidvalidity=12345,
            uidnext=100,
            highestmodseq=999,
        )
        assert ok is True

        state = await repo.get_state(account.id, folder.imap_name)
        assert state is not None
        assert state.uidvalidity == 12345
        assert state.uidnext == 100

    async def test_get_by_account(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Verify get_by_account returns account's folders."""
        repo = FolderRepository(db)
        folders = await repo.get_by_account(account.id)
        assert any(f.id == folder.id for f in folders)


class TestMailOperations:
    """Test Mail model and MailRepository."""

    async def test_upsert_mail_creates(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Verify upsert creates a new mail."""
        repo = MailRepository(db)
        mail = await repo.upsert_mail(
            account_id=account.id,
            folder_id=folder.id,
            uid=2001,
            subject="New Upserted Mail",
            from_addr="test@upsert.com",
            body_text="Upsert test body.",
        )
        assert mail.id is not None
        assert mail.subject == "New Upserted Mail"

    async def test_upsert_mail_updates_on_conflict(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Verify upsert updates existing mail on (folder_id, uid) conflict."""
        repo = MailRepository(db)
        m1 = await repo.upsert_mail(
            account_id=account.id,
            folder_id=folder.id,
            uid=3001,
            subject="Original Subject",
        )
        m2 = await repo.upsert_mail(
            account_id=account.id,
            folder_id=folder.id,
            uid=3001,
            subject="Updated Subject",
        )
        assert m1.id == m2.id
        assert m2.subject == "Updated Subject"

    async def test_get_by_id_with_account_scoping(
        self, db: DatabaseConnection, account: Account, mail: Mail
    ) -> None:
        """Verify get_by_id respects account scoping."""
        repo = MailRepository(db)
        found = await repo.get_by_id(account.id, mail.id)
        assert found is not None
        assert found.id == mail.id

        # Wrong account should not find it
        wrong_account = uuid.uuid4()
        not_found = await repo.get_by_id(wrong_account, mail.id)
        assert not_found is None

    async def test_get_by_message_id(
        self, db: DatabaseConnection, account: Account, mail: Mail
    ) -> None:
        """Verify search by Message-ID header."""
        repo = MailRepository(db)
        results = await repo.get_by_message_id(account.id, mail.message_id or "")
        assert len(results) >= 1
        assert any(m.id == mail.id for m in results)

    async def test_get_by_folder(self, db: DatabaseConnection, folder: Folder, mail: Mail) -> None:
        """Verify listing mails by folder."""
        repo = MailRepository(db)
        mails = await repo.get_by_folder(folder.id)
        assert any(m.id == mail.id for m in mails)

    async def test_get_unprocessed(
        self, db: DatabaseConnection, account: Account, mail: Mail
    ) -> None:
        """Verify get_unprocessed returns mails without verdicts."""
        repo = MailRepository(db)
        unprocessed = await repo.get_unprocessed(account.id)
        assert any(m.id == mail.id for m in unprocessed)


class TestFullTextSearch:
    """Test PostgreSQL full-text search with tsvector + pg_trgm."""

    async def test_search_by_subject(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Search by subject keyword using tsvector."""
        # Create mail with tsvector populated
        async with db.session() as session:
            m = Mail(
                account_id=account.id,
                folder_id=folder.id,
                uid=9001,
                subject="Important Invoice Document",
                body_text="Please review the attached invoice.",
                received_at=datetime.now(timezone.utc),
            )
            session.add(m)
            await session.flush()

            # Manually update search vector (normally done by trigger/migration)
            await session.execute(
                text(
                    "UPDATE mails SET search_vector = "
                    "to_tsvector('english', coalesce(subject, '') || ' '"
                    " || coalesce(body_text, '')) "
                    "WHERE id = :id"
                ),
                {"id": str(m.id)},
            )

        repo = MailRepository(db)
        results = await repo.search_fulltext(account.id, "invoice")
        assert any(r.subject == "Important Invoice Document" for r in results)


class TestCascadeDeletes:
    """Test cascade delete behavior."""

    async def test_delete_mail_cascades_to_attachments(
        self, db: DatabaseConnection, account: Account, folder: Folder
    ) -> None:
        """Verify deleting a mail cascades to its attachments."""
        mail_id: uuid.UUID
        att_id: uuid.UUID

        async with db.session() as session:
            m = Mail(
                account_id=account.id,
                folder_id=folder.id,
                uid=5001,
                subject="Mail with attachment",
                received_at=datetime.now(timezone.utc),
            )
            session.add(m)
            await session.flush()
            mail_id = m.id

            att = Attachment(
                mail_id=m.id,
                filename="test.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                data=b"fake pdf content",
            )
            session.add(att)
            await session.flush()
            att_id = att.id

        # Delete the mail
        async with db.session() as session:
            m = await session.get(Mail, mail_id)
            if m:
                await session.delete(m)

        # Verify attachment is gone
        async with db.session() as session:
            result = await session.execute(select(Attachment).where(Attachment.id == att_id))
            assert result.scalar_one_or_none() is None

    async def test_delete_account_cascades_to_folders_and_mails(
        self, db: DatabaseConnection
    ) -> None:
        """Verify deleting an account cascades to folders and mails."""
        async with db.session() as session:
            acc = Account(
                name=f"cascade-{uuid.uuid4().hex[:8]}",
                imap_host="h",
                imap_port=993,
                imap_user="u",
            )
            session.add(acc)
            await session.flush()

            f = Folder(account_id=acc.id, imap_name="INBOX")
            session.add(f)
            await session.flush()

            m = Mail(
                account_id=acc.id,
                folder_id=f.id,
                uid=1,
                received_at=datetime.now(timezone.utc),
            )
            session.add(m)
            await session.flush()
            account_id = acc.id
            folder_id = f.id
            mail_id = m.id

        async with db.session() as session:
            acc = await session.get(Account, account_id)
            if acc:
                await session.delete(acc)

        async with db.session() as session:
            assert (await session.get(Folder, folder_id)) is None
            assert (await session.get(Mail, mail_id)) is None


class TestVerdictOperations:
    """Test Verdict model and VerdictRepository."""

    async def test_create_verdict(self, db: DatabaseConnection, mail: Mail) -> None:
        """Create a verdict and verify persistence."""
        repo = VerdictRepository(db)
        verdict = await repo.create_verdict(
            mail_id=mail.id,
            is_spam=True,
            source=VerdictSource.AI,
            model_used="gpt-4o-mini",
            reasoning="Suspicious sender pattern",
        )
        assert verdict.id is not None
        assert verdict.is_spam is True
        assert verdict.source == VerdictSource.AI

    async def test_get_latest_for_mail(self, db: DatabaseConnection, mail: Mail) -> None:
        """Verify get_latest_for_mail returns most recent verdict."""
        repo = VerdictRepository(db)
        # Create two verdicts
        await repo.create_verdict(
            mail_id=mail.id,
            is_spam=True,
            source=VerdictSource.AI,
        )
        v2 = await repo.create_verdict(
            mail_id=mail.id,
            is_spam=False,
            source=VerdictSource.USER_FEEDBACK,
        )

        latest = await repo.get_latest_for_mail(mail.id)
        assert latest is not None
        assert latest.id == v2.id
        assert latest.is_spam is False

    async def test_get_stats(self, db: DatabaseConnection, account: Account, mail: Mail) -> None:
        """Verify verdict stats aggregation."""
        repo = VerdictRepository(db)
        stats = await repo.get_stats(account.id)
        assert "total" in stats
        assert "spam" in stats
        assert "ham" in stats


class TestTagOperations:
    """Test MailTag model and TagRepository."""

    async def test_add_tag(self, db: DatabaseConnection, mail: Mail) -> None:
        """Create a tag and verify persistence."""
        repo = TagRepository(db)
        tag = await repo.add_tag(mail.id, "newsletter", TagSource.ENRICHMENT)
        assert tag.tag_name == "newsletter"
        assert tag.source == TagSource.ENRICHMENT

    async def test_add_tag_idempotent(self, db: DatabaseConnection, mail: Mail) -> None:
        """Verify adding same tag twice is idempotent."""
        repo = TagRepository(db)
        t1 = await repo.add_tag(mail.id, "finance", TagSource.RULE)
        t2 = await repo.add_tag(mail.id, "finance", TagSource.RULE)
        assert t1.id == t2.id

    async def test_remove_tag(self, db: DatabaseConnection, mail: Mail) -> None:
        """Verify tag removal."""
        repo = TagRepository(db)
        await repo.add_tag(mail.id, "removeme", TagSource.USER)
        removed = await repo.remove_tag(mail.id, "removeme")
        assert removed is True

        removed_again = await repo.remove_tag(mail.id, "removeme")
        assert removed_again is False

    async def test_get_tags_for_mail(self, db: DatabaseConnection, mail: Mail) -> None:
        """Verify listing tags for a mail."""
        repo = TagRepository(db)
        await repo.add_tag(mail.id, "tag-list-test", TagSource.SPAM)
        tags = await repo.get_tags_for_mail(mail.id)
        assert any(t.tag_name == "tag-list-test" for t in tags)


class TestAttachmentStorage:
    """Test Attachment model and AttachmentRepository."""

    async def test_create_and_retrieve(self, db: DatabaseConnection, mail: Mail) -> None:
        """Store attachment data (BYTEA) and retrieve it."""
        repo = AttachmentRepository(db)
        data = b"PK\x03\x04" + b"\x00" * 100  # Fake ZIP data

        att = await repo.create(
            mail_id=mail.id,
            filename="document.zip",
            content_type="application/zip",
            size_bytes=len(data),
            data=data,
        )
        assert att.id is not None

        attachments = await repo.get_by_mail_id(mail.id)
        found = next((a for a in attachments if a.id == att.id), None)
        assert found is not None
        assert found.data == data
        assert found.filename == "document.zip"
