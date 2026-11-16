"""POST /v1/feedback: UMFR v0 ingest (v0.1 Section 10; v0.2 Decision C9).

Write-protected by a simple API key (Decision: Section 17 defers real
multi-tenant auth). Metadata-only: the schema has no field for raw prompts
or responses, so there is nothing here to redact.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_write_key
from app.db import get_db
from app.models.feedback import FeedbackEvent
from app.schemas.feedback import FeedbackIn, FeedbackOut

router = APIRouter(prefix="/v1", tags=["feedback"])


@router.post("/feedback", response_model=FeedbackOut, dependencies=[Depends(require_write_key)])
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)):
    event = FeedbackEvent(**payload.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return FeedbackOut(id=event.id, ingested_at=event.ingested_at)
