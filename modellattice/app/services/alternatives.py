"""Shared alternatives resolution for GET /v1/alternatives and the MCP
get_alternatives tool, so the bidirectional-traversal logic (v0.1 Section
6.3) lives in exactly one place.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Offering
from app.models.relationship import Relationship
from app.services.recommender import evaluate_offerings


def _representative_offering_id(db: Session, model_id: uuid.UUID) -> uuid.UUID | None:
    """A model-level relationship (e.g. a naming-convention tier edge)
    names a Model, which may expose several Offerings (different hosts,
    or free/paid tiers of the same host). Picks one to show: prefer a
    generally-available offering, else any."""
    rows = db.execute(select(Offering).where(Offering.model_id == model_id)).scalars().all()
    if not rows:
        return None
    ga = [o for o in rows if o.lifecycle == "generally_available"]
    return (ga or rows)[0].id


def find_alternatives(db: Session, offering: Offering) -> list[dict]:
    """Substitutes, tier up/down and lineage relationships for an
    offering. Looks both forward AND backward (v0.1 Section 6.3:
    "bidirectional navigation" - an edge A->B must be traversable from
    B's side too, using the reverse_profile, which is independently
    worded rather than assumed equivalent)."""
    subject_ids = {offering.id, offering.model_id}
    rels = (
        db.execute(
            select(Relationship).where(
                (Relationship.source_id.in_(subject_ids) | Relationship.target_id.in_(subject_ids)),
                Relationship.lifecycle_status == "active",
            )
        )
        .scalars()
        .all()
    )
    if not rels:
        return []

    resolved: list[tuple[Relationship, bool, uuid.UUID | None]] = []
    for r in rels:
        traversed_forward = r.source_id in subject_ids
        other_id = r.target_id if traversed_forward else r.source_id
        other_type = r.target_type if traversed_forward else r.source_type
        other_offering_id = other_id if other_type == "offering" else _representative_offering_id(db, other_id)
        resolved.append((r, traversed_forward, other_offering_id))

    target_ids = [oid for _, _, oid in resolved if oid]
    candidates_by_id = {c.offering.id: c for c in evaluate_offerings(db, target_ids, None)}

    out = []
    for r, traversed_forward, other_offering_id in resolved:
        cand = candidates_by_id.get(other_offering_id) if other_offering_id else None
        out.append(
            {
                "relationship_type": r.type,
                "direction": "forward" if traversed_forward else "reverse",
                "confidence": r.confidence,
                "profile": r.forward_profile if traversed_forward else r.reverse_profile,
                "target_offering_id": other_offering_id if cand else None,
                "target": (
                    {
                        "provider_name": cand.provider_name,
                        "model_name": cand.model_name,
                        "est_cost_per_1k_tasks_usd": cand.cost_per_1k_tasks_usd,
                        "quality_score": cand.quality_score,
                    }
                    if cand
                    else None
                ),
            }
        )
    return out
