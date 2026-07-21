"""
run_full_sweep.py

Runs the agent loop against all 8 failure-injection scenario types in one
pass, using the CURRENT (fully-fixed) tool set, and prints a summary table
of ground truth vs. conclusion vs. source vs. correctness.

This is meant to be run after any tool-layer or prompt change, as a full
regression check — not a substitute for the eventual formal eval harness
(Phase 5), just a fast sanity sweep across all scenario types at once.

Usage:
    export GITHUB_TOKEN=...
    export CEREBRAS_API_KEY=...   # or whichever backend you're using
    python3 run_full_sweep.py --owner OWNER --repo REPO --backend cerebras

Notes:
- Scenarios with stochastic outcomes (flaky_test, ambiguous_flaky_or_regression)
  do not have a single fixed "correct" conclusion — the script prints the
  observed conclusion and flags these rows for manual/contextual judgment
  rather than a strict pass/fail, consistent with how they've been handled
  in research_notes.md.
- Results are saved to sweep_results.json for later reference, in addition
  to the printed table.
"""

import argparse
import datetime
import json
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from agent.loop import (  # noqa: E402
    ALLOWED_HYPOTHESES,
    BeliefState,
    QUERY_TEST_FAILURE_LOGS_SCHEMA,
    _make_query_test_failure_logs,
    _uniform_prior,
    run_loop,
)

# Import the other tool factories + schemas the same way loop.py's __main__ does.
# If your loop.py uses different names for these, adjust the imports below to match.
try:
    from agent.loop import (  # noqa: E402
        QUERY_CI_RUN_HISTORY_SCHEMA,
        QUERY_FLAKINESS_HISTORY_SCHEMA,
        _make_query_ci_run_history,
        _make_query_flakiness_history,
    )
    HAVE_EXTRA_TOOLS = True
except ImportError:
    HAVE_EXTRA_TOOLS = False
    print(
        "WARNING: could not import query_ci_run_history / query_flakiness_history "
        "tool factories or schemas from agent.loop — check the exact names in your "
        "file and adjust this script's imports. Continuing with query_test_failure_logs only.",
        file=sys.stderr,
    )

from tools.trigger_and_fetch import (  # noqa: E402
    get_latest_run,
    poll_until_complete,
    trigger_run,
)

# Ground truth per scenario. "STOCHASTIC" scenarios have no single fixed
# correct answer — flagged for manual/contextual judgment in the summary.
SCENARIOS = [
    ("none", "healthy"),
    ("flaky_test", "STOCHASTIC"),
    ("real_regression", "real_regression"),
    ("env_dependency", "env_dependency"),
    ("schema_change", "schema_change"),
    ("ci_infra_issue", "ci_infra_issue"),
    ("ambiguous_flaky_or_regression", "ambiguous"),
    ("regression_with_redherring", "real_regression"),
]


