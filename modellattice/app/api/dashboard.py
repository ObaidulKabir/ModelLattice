"""Internal dashboard (v0.2 Section 12, week 4 "internal dashboard" /
week 10 "savings and quality dashboard"). Server-rendered, not a SPA -
this is an admin/ops tool for browsing the real catalog, pricing,
evidence and relationships already built, not a customer-facing
surface. Every number here comes straight from the same service
functions the REST API and MCP server use (app/services/*,
app/monitoring/regression.py) - no logic is duplicated.
"""
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.catalog import Model, Offering, Provider
from app.models.evaluation import EvalResult, EvalRun
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.relationship import Relationship
from app.models.types import OfferingLifecycle
from app.monitoring.regression import check_regression
from app.services.alternatives import find_alternatives
from app.services.cost import get_current_prices, pricing_freshness
from app.attribute_registry import RANKABLE_DIMENSIONS
from app.services.ranking import COST_DIMENSION, COST_KEY, MODES, DimensionWeight, rank_offerings, rank_sort_key
from app.version import get_build_version

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["build_version"] = get_build_version()

PAGE_SIZE = 50


def _count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    offerings_total = _count(db, Offering)
    ga_count = db.execute(
        select(func.count()).select_from(Offering).where(Offering.lifecycle == OfferingLifecycle.GENERALLY_AVAILABLE.value)
    ).scalar_one()
    deprecated_count = db.execute(
        select(func.count()).select_from(Offering).where(Offering.lifecycle == OfferingLifecycle.DEPRECATED.value)
    ).scalar_one()

    freshness = pricing_freshness(db)

    stats = {
        "providers": _count(db, Provider),
        "models": _count(db, Model),
        "offerings": offerings_total,
        "offerings_ga": ga_count,
        "offerings_deprecated": deprecated_count,
        "price_points": _count(db, PricePoint),
        "measurements": _count(db, Measurement),
        "relationships": _count(db, Relationship),
        "eval_runs": _count(db, EvalRun),
        "eval_results": _count(db, EvalResult),
        "freshness": freshness,
    }

    recent_evidence = db.execute(
        select(EvidenceSource).order_by(EvidenceSource.collected_at.desc()).limit(15)
    ).scalars().all()

    host_breakdown = db.execute(
        select(Provider.name, func.count(Offering.id))
        .join(Offering, Offering.provider_id == Provider.id)
        .group_by(Provider.name)
        .order_by(func.count(Offering.id).desc())
    ).all()

    return templates.TemplateResponse(
        request, "overview.html",
        {"stats": stats, "recent_evidence": recent_evidence, "host_breakdown": host_breakdown},
    )


