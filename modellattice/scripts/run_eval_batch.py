"""Spend one approved budget across MANY offerings in a single,
still-manual invocation (v0.2 Section 12, closing the real coverage gap:
546 offerings x 5 task profiles, ~8 genuine live-evaluation data points
today). This does not change the project's standing rule that evals are
a deliberate, explicit action, never an automatic background one
(app/config.py) - it's a second manual command, not a scheduler hook.

All 5 task profiles are supported. `rag-qa`/`summarization` are judge-
scored and need `--judge-provider-model-name` (or `--judge-offering`):
ONE judge, explicitly chosen by you, reused for every candidate in the
batch - exactly the same judge-required rule scripts/run_eval.py already
enforces for a single call, just applied once per batch instead of once
per invocation. Picking a good judge model is a quality judgment call
this project has never auto-decided anywhere; batching doesn't change
that. OpenRouter-evaluable offerings only (matching what
app/evals/model_client.py can call live today); non-OpenRouter providers
stay single-invocation via scripts/run_eval.py.

Usage:
    .venv/Scripts/python.exe -m scripts.run_eval_batch \\
        --task-profile structured-extraction --version 1 \\
        --max-total-budget-usd 5.00 --limit 50 [--dry-run]

    .venv/Scripts/python.exe -m scripts.run_eval_batch \\
        --task-profile rag-qa --max-total-budget-usd 5.00 --limit 20 \\
        --judge-provider-model-name "openai/gpt-5"

Selection (app/evals/batch.py::select_targets): offerings with ZERO
task_quality_score evidence for this exact profile@version, cheapest
known price first - so a shared budget stretches across the most
offerings before it runs out. For a judge-scored profile, the judge's
own offering is excluded from selection (a model can never judge
itself). The price preview shown below (and by --dry-run) is the
CANDIDATE's cost only - it does not add the judge's own call cost, which
depends on actual response length, not a fixed per-call estimate.
--max-total-budget-usd is a ceiling on top of (never a replacement for)
the existing EVAL_WEEKLY_BUDGET_USD / EVAL_MAX_RUN_BUDGET_USD caps that
app/evals/runner.py::run_evaluation still enforces on every single call
(including the judge's own spend, which it already folds into that
call's reported cost).
"""
import argparse
import sys
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.evals.batch import coverage_count, run_batch, select_targets
from app.evals.model_client import NoLiveKeyError, UnsupportedProviderError, build_client_for_offering
from app.evals.runner import get_weekly_spent
from app.models.catalog import Offering

ALL_PROFILES = ["structured-extraction", "code-edit", "tool-use-agent", "rag-qa", "summarization"]
JUDGE_PROFILES = {"rag-qa", "summarization"}


def _find_offering(db, offering_arg, provider_model_name_arg):
    if offering_arg:
        return db.get(Offering, uuid.UUID(offering_arg))
    return db.execute(select(Offering).where(Offering.provider_model_name == provider_model_name_arg)).scalar_one_or_none()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real ModelLattice evaluations across many offerings in one budget-gated batch.")
    parser.add_argument("--task-profile", required=True, choices=ALL_PROFILES)
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--max-total-budget-usd", type=float, required=True)
    parser.add_argument("--limit", type=int, default=50, help="max number of offerings to evaluate this batch")
    parser.add_argument("--dry-run", action="store_true", help="show what would run; spend nothing")
    parser.add_argument("--judge-offering", help="Judge's offering UUID (rag-qa/summarization only)")
    parser.add_argument("--judge-provider-model-name", help="Judge's provider model name (rag-qa/summarization only)")
    args = parser.parse_args()

    if args.task_profile in JUDGE_PROFILES and not (args.judge_offering or args.judge_provider_model_name):
        parser.error(f"{args.task_profile!r} is judge-scored: pass --judge-offering or --judge-provider-model-name")
    if args.task_profile not in JUDGE_PROFILES and (args.judge_offering or args.judge_provider_model_name):
        parser.error(f"{args.task_profile!r} is not judge-scored: --judge-offering/--judge-provider-model-name do not apply")

    settings = get_settings()
    db = SessionLocal()
    try:
        print(f"Weekly eval spend so far: ${get_weekly_spent(db):.2f} of ${settings.eval_weekly_budget_usd:.2f}")
        before = coverage_count(db, args.task_profile, args.version)
        print(f"Current coverage for {args.task_profile}@{args.version}: {before} offerings")

        judge_offering = None
        if args.task_profile in JUDGE_PROFILES:
            judge_offering = _find_offering(db, args.judge_offering, args.judge_provider_model_name)
            if judge_offering is None:
                print("judge offering not found", file=sys.stderr)
                sys.exit(1)
            print(f"Judge for this batch: {judge_offering.provider.name} / {judge_offering.provider_model_name}")

        targets = select_targets(
            db, args.task_profile, args.version, args.limit,
            exclude_offering_id=judge_offering.id if judge_offering else None,
        )
        if not targets:
            print("No zero-coverage OpenRouter offerings left to evaluate for this profile - nothing to do.")
            return

        print(f"\nSelected {len(targets)} offering(s), cheapest known price first (candidate cost only - excludes judge cost):")
        for t in targets:
            price = f"${t.known_input_price_per_1m_usd:.4f}/1M in" if t.known_input_price_per_1m_usd is not None else "price unknown"
            print(f"  {t.offering.provider_model_name}  ({price})")

        if args.dry_run:
            print("\n--dry-run: no evaluations were run, no budget was spent.")
            return

        def _client_factory(offering):
            try:
                return build_client_for_offering(offering.provider.name, offering.provider_model_name)
            except (NoLiveKeyError, UnsupportedProviderError) as e:
                print(f"\nRefusing to run: {e}", file=sys.stderr)
                sys.exit(2)

        judge_client = _client_factory(judge_offering) if judge_offering else None

        report = run_batch(
            db, targets, args.task_profile, args.version, args.max_total_budget_usd, _client_factory,
            judge_client=judge_client, judge_offering=judge_offering,
        )

        for name, r in report.evaluated:
            print(f"  {name}: n_tasks={r.n_tasks} mean_score={r.mean_score:.3f} spent=${r.spent_usd:.4f}")

        print(f"\nEvaluated: {len(report.evaluated)}   Skipped (budget): {len(report.skipped_budget)}")
        print(f"Total spent this batch: ${report.total_spent_usd:.4f} of ${args.max_total_budget_usd:.2f}")
        print(f"Coverage for {args.task_profile}@{args.version}: {report.coverage_before} -> {report.coverage_after}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
