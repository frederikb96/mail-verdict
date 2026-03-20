"""
Settings repository: CRUD for the settings table.

Each row is one category with a JSONB data blob.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Setting

logger = logging.getLogger(__name__)


class SettingsRepository:
    """Database operations for the Setting model."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize the settings repository.

        Args:
            db: Database connection
        """
        self._db = db

    async def get_all(self) -> dict[str, dict[str, Any]]:
        """
        Load all settings grouped by category.

        Returns:
            Dict mapping category name to JSONB data
        """
        async with self._db.session() as session:
            result = await session.execute(select(Setting))
            rows = list(result.scalars().all())
        return {row.category: row.data for row in rows}

    async def get_category(self, category: str) -> dict[str, Any] | None:
        """
        Load settings for a single category.

        Args:
            category: Setting category name

        Returns:
            JSONB data dict, or None if not stored
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Setting).where(Setting.category == category)
            )
            row = result.scalar_one_or_none()
        return row.data if row else None

    async def upsert_category(self, category: str, data: dict[str, Any]) -> None:
        """
        Insert or update settings for a category (merge semantics).

        Merges provided keys into existing data, preserving unmentioned keys.

        Args:
            category: Setting category name
            data: Partial or full JSONB data to merge
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Setting).where(Setting.category == category)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                merged = {**existing.data, **data}
                existing.data = merged
            else:
                session.add(Setting(category=category, data=data))
        logger.info("Settings upserted", extra={"category": category})