@router.get("/offerings", response_class=HTMLResponse)
def offerings_list(
    request: Request,
    provider: str | None = Query(default=None),
    lifecycle: str | None = Query(default=None),
    q: str | None = Query(default=None),
    dim: str | None = Query(default=None, description="rank the list by this capability dimension"),
    mode: str = Query(default="relative"),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    stmt = select(Offering).options(
        selectinload(Offering.model).selectinload(Model.developer), selectinload(Offering.provider)
    )
    if provider:
        stmt = stmt.join(Provider, Provider.id == Offering.provider_id).where(Provider.name == provider)
    if lifecycle:
        stmt = stmt.where(Offering.lifecycle == lifecycle)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Model, Model.id == Offering.model_id).where(
            (Model.family.ilike(like)) | (Offering.provider_model_name.ilike(like))
        )

    all_offerings = db.execute(stmt).scalars().unique().all()

    # Ranking runs over the FULL filtered set before pagination, so relative
    # scores don't shift from page to page. A blank/unknown `dim` (or bad
    # `mode`) degrades to the default order rather than erroring.
    dim_options = {**RANKABLE_DIMENSIONS, COST_KEY: COST_DIMENSION}
    if mode not in MODES:
        mode = "relative"
    active_dim = dim if dim in dim_options else None
    ranked = {}
    if active_dim:
        ranked = rank_offerings(db, all_offerings, [DimensionWeight(active_dim, 1.0)], mode)
        all_offerings.sort(key=lambda o: (rank_sort_key(ranked[o.id]), o.model.family, o.provider.name))
    else:
        all_offerings.sort(key=lambda o: (o.model.family, o.provider.name))
    total = len(all_offerings)
    start = (page - 1) * PAGE_SIZE
    page_offerings = all_offerings[start : start + PAGE_SIZE]

    rows = []
    for o in page_offerings:
        prices = get_current_prices(db, o.id)
        inp = prices.get("input_token")
        out = prices.get("output_token")
        rows.append(
            {
                "id": o.id, "family": o.model.family, "developer": o.model.developer.name if o.model.developer else "—",
                "host": o.provider.name, "name": o.provider_model_name, "lifecycle": o.lifecycle,
                "input_price": inp.amount if inp else None, "output_price": out.amount if out else None,
                "context_window": (o.limits or {}).get("max_context_tokens"),
                "tool_calling": (o.api_features or {}).get("tool_calling"),
                "json_schema": (o.api_features or {}).get("json_schema_output"),
                "dim_value": ranked[o.id].dimension_scores[0].raw_value if active_dim else None,
                "dim_score": ranked[o.id].composite_score if active_dim else None,
            }
        )

    providers = db.execute(select(Provider.name).order_by(Provider.name)).scalars().all()

    return templates.TemplateResponse(
        request, "offerings.html",
        {
            "rows": rows, "total": total, "page": page, "page_size": PAGE_SIZE,
            "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "providers": providers, "filter_provider": provider, "filter_lifecycle": lifecycle, "filter_q": q or "",
            "dim_options": dim_options, "filter_dim": active_dim, "filter_mode": mode,
        },
    )


@router.get("/offerings/{offering_id}", response_class=HTMLResponse)
def offering_detail(request: Request, offering_id: uuid.UUID, db: Session = Depends(get_db)):
    offering = db.get(Offering, offering_id)
    if offering is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    sibling_offerings = db.execute(
        select(Offering).options(selectinload(Offering.provider)).where(Offering.model_id == offering.model_id)
    ).scalars().all()

    prices = db.execute(
        select(PricePoint).where(PricePoint.offering_id == offering_id).order_by(PricePoint.effective_from.desc()).limit(20)
    ).scalars().all()

    measurements = db.execute(
        select(Measurement).where(
            ((Measurement.subject_type == "offering") & (Measurement.subject_id == offering_id))
            | ((Measurement.subject_type == "model") & (Measurement.subject_id == offering.model_id))
        )
        .order_by(Measurement.observed_at.desc()).limit(30)
    ).scalars().all()

    alternatives = find_alternatives(db, offering)

    eval_results = db.execute(
        select(EvalResult, EvalRun.task_profile_key, EvalRun.task_profile_version)
        .join(EvalRun, EvalRun.id == EvalResult.eval_run_id)
        .where(EvalRun.offering_id == offering_id)
        .order_by(EvalResult.created_at.desc()).limit(20)
    ).all()

    regression = None
    try:
        regression = check_regression(db, offering_id, window=dt.timedelta(days=7))
    except Exception:
        regression = None

    return templates.TemplateResponse(
        request, "offering_detail.html",
        {
            "offering": offering, "siblings": sibling_offerings, "prices": prices, "measurements": measurements,
            "alternatives": alternatives, "eval_results": eval_results, "regression": regression,
        },
    )


@router.get("/evidence", response_class=HTMLResponse)
def evidence_list(request: Request, page: int = Query(default=1, ge=1), db: Session = Depends(get_db)):
    total = _count(db, EvidenceSource)
    rows = db.execute(
        select(EvidenceSource).order_by(EvidenceSource.collected_at.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).scalars().all()
    return templates.TemplateResponse(
        request, "evidence.html",
        {"rows": rows, "total": total, "page": page, "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)},
    )
