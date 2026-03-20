"""
Verdict API endpoints.

GET /api/verdicts — verdict history
GET /api/mails/:id/verdict — latest verdict for a mail
POST /api/mails/:id/feedback — submit user spam feedback
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select

from mail_verdict.api.deps import get_verdict_repo
from mail_verdict.api.schemas import FeedbackRequest, FeedbackResponse, VerdictResponse
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Mail, Verdict

logger = logging.getLogger(__name__)

router = APIRouter(tags=["verdicts"])


@router.get("/verdicts", response_model=list[VerdictResponse])
async def list_verdicts(
    account_id: uuid.UUID | None = Query(default=None),
    mail_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[VerdictResponse]:
    """List verdicts with optional filters."""
    db = get_db_connection()
    async with db.session() as session:
        stmt = select(Verdict).order_by(desc(Verdict.created_at))

        if mail_id is not None:
            stmt = stmt.where(Verdict.mail_id == mail_id)
        if account_id is not None:
            stmt = stmt.join(Mail, Verdict.mail_id == Mail.id).where(Mail.account_id == account_id)

        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        verdicts = list(result.scalars().all())

    return [
        VerdictResponse(
            id=v.id,
            mail_id=v.mail_id,
            is_spam=v.is_spam,
            model_used=v.model_used,
            reasoning=v.reasoning,
            source=v.source.value,
            created_at=v.created_at,
        )
        for v in verdicts
    ]


@router.get("/mails/{mail_id}/verdict", response_model=VerdictResponse | None)
async def get_mail_verdict(
    mail_id: uuid.UUID,
) -> VerdictResponse | None:
    """Get the latest verdict for a specific mail."""
    verdict_repo = get_verdict_repo()
    verdict = await verdict_repo.get_latest_for_mail(mail_id)
    if verdict is None:
        return None

    return VerdictResponse(
        id=verdict.id,
        mail_id=verdict.mail_id,
        is_spam=verdict.is_spam,
        model_used=verdict.model_used,
        reasoning=verdict.reasoning,
        source=verdict.source.value,
        created_at=verdict.created_at,
    )


@router.post("/mails/{mail_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    mail_id: uuid.UUID,
    request: FeedbackRequest,
    account_id: uuid.UUID = Query(),
) -> FeedbackResponse:
    """
    Submit user feedback on spam classification.

    Triggers SpamFeedbackHandler to update Qdrant tag and log correction verdict.
    """
    from mail_verdict.api.deps import get_mail_repo

    mail_repo = get_mail_repo()
    mail = await mail_repo.get_by_id(account_id, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    # Access SpamFeedbackHandler from server state
    from mail_verdict.server import get_spam_processor

    processor = get_spam_processor()
    if processor is None or processor._feedback is None:
        raise HTTPException(status_code=503, detail="Spam feedback handler not available")

    feedback = processor._feedback
    if request.is_spam:
        ok = await feedback.handle_moved_to_spam(mail_id, account_id)
    else:
        ok = await feedback.handle_moved_from_spam(mail_id, account_id)

    return FeedbackResponse(
        success=ok,
        mail_id=mail_id,
        is_spam=request.is_spam,
        message="Feedback recorded" if ok else "Feedback processing failed",
    )
