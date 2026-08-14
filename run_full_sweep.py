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
- Ground truth is derived from github_conclusion, not scenario name: if the
  run didn't actually fire (github_conclusion == "success"), ground truth is
  "healthy" regardless of what failure type the scenario was configured to
  inject. Only fired runs are scored against the scenario's injected type.
- Stochastic scenarios (flaky_test, ambiguous_flaky_or_regression) can go
  either way on a single run. Non-firing runs of these scenarios are scored
  as "healthy" like any other non-firing run, and flagged via
  scenario_did_not_trigger in each result / "[did not trigger]" in the
  summary table, so they're visible without being excluded from scoring.
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
    build_tool_registry,
    _uniform_prior,
    run_loop,
)
from agent.baselines import (  # noqa: E402
    run_fixed_order_baseline,
    run_single_shot_baseline,
)

from tools.trigger_and_fetch import (  # noqa: E402
    get_latest_run,
    poll_until_complete,
    trigger_run,
)

# Fixed (tool_name, param_fn) sequence shared by both RQ1 baselines — the
# harness executes these directly, bypassing the model's own tool choice.
# param_fn receives (evidence_log_so_far, run_id).
#
# test_id is derived mechanically from tool #1's (query_test_failure_logs)
# failures_detail — NOT hardcoded — so "fixed order" means a fixed SELECTION
# of which tool runs when, not a fixed parameter value.
#
# Known-limitation interaction (docs/research_notes.md): query_flakiness_history
# undercounts failures among historical runs whose OWN evidence lacks
# failures_detail (e.g. other no_test_output-type runs like ci_infra_issue) —
# those runs have had_failures=True but an empty failures_detail list, which
# falls through to "passed" in query_flakiness_history's outcome logic
# regardless of what test_id is queried. This affects both baselines below
# identically to how it would affect the full loop whenever the CURRENT run
# is itself a no_test_output type (test_id will be "" here, and/or the
# historical sample will undercount independent of that value).
CI_FIXED_ORDER = [
    ("query_test_failure_logs", lambda ev, run_id: {"run_id": run_id}),
    ("query_ci_run_history", lambda ev, run_id: {"limit": 10}),
    ("query_flakiness_history", lambda ev, run_id: {
        "test_id": (ev[-2]["result"].get("failures_detail") or [{}])[0].get("test_id", ""),
        "run_ids": [r["run_id"] for r in ev[-1]["result"].get("runs", [])],
    }),
]

# Ground truth per scenario, i.e. the injected failure type when the run
# actually fires (github_conclusion == "failure"). When a scenario doesn't
# fire (github_conclusion == "success"), ground truth is "healthy" regardless
# of scenario — see the override in main() below.
SCENARIOS = [
    ("none", "healthy"),
    ("flaky_test", "flaky_test"),
    ("real_regression", "real_regression"),
    ("env_dependency", "env_dependency"),
    ("schema_change", "schema_change"),
    ("ci_infra_issue", "ci_infra_issue"),
    ("ambiguous_flaky_or_regression", "ambiguous"),
    ("regression_with_redherring", "real_regression"),
]

# Scenario types whose trigger is probabilistic — a single run may or may not
# actually fire the injected failure. Used only to flag non-firing runs for
# the results table, not to exclude them from scoring.
STOCHASTIC_SCENARIO_TYPES = {"flaky_test", "ambiguous_flaky_or_regression"}


def run_one_scenario(owner, repo, failure_type, backend, model, token, tool_budget, llm_timeout, mode="full"):
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
        mode=mode,
    )

    tool_registry, tool_schemas = build_tool_registry(owner, repo, token, run_id)

    if mode in ("full", "no_backtrack"):
        final = run_loop(
            belief_state,
            tool_schemas=tool_schemas,
            tool_registry=tool_registry,
            backend=backend,
            model=model,
            timeout=llm_timeout,
        )
    elif mode == "single_shot":
        # No real "budget" concept for this baseline — it always runs
        # exactly len(CI_FIXED_ORDER) up-front calls, so report that instead
        # of the --tool-budget CLI value, which doesn't apply here.
        belief_state.tool_call_budget = len(CI_FIXED_ORDER)
        final = run_single_shot_baseline(
            belief_state, tool_registry, CI_FIXED_ORDER, backend, model, llm_timeout,
        )
    elif mode == "fixed_order":
        belief_state.tool_call_budget = len(CI_FIXED_ORDER)
        final = run_fixed_order_baseline(
            belief_state, tool_registry, CI_FIXED_ORDER, backend, model, llm_timeout,
        )
    else:
        raise ValueError(f"unknown mode {mode!r}")

    # "confidence" is the number RQ3's calibration comparison scores against
    # ground truth, so it must track whichever hypothesis this run is actually
    # committed to. In "full" mode that's max(current_belief). In
    # "no_backtrack" mode the locked label is not guaranteed to be the
    # numerical max once a lock violation occurs (see _commit_belief) — the
    # locked label's own confidence is the correct number to report there.
    if final.mode == "no_backtrack" and final.locked_hypothesis is not None:
        locked_entry = next(
            (h for h in final.current_belief if h.get("label") == final.locked_hypothesis),
            None,
        )
        confidence = locked_entry.get("confidence") if locked_entry else None
    else:
        confidence = max((h["confidence"] for h in final.current_belief), default=None)

    return {
        "failure_type": failure_type,
        "run_id": run_id,
        "github_conclusion": completed["conclusion"],
        "mode": final.mode,
        "conclusion": final.conclusion,
        "conclusion_source": final.conclusion_source,
        "model_stated_conclusion": final.model_stated_conclusion,
        "confidence": confidence,
        "mismatch_type": final.mismatch_type,
        "locked_hypothesis": final.locked_hypothesis,
        "lock_confidence": final.lock_confidence,
        "lock_margin": final.lock_margin,
        "lock_violations_count": sum(
            1 for e in final.lock_enforcement_log if e["event"] != "locked"
        ),
        "lock_enforcement_log": final.lock_enforcement_log,
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
    parser.add_argument(
        "--mode", default="full",
        choices=["full", "no_backtrack", "single_shot", "fixed_order"],
        help=(
            "full (default): current behaviour. no_backtrack: RQ1 ablation 3 — "
            "locks the leading hypothesis label after the first belief update; "
            "see _commit_belief in agent/loop.py. single_shot: RQ1 baseline 1 — "
            "all evidence handed over up front, one LLM call, no iteration. "
            "fixed_order: RQ1 baseline 2 — tools called in a hardcoded sequence, "
            "no dynamic hypothesis-driven tool choice; see agent/baselines.py."
        ),
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
            token, args.tool_budget, args.llm_timeout, mode=args.mode,
        )
        # Ground truth branches on whether the run actually fired, not on
        # scenario name: a scenario that didn't trigger the injected failure
        # is a "healthy" run regardless of what it was configured to inject.
        if result.get("github_conclusion") == "failure":
            result["ground_truth"] = ground_truth
        else:
            result["ground_truth"] = "healthy"
        result["scenario_did_not_trigger"] = (
            failure_type in STOCHASTIC_SCENARIO_TYPES
            and result.get("github_conclusion") == "success"
        )
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
        correct = "YES" if concl == gt else "NO"
        trigger_note = " [did not trigger]" if r.get("scenario_did_not_trigger") else ""
        print(f"{r['failure_type']:<32} {gt:<16} {str(concl):<16} {str(r['conclusion_source']):<24} {correct:<10}{trigger_note}")

    # --- Save full results ---
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"results/sweep_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