def run_one_scenario(owner, repo, failure_type, backend, model, token, tool_budget, llm_timeout):
    print(f"\n{'=' * 60}\nSCENARIO: {failure_type}\n{'=' * 60}")

    trigger_run(owner, repo, failure_type, token)
    print("Waiting for run to register...")
    time.sleep(6)

    run = get_latest_run(owner, repo, token)
    if not run:
        return {"failure_type": failure_type, "error": "no run found"}
    run_id = run["id"]
    print(f"Polling run {run_id}...")
    completed = poll_until_complete(owner, repo, token, run_id)
    print(f"GitHub conclusion: {completed['conclusion']}")

    belief_state = BeliefState(
        current_belief=_uniform_prior(),
        evidence_log=[{
            "init": f"investigating run_id={run_id}",
            "github_conclusion": completed["conclusion"],
        }],
        tool_calls_used=0,
        tool_call_budget=tool_budget,
        status="running",
        run_id=run_id,
    )

    tool_registry = {
        "query_test_failure_logs": _make_query_test_failure_logs(owner, repo, token),
    }
    tool_schemas = [QUERY_TEST_FAILURE_LOGS_SCHEMA]

    if HAVE_EXTRA_TOOLS:
        tool_registry["query_ci_run_history"] = _make_query_ci_run_history(
            owner, repo, token, initial_run_id=run_id
        )
        tool_registry["query_flakiness_history"] = _make_query_flakiness_history(
            owner, repo, token, initial_run_id=run_id
        )
        tool_schemas.append(QUERY_CI_RUN_HISTORY_SCHEMA)
        tool_schemas.append(QUERY_FLAKINESS_HISTORY_SCHEMA)

    final = run_loop(
        belief_state,
        tool_schemas=tool_schemas,
        tool_registry=tool_registry,
        backend=backend,
        model=model,
        timeout=llm_timeout,
    )

    return {
        "failure_type": failure_type,
        "run_id": run_id,
        "github_conclusion": completed["conclusion"],
        "conclusion": final.conclusion,
        "conclusion_source": final.conclusion_source,
        "confidence": max((h["confidence"] for h in final.current_belief), default=None),
        "mismatch_type": final.mismatch_type,
        "tool_calls_used": final.tool_calls_used,
        "tool_call_budget": final.tool_call_budget,
        "belief_trajectory_len": len(final.belief_trajectory),
        "final_belief": final.current_belief,
    }


def main():
    parser = argparse.ArgumentParser(description="Run the full 8-scenario regression sweep.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--backend", default="cerebras")
    parser.add_argument("--model", default=None)
    parser.add_argument("--tool-budget", type=int, default=5)
    parser.add_argument("--llm-timeout", type=int, default=300)
    parser.add_argument(
        "--sleep-between", type=int, default=15,
        help="Seconds to sleep between scenarios to avoid CI queuing / rate limits",
    )
    parser.add_argument(
        "--only", default=None,
        help="Comma-separated list of failure_type values to run, instead of all 8",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)

    from agent.loop import DEFAULT_MODELS  # noqa: E402
    model = args.model or DEFAULT_MODELS.get(args.backend)
    if model is None:
        print(f"ERROR: no default model known for backend {args.backend!r}; pass --model", file=sys.stderr)
        sys.exit(1)

    scenarios = SCENARIOS
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [(ft, gt) for ft, gt in SCENARIOS if ft in wanted]
        if not scenarios:
            print(f"ERROR: none of {wanted} matched known scenarios", file=sys.stderr)
            sys.exit(1)

    results = []
    for i, (failure_type, ground_truth) in enumerate(scenarios):
        result = run_one_scenario(
            args.owner, args.repo, failure_type, args.backend, model,
            token, args.tool_budget, args.llm_timeout,
        )
        result["ground_truth"] = ground_truth
        # flaky_test at flaky_rate=0.5: if the run passed, the correct
        # conclusion is "healthy" (deterministic — no failure = healthy).
        # Only the failure case is truly stochastic (single run can't
        # distinguish flaky from real_regression).
        if (
            ground_truth == "STOCHASTIC"
            and result.get("github_conclusion") == "success"
        ):
            result["ground_truth"] = "healthy"
        results.append(result)

        if i < len(scenarios) - 1:
            print(f"Sleeping {args.sleep_between}s before next scenario...")
            time.sleep(args.sleep_between)

    # --- Summary table ---
    print("\n\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    header = f"{'Scenario':<32} {'Ground Truth':<16} {'Conclusion':<16} {'Source':<24} {'Correct?':<10}"
    print(header)
    print("-" * len(header))
    for r in results:
        if "error" in r:
            print(f"{r['failure_type']:<32} {'ERROR: ' + r['error']}")
            continue
        gt = r["ground_truth"]
        concl = r["conclusion"]
        if gt == "STOCHASTIC":
            correct = "n/a (stochastic)"
        else:
            correct = "YES" if concl == gt else "NO"
        print(f"{r['failure_type']:<32} {gt:<16} {str(concl):<16} {str(r['conclusion_source']):<24} {correct:<10}")

    # --- Save full results ---
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"results/sweep_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
