"""Run a real evaluation against a live offering (v0.2 Section 12, weeks
5-6). Refuses to run - rather than fabricate results - when no
OPENROUTER_API_KEY is configured; see app/evals/model_client.py.

Usage:
    .venv/Scripts/python.exe -m scripts.run_eval \\
        --task-profile structured-extraction --version 1 \\
        --offering <offering-uuid>

    .venv/Scripts/python.exe -m scripts.run_eval \\
        --task-profile code-edit --version 1 \\
        --provider-model-name "anthropic/claude-fable-5.2"
"""
import argparse
import sys
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.evals.model_client import NoLiveKeyError, UnsupportedProviderError, build_client_for_offering
from app.evals.runner import BudgetExceededError, UnknownTaskProfileError, get_weekly_spent, run_evaluation
from app.models.catalog import Offering


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real ModelLattice evaluation against a live offering.")
    parser.add_argument("--task-profile", required=True, choices=["structured-extraction", "code-edit"])
    parser.add_argument("--version", type=int, default=1)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--offering", help="Offering UUID")
    group.add_argument("--provider-model-name", help="e.g. 'anthropic/claude-fable-5.2' (OpenRouter offerings only)")
    parser.add_argument("--max-budget-usd", type=float, default=None)
    args = parser.parse_args()

    settings = get_settings()
    db = SessionLocal()
    try:
        print(f"Weekly eval spend so far: ${get_weekly_spent(db):.2f} of ${settings.eval_weekly_budget_usd:.2f}")

        if args.offering:
            offering = db.get(Offering, uuid.UUID(args.offering))
        else:
            offering = db.execute(
                select(Offering).where(Offering.provider_model_name == args.provider_model_name)
            ).scalar_one_or_none()
        if offering is None:
            print("offering not found", file=sys.stderr)
            sys.exit(1)

        try:
            client = build_client_for_offering(offering.provider.name, offering.provider_model_name)
        except NoLiveKeyError as e:
            print(f"Refusing to run: {e}", file=sys.stderr)
            print("No results were written - this is not a fabricated evaluation.", file=sys.stderr)
            sys.exit(2)
        except UnsupportedProviderError as e:
            print(f"Refusing to run: {e}", file=sys.stderr)
            sys.exit(2)

        try:
            report = run_evaluation(
                db, offering, args.task_profile, args.version, client, max_run_budget_usd=args.max_budget_usd
            )
        except BudgetExceededError as e:
            print(f"Refusing to run: {e}", file=sys.stderr)
            sys.exit(3)
        except UnknownTaskProfileError as e:
            print(f"Refusing to run: {e}", file=sys.stderr)
            sys.exit(4)

        print(f"\neval_run_id: {report.eval_run_id}")
        print(f"task_profile: {report.task_profile}")
        print(f"offering: {offering.provider.name} / {offering.provider_model_name}")
        print(f"n_tasks: {report.n_tasks}  pass_rate: {report.pass_rate:.0%}")
        print(f"mean_score: {report.mean_score:.3f}  ci: [{report.ci_low:.3f}, {report.ci_high:.3f}]")
        print(f"spent_usd: ${report.spent_usd:.4f}")
        print(f"measurement_id: {report.measurement_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
