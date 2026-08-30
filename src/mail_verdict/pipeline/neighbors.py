"""
NeighborService: nearest-neighbour lookup restricted to human-originated
labels, for the classify stage's optional neighbour hints (settings.
semantic.neighbor_hints_enabled).

Mail is enormously repetitive -- the same sender, the same template, month
after month -- so the nearest neighbours of a new message are dominated by
near-identical past ones. If the classifier's own past verdicts were in
that pool, the first verdict for a sender would become permanent: get one
newsletter wrong and every future one sees a spam neighbour, agrees, and
becomes another spam neighbour, indistinguishable from the model actually
being right. So the pool here is exactly two kinds of evidence, both
produced by a person rather than by this application: an explicit user
correction (verdicts.source = 'user_feedback'), and the folder a message
currently sits in. Nothing sourced from source = 'ai' or 'rule' ever
enters it, in this module or any other -- that is the whole point.

Folder membership is asymmetric evidence, not a second kind of verdict:
a message in Junk was put there by someone, which is a strong spam
signal, but a message simply sitting in the inbox is weak evidence of
not-spam -- it may just not have been dealt with yet. Both directions are
handed to the model as separate, labelled facts (see
pipeline/stages/classify.py) rather than collapsed into one score here.

Deliberately does not implement a near-duplicate short-circuit: a
sufficiently close neighbour is still only ever a hint in the prompt,
never a reason to skip the model call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from mail_verdict.database.connection import DatabaseConnection

# How many candidates to pull by raw similarity before label-based
# filtering narrows them down to at most `k` -- generous enough that a
# handful of unlabelled near-duplicates (no folder signal, no user
# correction) does not starve the result of hints entirely.
_CANDIDATE_POOL_MULTIPLIER = 6


@dataclass(frozen=True)
class NeighborHint:
    """One labelled past message, offered to the classifier as evidence
    -- never as a verdict to imitate."""

    similarity: float
    is_spam: bool
    label_source: str  # "user_correction" | "junk_folder" | "inbox_folder"
    from_addr: str
    subject: str


class NeighborService:
    """Semantic neighbours for one message, filtered to human labels and
    scoped to one account -- a stage never writes vector SQL directly."""

    def __init__(self, db: DatabaseConnection, account_id: uuid.UUID) -> None:
        self._db = db
        self._account_id = account_id

    async def hints_for(
        self, *, msg_key: str, model: str, k: int, min_similarity: float,
    ) -> list[NeighborHint]:
        """
        The message's k nearest human-labelled neighbours, nearest first.

        Args:
            msg_key: The message being classified -- excluded from its
                own results
            model: Only vectors under this embedding model are compared;
                mixing models would compare unrelated spaces
            k: Maximum hints to return
            min_similarity: Cosine similarity floor; a neighbour below it
                is dropped rather than padding the result with noise

        Returns:
            Up to k hints ordered by similarity, descending. Empty if the
            message has no embedding yet, or nothing in scope has a human
            label.
        """
        if k <= 0:
            return []
        async with self._db.session() as session:
            rows = await session.execute(
                text(
                    """
                    WITH query_vec AS (
                        SELECT embedding FROM message_embeddings
                        WHERE account_id = :account_id AND msg_key = :msg_key
                          AND model = :model AND status = 'done'
                        LIMIT 1
                    ),
                    candidates AS (
                        SELECT me.msg_key, me.message_id,
                               (1 - (me.embedding <=> qv.embedding)) AS similarity
                        FROM message_embeddings me, query_vec qv
                        WHERE me.account_id = :account_id AND me.model = :model
                          AND me.status = 'done' AND me.msg_key != :msg_key
                        ORDER BY me.embedding <=> qv.embedding
                        LIMIT :pool_size
                    )
                    SELECT c.similarity, m.from_addr, m.subject,
                           coalesce(fp.special_use_override, f.special_use)
                               AS effective_special_use,
                           uf.is_spam AS user_feedback_is_spam
                    FROM candidates c
                    JOIN messages m ON m.id = c.message_id AND m.account_id = :account_id
                    JOIN folders f ON f.id = m.folder_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    LEFT JOIN LATERAL (
                        SELECT is_spam FROM verdicts v
                        WHERE v.account_id = :account_id AND v.msg_key = c.msg_key
                          AND v.source = 'user_feedback'
                        ORDER BY v.created_at DESC LIMIT 1
                    ) uf ON true
                    WHERE m.expunged_at IS NULL AND f.deleted_at IS NULL
                      AND c.similarity >= :min_similarity
                      AND coalesce(fp.special_use_override, f.special_use, '')
                          NOT IN ('sent', 'drafts')
                    ORDER BY c.similarity DESC
                    """
                ),
                {
                    "account_id": self._account_id, "msg_key": msg_key, "model": model,
                    "pool_size": k * _CANDIDATE_POOL_MULTIPLIER, "min_similarity": min_similarity,
                },
            )
            hints: list[NeighborHint] = []
            for row in rows.all():
                labelled = _label(
                    user_feedback_is_spam=row.user_feedback_is_spam,
                    effective_special_use=row.effective_special_use,
                )
                if labelled is None:
                    continue
                is_spam, label_source = labelled
                hints.append(NeighborHint(
                    similarity=float(row.similarity), is_spam=is_spam, label_source=label_source,
                    from_addr=row.from_addr or "", subject=row.subject or "",
                ))
                if len(hints) >= k:
                    break
            return hints


def _label(
    *, user_feedback_is_spam: bool | None, effective_special_use: str | None,
) -> tuple[bool, str] | None:
    """A human label for one candidate, or None if it has none.

    A user correction always wins over folder placement, whichever
    folder the message happens to sit in right now -- it is the more
    direct evidence and may be why the message moved in the first place.
    """
    if user_feedback_is_spam is not None:
        return user_feedback_is_spam, "user_correction"
    if effective_special_use == "junk":
        return True, "junk_folder"
    if effective_special_use == "inbox":
        return False, "inbox_folder"
    return None


__all__ = ["NeighborHint", "NeighborService"]
