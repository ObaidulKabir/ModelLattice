"""Periodic data refresh: runs every registered connector
(app/connectors/registry.py), then re-derives relationships from
whatever changed, on a fixed interval. Run as its own long-lived
process (scripts/scheduler_daemon.py) - deliberately separate from the
web process (app.main), so scaling API workers never risks the same
refresh job firing twice at once.

Does NOT schedule the evaluation runner. Evaluations call real paid
APIs; running them automatically in the background, unattended, is a
materially different kind of decision from refreshing a public catalog
listing, and stays a deliberate, explicit action (scripts/run_eval.py)
with its own budget caps - never something a timer triggers on its own.
"""
import datetime as dt
import logging

from app.config import get_settings
from app.connectors.registry import run_all_connectors
from app.db import SessionLocal
from app.relationships.infer import infer_naming_convention_tiers, infer_substitution_from_evidence

logger = logging.getLogger("modellattice.scheduler")


def refresh_connectors_job() -> None:
    """Runs every registered connector. One connector's failure (a
    network error, a changed page format, a provider outage) is logged
    and skipped - it never blocks the others, and never crashes the
    scheduler process itself."""
    results = run_all_connectors()
    for r in results:
        if r.ok:
            s = r.summary
            logger.info(
                "[%s] ok: seen=%d created(providers=%d models=%d offerings=%d) "
                "prices_written=%d measurements_written=%d deprecated=%d",
                r.name, s.offerings_seen, s.providers_created, s.models_created, s.offerings_created,
                s.price_points_written, s.measurements_written, s.offerings_deprecated,
            )
        else:
            logger.error("[%s] FAILED: %s", r.name, r.error)


def refresh_relationships_job() -> None:
    """Re-derives relationships (app/relationships/infer.py) so new
    offerings from the connector run above complete a tier family, and
    any new eval evidence gets a fresh substitution check. Idempotent -
    see app/relationships/infer.py docstring."""
    db = SessionLocal()
    try:
        tiers = infer_naming_convention_tiers(db)
        subs = infer_substitution_from_evidence(db)
        logger.info(
            "relationships: tier_edges_written=%d substitution_edges_written=%d (pairs_considered=%d)",
            tiers.tier_edges_written, subs.substitution_edges_written, subs.pairs_considered,
        )
    except Exception:
        logger.exception("relationship inference failed")
    finally:
        db.close()


def refresh_all_job() -> None:
    """The one scheduled job: connectors, then relationships, in that
    order, since new models/prices can complete a tier family."""
    refresh_connectors_job()
    refresh_relationships_job()


def build_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        refresh_all_job, "interval", hours=settings.connector_refresh_interval_hours,
        id="refresh_all", max_instances=1, coalesce=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) if settings.scheduler_run_immediately_on_start else None,
    )
    return scheduler
