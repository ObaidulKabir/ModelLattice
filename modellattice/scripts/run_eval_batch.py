"""Spend one approved budget across MANY offerings in a single,
still-manual invocation (v0.2 Section 12, closing the real coverage gap:
546 offerings x 5 task profiles, ~8 genuine live-evaluation data points
today). This does not change the project's standing rule that evals are
a deliberate, explicit action, never an automatic background one
(app/config.py) - it's a second manual command, not a scheduler hook.

v1 scope, deliberately: deterministic task profiles only
(structured-extraction, code-edit, tool-use-agent - no judge model
needed), OpenRouter-evaluable offerings only (matching what
app/evals/model_client.py can call live today). rag-qa/summarization
and non-OpenRouter providers stay single-invocation via
scripts/run_eval.py.

Usage:
    .venv/Scripts/python.exe -m scripts.run_eval_batch \\
        --task-profile structured-extraction --version 1 \\
        --max-total-budget-usd 5.00 --limit 50 [--dry-run]

Selection (app/evals/batch.py::select_targets): offerings with ZERO
task_quality_score evidence for this exact profile@version, cheapest
known price first - so a shared budget stretches across the most
offerings before it runs out. --max-total-budget-usd is a ceiling on
top of (never a replacement for) the existing EVAL_WEEKLY_BUDGET_USD /
EVAL_MAX_RUN_BUDGET_USD caps that app/evals/runner.py::run_evaluation
still enforces on every single call.
"""
import argparse
import sys

from app.config import get_settings
from app.db import SessionLocal
from app.evals.batch import coverage_count, run_batch, select_targets
from app.evals.model_client import NoLiveKeyError, UnsupportedProviderError, build_client_for_offering
from app.evals.runner import get_weekly_spent

NON_JUDGE_PROFILES = ["structured-extraction", "code-edit", "tool-use-agent"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real ModelLattice evaluations across many offerings in one budget-gated batch.")
    parser.add_argument("--task-profile", required=True, choices=NON_JUDGE_PROFILES)
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--max-total-budget-usd", type=float, required=True)
    parser.add_argument("--limit", type=int, default=50, help="max number of offerings to evaluate this batch")
    parser.add_argument("--dry-run", action="store_true", help="show what would run; spend nothing")
    args = parser.parse_args()

    settings = get_settings()
    db = SessionLocal()
    try:
        print(f"Weekly eval spend so far: ${get_weekly_spent(db):.2f} of ${settings.eval_weekly_budget_usd:.2f}")
        before = coverage_count(db, args.task_profile, args.version)
        print(f"Current coverage for {args.task_profile}@{args.version}: {before} offerings")

        targets = select_targets(db, args.task_profile, args.version, args.limit)
        if not targets:
            print("No zero-coverage OpenRouter offerings left to evaluate for this profile - nothing to do.")
            return

        print(f"\nSelected {len(targets)} offering(s), cheapest known price first:")
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

        report = run_batch(db, targets, args.task_profile, args.version, args.max_total_budget_usd, _client_factory)

        for name, r in report.evaluated:
            print(f"  {name}: n_tasks={r.n_tasks} mean_score={r.mean_score:.3f} spent=${r.spent_usd:.4f}")

        print(f"\nEvaluated: {len(report.evaluated)}   Skipped (budget): {len(report.skipped_budget)}")
        print(f"Total spent this batch: ${report.total_spent_usd:.4f} of ${args.max_total_budget_usd:.2f}")
        print(f"Coverage for {args.task_profile}@{args.version}: {report.coverage_before} -> {report.coverage_after}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
