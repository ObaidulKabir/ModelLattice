"""Seed data for local development and the week-1 demo.

Populates: the attribute registry (a slice of UMIM D01-D12 attributes
actually used by the MVP, v0.2 Section 5.2), a handful of providers/
models/offerings with current pricing, one task profile ("structured-
extraction@1", v0.2 Section 9), one eval run per offering with a
task_quality_score measurement, and one lineage + one substitution
relationship so /v1/alternatives has something to return.

All figures below are illustrative placeholders for development, not real
provider pricing or measured quality - replace via the real connectors and
evaluation runner (v0.2 Section 12, weeks 1-6).

Run with: .venv/Scripts/python.exe -m scripts.seed
"""
import datetime as dt
import uuid

from app.attribute_registry import ensure_attribute_definitions
from app.db import Base, SessionLocal, engine
from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.decision import TaskProfile
from app.models.evaluation import EvalResult, EvalRun
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.relationship import Relationship
from app.models.types import EvidenceType, MeasurementStatus, PriceType, RelationshipType, SubjectType

NOW = dt.datetime.now(dt.timezone.utc)


def seed():
    Base.metadata.create_all(bind=engine)  # safety net; Alembic already created these
    db = SessionLocal()
    try:
        if db.query(Provider).count() > 0:
            print("Seed data already present; skipping. Delete rows manually to reseed.")
            return

        ensure_attribute_definitions(db)

        doc_evidence = EvidenceSource(
            type=EvidenceType.OFFICIAL_DOC.value,
            uri="https://example.invalid/provider-docs",
            method="manual review of published documentation",
            collected_at=NOW,
        )
        db.add(doc_evidence)
        db.flush()

        providers = {
            name: Provider(name=name, type="both", urls={"docs": f"https://example.invalid/{name.lower()}"})
            for name in ["ProviderAlpha", "ProviderBeta", "ProviderGamma"]
        }
        db.add_all(providers.values())
        db.flush()

        # Two model families, one with a lineage parent -> child (distilled/faster) pair.
        model_flagship = Model(
            family="Flagship-Reasoner", version="2026-09", developer_id=providers["ProviderAlpha"].id,
            modalities=["text", "image"], release_date=dt.date(2026, 9, 1),
        )
        db.add(model_flagship)
        db.flush()

        model_fast = Model(
            family="Flagship-Reasoner-Fast", version="2026-09", developer_id=providers["ProviderAlpha"].id,
            lineage_parent_id=model_flagship.id, modalities=["text"], release_date=dt.date(2026, 9, 15),
        )
        model_rival = Model(
            family="Rival-Pro", version="3.1", developer_id=providers["ProviderBeta"].id,
            modalities=["text", "image"], release_date=dt.date(2026, 8, 1),
        )
        model_open = Model(
            family="Open-Instruct", version="70B", developer_id=providers["ProviderGamma"].id,
            modalities=["text"], release_date=dt.date(2026, 6, 1),
        )
        db.add_all([model_fast, model_rival, model_open])
        db.flush()

        def make_offering(model, provider, name, features, limits, deployments):
            off = Offering(
                model_id=model.id, provider_id=provider.id, provider_model_name=name,
                api_features=features, limits=limits,
            )
            db.add(off)
            db.flush()
            for region, residency in deployments:
                db.add(Deployment(offering_id=off.id, region=region, data_residency=residency))
            return off

        off_flagship = make_offering(
            model_flagship, providers["ProviderAlpha"], "flagship-reasoner-2026-09",
            {"tool_calling": True, "json_schema_output": True, "no_training_on_inputs": True},
            {"max_context_tokens": 200000},
            [("us-east", "US"), ("eu-west", "EU")],
        )
        off_fast = make_offering(
            model_fast, providers["ProviderAlpha"], "flagship-reasoner-fast-2026-09",
            {"tool_calling": True, "json_schema_output": True, "no_training_on_inputs": True},
            {"max_context_tokens": 128000},
            [("us-east", "US"), ("eu-west", "EU")],
        )
        off_rival = make_offering(
            model_rival, providers["ProviderBeta"], "rival-pro-3.1",
            {"tool_calling": True, "json_schema_output": False, "no_training_on_inputs": True},
            {"max_context_tokens": 128000},
            [("us-east", "US")],
        )
        off_open = make_offering(
            model_open, providers["ProviderGamma"], "open-instruct-70b",
            {"tool_calling": False, "json_schema_output": False, "no_training_on_inputs": True},
            {"max_context_tokens": 64000},
            [("eu-central", "EU")],
        )

        # Pricing (USD per 1M tokens). Placeholder figures for development only.
        pricing = {
            off_flagship: (5.00, 20.00),
            off_fast: (0.80, 3.20),
            off_rival: (4.50, 18.00),
            off_open: (0.20, 0.60),
        }
        for off, (inp, out) in pricing.items():
            db.add(PricePoint(offering_id=off.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=inp, effective_from=NOW, source_evidence_id=doc_evidence.id))
            db.add(PricePoint(offering_id=off.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=out, effective_from=NOW, source_evidence_id=doc_evidence.id))

        task_profile = TaskProfile(
            key="structured-extraction", version=1, label="Structured extraction",
            definition={"avg_input_tokens": 500, "avg_output_tokens": 300},
            metrics={"primary": "field_level_f1_and_schema_validity"},
        )
        db.add(task_profile)
        db.flush()

        eval_evidence = EvidenceSource(
            type=EvidenceType.PLATFORM_EVAL.value, method="scheduled platform evaluation run",
            collected_at=NOW,
        )
        db.add(eval_evidence)
        db.flush()

        # One eval run + aggregate quality measurement per offering, except
        # off_open, which is left with NO quality measurement on purpose:
        # this is how "unknown" (not a zero score) shows up in /v1/recommend.
        quality_scores = {off_flagship: (0.93, 0.90, 0.96), off_fast: (0.86, 0.82, 0.90), off_rival: (0.89, 0.85, 0.92)}
        for off, (score, lo, hi) in quality_scores.items():
            run = EvalRun(
                task_profile_key="structured-extraction", task_profile_version=1, offering_id=off.id,
                judge="cross-provider-judge-v1", budget_usd=5.0, spent_usd=4.1,
                started_at=NOW, finished_at=NOW,
            )
            db.add(run)
            db.flush()
            for i in range(3):
                db.add(EvalResult(eval_run_id=run.id, task_id=f"task-{i}", score=score, passed=True, cost_usd=0.02, latency_ms=1800))
            db.add(
                Measurement(
                    subject_type=SubjectType.OFFERING.value, subject_id=off.id,
                    attribute_key="task_quality_score", value={"raw": score, "ci": [lo, hi]},
                    status=MeasurementStatus.SUPPORTED.value,
                    context={"task_profile": "structured-extraction@1"},
                    evidence_id=eval_evidence.id, confidence=0.8, sample_size=50, maturity="S3",
                    observed_at=NOW,
                )
            )
            db.add(
                Measurement(
                    subject_type=SubjectType.OFFERING.value, subject_id=off.id,
                    attribute_key="p95_latency_ms", value={"raw": 1800 if off is not off_fast else 900},
                    status=MeasurementStatus.SUPPORTED.value, context={}, evidence_id=eval_evidence.id,
                    observed_at=NOW,
                )
            )

        # Relationships: fast model is a lineage child and a downward tier
        # substitute of the flagship; rival is a lateral substitute.
        db.add(
            Relationship(
                type=RelationshipType.LINEAGE.value, source_type=SubjectType.MODEL.value, source_id=model_fast.id,
                target_type=SubjectType.MODEL.value, target_id=model_flagship.id,
                forward_profile={"relation": "distilled_from"}, evidence_ids=[], confidence=1.0,
            )
        )
        db.add(
            Relationship(
                type=RelationshipType.TIER_DOWN.value, source_type=SubjectType.OFFERING.value, source_id=off_flagship.id,
                target_type=SubjectType.OFFERING.value, target_id=off_fast.id,
                forward_profile={"task_profile": "structured-extraction@1", "quality_delta": -0.07, "cost_delta_pct": -84},
                reverse_profile={"task_profile": "structured-extraction@1", "note": "fast model cannot reliably escalate to flagship quality"},
                conditions={"task_categories": ["structured-extraction"]},
                evidence_ids=[], confidence=0.7,
            )
        )
        db.add(
            Relationship(
                type=RelationshipType.SUBSTITUTION.value, source_type=SubjectType.OFFERING.value, source_id=off_flagship.id,
                target_type=SubjectType.OFFERING.value, target_id=off_rival.id,
                forward_profile={"task_profile": "structured-extraction@1", "note": "comparable quality, different provider"},
                reverse_profile={"task_profile": "structured-extraction@1", "note": "assessed independently; not assumed symmetric"},
                conditions={"task_categories": ["structured-extraction"]},
                evidence_ids=[], confidence=0.6,
            )
        )

        db.commit()
        print("Seed complete:")
        print(f"  providers: {len(providers)}")
        print(f"  offerings: 4 (flagship={off_flagship.id}, fast={off_fast.id}, rival={off_rival.id}, open={off_open.id})")
        print("  task_profile: structured-extraction@1")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
