"""
The document a user starts with.

Every user owns a complete pipeline document, so a user who has none yet
is given this one the first time anything needs it: classify new mail,
move what is spam to Junk and mark it read, bundle orders. It is the
shape a migrated deployment's first document has (see
`build_migrated_definition`) without any rule of someone's own, and what
the user changes from there is theirs alone.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from mail_verdict.pipeline.revisions import PipelineDefinition, build_migrated_definition

if TYPE_CHECKING:
    from mail_verdict.pipeline.revisions import PipelineRevisionRepository

TEMPLATE_NOTE = "starting rules"


def template_document() -> dict[str, Any]:
    """A fresh copy of the starting document."""
    return build_migrated_definition(raw_rules=[], spam_settings={})


async def current_or_template(
    repo: PipelineRevisionRepository, user_id: uuid.UUID,
) -> PipelineDefinition:
    """The user's current definition, written from the template first if
    they have none."""
    return await repo.ensure(user_id, template_document(), note=TEMPLATE_NOTE)


__all__ = ["TEMPLATE_NOTE", "current_or_template", "template_document"]
