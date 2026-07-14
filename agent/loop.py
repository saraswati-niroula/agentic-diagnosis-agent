"""
agent/loop.py

Core agent loop for CI failure diagnosis.

Supports multiple LLM backends via call_llm():
  ollama  — local, no API key required (default)
  groq    — requires GROQ_API_KEY
  gemini  — requires GEMINI_API_KEY (or GOOGLE_API_KEY)

Pass --backend to select. The loop drives think_step() until the model
issues a CONCLUDE action or the tool call budget is exhausted.

BeliefState.belief_trajectory records a snapshot of the hypothesis
distribution after every turn so backtracking frequency can be computed
offline.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tools.trigger_and_fetch import (
    download_zip,
    extract_evidence_from_zip,
    fetch_logs_url,
)

GITHUB_API = "https://api.github.com"

ALLOWED_HYPOTHESES = [
    "flaky_test",
    "real_regression",
    "env_dependency",
    "ci_infra_issue",
    "schema_change",
    "healthy",
    "ambiguous",
    "other",
]

DEFAULT_MODELS = {
    "cerebras": "gpt-oss-120b",
    "ollama": "llama3.2",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
}

# Exact prompt — curly-brace placeholders use .format(); literal braces doubled.
PROMPT_TEMPLATE = """\
You are investigating the root cause of a software failure. You have a fixed
set of possible hypotheses, a log of evidence gathered so far, and a limited
number of tool calls remaining.

ALLOWED HYPOTHESES (choose only from this list):
- flaky_test
- real_regression
- env_dependency
- ci_infra_issue
- schema_change
- healthy
- ambiguous
- other

IMPORTANT: if the evidence shows had_failures: false (no test failures detected),
you MUST conclude "healthy" — do NOT conclude "other". "other" is reserved for
failure runs where the root cause does not fit any named hypothesis.

CURRENT BELIEF STATE:
{belief_state_json}

EVIDENCE LOG SO FAR:
{evidence_log_json}

TOOL CALLS USED: {tool_calls_used} / {tool_call_budget}

AVAILABLE TOOLS:
{tool_schemas_json}

Your job this turn is exactly ONE of the following three actions:

1. CALL A TOOL — if there are still competing hypotheses with meaningfully
   different confidence and a tool call could discriminate between them.
   You MUST explain which two (or more) hypotheses this call is meant to
   discriminate between, and why the expected result would move your
   confidence in one direction or another. Do not call a tool "just to
   gather more information" — every call must target a specific ambiguity
   in your current belief state.

2. UPDATE BELIEF STATE — if you have new evidence to incorporate but are not
   ready to call another tool or conclude yet. Re-emit your full hypothesis
   list with updated confidence scores (0.0 to 1.0, must sum to 1.0) and a
   short rationale per hypothesis. You are allowed and expected to change
   your leading hypothesis if evidence contradicts it — do not anchor on
   your first guess.

3. CONCLUDE — if either (a) one hypothesis clearly dominates with strong
   supporting evidence, or (b) your tool call budget is nearly exhausted, or
   (c) you have genuinely insufficient evidence to discriminate between
   competing hypotheses even in principle. In case (c), you MUST conclude
   "ambiguous" rather than arbitrarily picking a hypothesis. Confidently
   guessing when evidence does not support it is a worse outcome than
   correctly reporting uncertainty.

Before finalizing a CONCLUDE action, briefly argue against your own leading
hypothesis using the evidence log — if you can construct a plausible
counter-argument you cannot rule out, you have not gathered enough evidence
to conclude yet.

Respond ONLY in this exact JSON format, nothing else:
{{
  "action": "call_tool" | "update_belief" | "conclude",
  "tool_name": "..." (only if action is call_tool),
  "tool_params": {{...}} (only if action is call_tool),
  "discriminates_between": ["hyp_a", "hyp_b"] (only if action is call_tool),
  "hypotheses": [{{"label": "...", "confidence": 0.0, "rationale": "..."}}, ...],
  "conclusion": "..." (only if action is conclude, one of the allowed hypotheses or "ambiguous"),
  "self_check_argument": "..." (only if action is conclude — your counter-argument against your own leading hypothesis)
}}\
"""

QUERY_TEST_FAILURE_LOGS_SCHEMA = {
    "name": "query_test_failure_logs",
    "description": (
        "Fetch and parse the pytest output from a specific GitHub Actions run. "
        "Returns structured evidence: had_failures, failures_detail (test_id + message), "
        "short_summary, summary_line, and no_test_output if pytest never ran."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": "The GitHub Actions run ID to fetch logs for.",
            }
        },
        "required": ["run_id"],
    },
}

QUERY_CI_RUN_HISTORY_SCHEMA = {
    "name": "query_ci_run_history",
    "description": (
        "Fetch recent GitHub Actions workflow run conclusions and timestamps for this "
        "repository. Returns run IDs, conclusions (success/failure/cancelled/skipped), "
        "statuses, and ISO timestamps for the most recent runs. Use this to detect "
        "whether failures are systematic across many runs (suggesting real_regression) "
        "or intermittent (suggesting flaky_test). Pass run_ids from this result to "
        "query_flakiness_history for per-test breakdown."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of recent runs to return (default 10, max 30).",
            }
        },
        "required": [],
    },
}

QUERY_FLAKINESS_HISTORY_SCHEMA = {
    "name": "query_flakiness_history",
    "description": (
        "Check whether a specific test passed or failed across a set of prior workflow "
        "runs. For each run_id provided, downloads the logs and checks whether the "
        "given test_id appears in the failure list. Returns pass/fail per run, plus "
        "aggregate failure_count and pass_count. Use this — after calling "
        "query_ci_run_history to obtain run IDs — to distinguish a consistently "
        "failing test (real_regression) from one that fails intermittently (flaky_test)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "test_id": {
                "type": "string",
                "description": (
                    "Test identifier to look up — e.g. 'test_flaky' or "
                    "'tests/test_calculator.py::test_flaky'. Matched as a substring "
                    "against the test IDs in each run's failure list."
                ),
            },
            "run_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "GitHub Actions run IDs to check. Obtain from query_ci_run_history."
                ),
            },
        },
        "required": ["test_id", "run_ids"],
    },
}


@dataclass
class BeliefState:
    current_belief: list[dict]       # [{"label", "confidence", "rationale"}]
    evidence_log: list[dict]         # accumulated tool results + metadata
    tool_calls_used: int
    tool_call_budget: int
    status: str                      # "running" | "concluded" | "budget_exhausted"
    belief_trajectory: list[list[dict]] = field(default_factory=list)
    conclusion: str | None = None
    conclusion_source: str | None = None      # "model_concluded" | "forced_budget_exhaustion"
    pre_override_belief: list[dict] | None = None  # diagnostic only — never use in metrics
    self_check_argument: str | None = None


def _uniform_prior() -> list[dict]:
    weight = round(1.0 / len(ALLOWED_HYPOTHESES), 4)
    return [
        {"label": h, "confidence": weight, "rationale": "no evidence yet"}
        for h in ALLOWED_HYPOTHESES
    ]


def _snapshot(belief_state: BeliefState) -> list[dict]:
    return copy.deepcopy(belief_state.current_belief)


def _build_redundancy_notice(evidence_log: list[dict]) -> str:
    """Return a prompt annotation listing tool calls already in the evidence log."""
    prior = [
        (i, e["tool"], e.get("params", {}))
        for i, e in enumerate(evidence_log)
        if "tool" in e
    ]
    if not prior:
        return ""
    lines = [
        "REDUNDANCY NOTICE — the following tool calls are already in your evidence log"
        " and would return identical results if repeated:",
    ]
    for idx, tool_name, params in prior:
        params_str = ", ".join(f"{k}={v}" for k, v in params.items())
        lines.append(f"  - {tool_name}({params_str}) — evidence log entry {idx}")
    lines.append(
        "Do not repeat any of these calls. Reference their results from the evidence"
        " log above instead of calling the tool again."
    )
    return "\n".join(lines)


def call_llm(prompt: str, backend: str, model: str, timeout: int = 300) -> str:
    """Send prompt to the chosen backend and return the raw text response."""
    if backend == "cerebras":
        from cerebras.cloud.sdk import Cerebras  # pip install cerebras-cloud-sdk
        api_key = os.environ.get("CEREBRAS_API_KEY")
        if not api_key:
            raise RuntimeError("CEREBRAS_API_KEY not set")
        client = Cerebras(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        return completion.choices[0].message.content

    if backend == "ollama":
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["response"]

    if backend == "groq":
        from groq import Groq  # pip install groq
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        return completion.choices[0].message.content

    if backend == "gemini":
        import google.generativeai as genai  # pip install google-generativeai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        genai.configure(api_key=api_key)
        response = genai.GenerativeModel(model).generate_content(prompt)
        return response.text

    raise ValueError(f"Unknown backend {backend!r}. Choose: cerebras, ollama, groq, gemini.")


def think_step(
    belief_state: BeliefState,
    tool_schemas: list[dict],
    tool_registry: dict[str, Callable],
    backend: str = "cerebras",
    model: str = "gpt-oss-120b",
    timeout: int = 300,
) -> dict:
    """Run one LLM turn and dispatch the resulting action. Mutates belief_state."""
    prompt = PROMPT_TEMPLATE.format(
        belief_state_json=json.dumps(belief_state.current_belief, indent=2),
        evidence_log_json=json.dumps(belief_state.evidence_log, indent=2),
        tool_calls_used=belief_state.tool_calls_used,
        tool_call_budget=belief_state.tool_call_budget,
        tool_schemas_json=json.dumps(tool_schemas, indent=2),
    )
    redundancy_notice = _build_redundancy_notice(belief_state.evidence_log)
    if redundancy_notice:
        prompt = prompt + "\n\n" + redundancy_notice

    raw = call_llm(prompt, backend, model, timeout=timeout).strip()
    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    step, _ = json.JSONDecoder().raw_decode(raw)
    action = step["action"]

    if action == "call_tool":
        tool_name = step["tool_name"]
        tool_params = step.get("tool_params", {})
        belief_state.tool_calls_used += 1

        params_key = json.dumps(tool_params, sort_keys=True)
        already_called = any(
            e.get("tool") == tool_name
            and json.dumps(e.get("params", {}), sort_keys=True) == params_key
            for e in belief_state.evidence_log
            if "tool" in e
        )
        if already_called:
            print(f"  [block] redundant call intercepted: {tool_name}({params_key})")
            result = {
                "source": "redundant_call_blocked",
                "message": (
                    f"{tool_name} with these exact params was already called. "
                    "See the prior evidence log entry for the result — "
                    "calling again returns identical data."
                ),
            }
        else:
            tool_fn = tool_registry.get(tool_name)
            if tool_fn is None:
                result = {"error": f"unknown tool: {tool_name}"}
            else:
                try:
                    result = tool_fn(**tool_params)
                except Exception as exc:
                    result = {"error": str(exc)}

        belief_state.evidence_log.append({
            "tool": tool_name,
            "params": tool_params,
            "result": result,
            "discriminates_between": step.get("discriminates_between", []),
        })
        # Snapshot at tool-call turn — hypotheses unchanged yet, but marks
        # the turn index in the trajectory for backtracking analysis.
        belief_state.belief_trajectory.append(_snapshot(belief_state))

    elif action == "update_belief":
        belief_state.current_belief = step.get("hypotheses", belief_state.current_belief)
        belief_state.belief_trajectory.append(_snapshot(belief_state))

    elif action == "conclude":
        belief_state.current_belief = step.get("hypotheses", belief_state.current_belief)
        belief_state.belief_trajectory.append(_snapshot(belief_state))
        belief_state.status = "concluded"
        belief_state.conclusion = step.get("conclusion", "ambiguous")
        belief_state.conclusion_source = "model_concluded"
        belief_state.self_check_argument = step.get("self_check_argument", "")

    return step


FORCE_UPDATE_PROMPT = """\
You just called a tool and received this result:

{last_evidence_json}

Full evidence log so far:

{evidence_log_json}

You MUST now update your belief state. Re-emit the complete hypothesis list
with updated confidence scores (must sum to 1.0) and a short rationale per
hypothesis that reflects what you just learned from the tool result.

ALLOWED HYPOTHESES: {allowed}

Respond ONLY in this exact JSON format, nothing else:
{{
  "action": "update_belief",
  "hypotheses": [{{"label": "...", "confidence": 0.0, "rationale": "..."}}, ...]
}}\
"""


def _force_belief_update(
    belief_state: BeliefState,
    backend: str,
    model: str,
    timeout: int = 300,
) -> None:
    """Force a belief-update turn immediately after a tool call.

    Uses a focused prompt that only allows update_belief — no tool calls, no
    conclude. Does not count against the tool budget. Mutates belief_state.
    """
    prompt = FORCE_UPDATE_PROMPT.format(
        last_evidence_json=json.dumps(belief_state.evidence_log[-1], indent=2),
        evidence_log_json=json.dumps(belief_state.evidence_log, indent=2),
        allowed=", ".join(ALLOWED_HYPOTHESES),
    )
    raw = call_llm(prompt, backend, model, timeout=timeout).strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    try:
        step = json.loads(raw)
    except json.JSONDecodeError:
        return  # leave belief unchanged rather than crash
    if step.get("action") == "update_belief" and "hypotheses" in step:
        belief_state.current_belief = step["hypotheses"]
        belief_state.belief_trajectory.append(_snapshot(belief_state))


def run_loop(
    belief_state: BeliefState,
    tool_schemas: list[dict],
    tool_registry: dict[str, Callable],
    backend: str = "cerebras",
    model: str = "gpt-oss-120b",
    max_turns: int = 20,
    timeout: int = 300,
) -> BeliefState:
    """Drive think_step() until concluded or budget/turn limit hit."""
    for turn in range(max_turns):
        if belief_state.status != "running":
            break
        if belief_state.tool_calls_used >= belief_state.tool_call_budget:
            belief_state.pre_override_belief = copy.deepcopy(belief_state.current_belief)
            belief_state.status = "budget_exhausted"
            belief_state.conclusion = "ambiguous"
            belief_state.conclusion_source = "forced_budget_exhaustion"
            print("[loop] budget exhausted — concluding ambiguous")
            break

        step = think_step(belief_state, tool_schemas, tool_registry, backend, model, timeout)

        label = f"action={step['action']}"
        if step["action"] == "call_tool":
            label += f" tool={step.get('tool_name')} discriminates={step.get('discriminates_between')}"
            _force_belief_update(belief_state, backend, model, timeout)
        elif step["action"] == "conclude":
            label += f" conclusion={step.get('conclusion')}"
        print(f"[turn {turn}] {label}")

    if belief_state.status == "running":
        belief_state.pre_override_belief = copy.deepcopy(belief_state.current_belief)
        belief_state.status = "budget_exhausted"
        belief_state.conclusion = "ambiguous"
        belief_state.conclusion_source = "forced_budget_exhaustion"
        print("[loop] turn limit exhausted — concluding ambiguous")

    return belief_state


def _make_query_test_failure_logs(owner: str, repo: str, token: str) -> Callable:
    """Return a closure over GitHub credentials for the query_test_failure_logs tool."""

    def _get_run(run_id: int) -> dict:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def query_test_failure_logs(run_id: int) -> dict:
        run = _get_run(run_id)
        logs_url = fetch_logs_url(owner, repo, token, run_id)
        if not logs_url:
            return {
                "error": "logs URL unavailable — run may still be in progress or logs expired",
                "run_status": run.get("status"),
                "run_conclusion": run.get("conclusion"),
            }
        zip_bytes = download_zip(logs_url)
        evidence = extract_evidence_from_zip(zip_bytes, run_conclusion=run.get("conclusion"))
        evidence["run_id"] = run_id
        evidence["run_conclusion"] = run.get("conclusion")
        return evidence

    return query_test_failure_logs


def _make_query_ci_run_history(owner: str, repo: str, token: str) -> Callable:
    """Return a closure over GitHub credentials for the query_ci_run_history tool."""

    def query_ci_run_history(limit: int = 10) -> dict:
        limit = min(int(limit), 30)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(url, headers=headers, params={"per_page": limit})
        resp.raise_for_status()
        data = resp.json()
        runs = [
            {
                "run_id": r["id"],
                "conclusion": r.get("conclusion"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "event": r.get("event"),
            }
            for r in data.get("workflow_runs", [])
        ]
        return {"runs": runs, "total_returned": len(runs)}

    return query_ci_run_history


def _make_query_flakiness_history(owner: str, repo: str, token: str) -> Callable:
    """Return a closure over GitHub credentials for the query_flakiness_history tool."""

    def _get_run_conclusion(run_id: int) -> str | None:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("conclusion")

    def query_flakiness_history(test_id: str, run_ids: list) -> dict:
        results = []
        for run_id in run_ids:
            try:
                run_conclusion = _get_run_conclusion(run_id)
                logs_url = fetch_logs_url(owner, repo, token, run_id)
                if not logs_url:
                    results.append({
                        "run_id": run_id,
                        "outcome": "logs_unavailable",
                        "run_conclusion": run_conclusion,
                    })
                    continue
                zip_bytes = download_zip(logs_url)
                evidence = extract_evidence_from_zip(
                    zip_bytes, run_conclusion=run_conclusion
                )
                if not evidence.get("had_failures"):
                    outcome = "passed"
                elif any(
                    test_id in f.get("test_id", "")
                    for f in evidence.get("failures_detail", [])
                ):
                    outcome = "failed"
                else:
                    outcome = "passed"  # run failed, but not this specific test
                results.append({
                    "run_id": run_id,
                    "outcome": outcome,
                    "run_conclusion": run_conclusion,
                })
            except Exception as exc:
                results.append({
                    "run_id": run_id,
                    "outcome": "error",
                    "error": str(exc),
                })
        return {
            "test_id": test_id,
            "results": results,
            "failure_count": sum(1 for r in results if r["outcome"] == "failed"),
            "pass_count": sum(1 for r in results if r["outcome"] == "passed"),
            "total_runs_checked": len(results),
        }

    return query_flakiness_history


if __name__ == "__main__":
    import argparse
    import time

    from tools.trigger_and_fetch import (
        get_latest_run,
        poll_until_complete,
        trigger_run,
    )

    parser = argparse.ArgumentParser(description="Run the CI diagnosis agent loop.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--failure-type", default="real_regression",
        choices=[
            "none", "flaky_test", "real_regression", "env_dependency",
            "schema_change", "ci_infra_issue",
            "ambiguous_flaky_or_regression", "regression_with_redherring",
        ],
    )
    parser.add_argument(
        "--backend", default="cerebras",
        choices=["cerebras", "ollama", "groq", "gemini"],
        help="LLM backend (default: cerebras — requires CEREBRAS_API_KEY)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name for the chosen backend (defaults: cerebras=gpt-oss-120b, ollama=llama3.2, groq=llama-3.3-70b-versatile, gemini=gemini-2.0-flash)",
    )
    parser.add_argument("--tool-budget", type=int, default=5)
    parser.add_argument(
        "--llm-timeout", type=int, default=300,
        help="Seconds to wait for an LLM response (default: 300; increase for slow local models)",
    )
    args = parser.parse_args()

    model = args.model or DEFAULT_MODELS[args.backend]

    # Validate API key for backends that need one
    if args.backend == "cerebras" and not os.environ.get("CEREBRAS_API_KEY"):
        print("ERROR: set CEREBRAS_API_KEY for cerebras backend", file=sys.stderr)
        sys.exit(1)
    if args.backend == "cerebras":
        from cerebras.cloud.sdk import Cerebras as _CerebrasClient
        _client = _CerebrasClient(api_key=os.environ["CEREBRAS_API_KEY"])
        _available = [m.id for m in _client.models.list().data]
        if model not in _available:
            print(f"ERROR: model '{model}' is not available on this Cerebras account.", file=sys.stderr)
            print(f"Available models: {', '.join(_available)}", file=sys.stderr)
            sys.exit(1)
    if args.backend == "groq" and not os.environ.get("GROQ_API_KEY"):
        print("ERROR: set GROQ_API_KEY for groq backend", file=sys.stderr)
        sys.exit(1)
    if args.backend == "gemini" and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ):
        print("ERROR: set GEMINI_API_KEY (or GOOGLE_API_KEY) for gemini backend", file=sys.stderr)
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)

    print(f"Backend: {args.backend} / {model}")
    print(f"Triggering {args.failure_type} run on {args.owner}/{args.repo}...")
    trigger_run(args.owner, args.repo, args.failure_type, token)
    print("Waiting for run to register...")
    time.sleep(6)

    run = get_latest_run(args.owner, args.repo, token)
    if not run:
        print("No run found.", file=sys.stderr)
        sys.exit(1)
    run_id = run["id"]
    print(f"Polling run {run_id}...")
    completed = poll_until_complete(args.owner, args.repo, token, run_id)
    print(f"Run complete — GitHub conclusion: {completed['conclusion']}")

    belief_state = BeliefState(
        current_belief=_uniform_prior(),
        evidence_log=[{
            "init": f"investigating run_id={run_id}",
            "github_conclusion": completed["conclusion"],
        }],
        tool_calls_used=0,
        tool_call_budget=args.tool_budget,
        status="running",
    )

    tool_registry = {
        "query_test_failure_logs": _make_query_test_failure_logs(
            args.owner, args.repo, token
        ),
        "query_ci_run_history": _make_query_ci_run_history(
            args.owner, args.repo, token
        ),
        "query_flakiness_history": _make_query_flakiness_history(
            args.owner, args.repo, token
        ),
    }

    final = run_loop(
        belief_state,
        tool_schemas=[
            QUERY_TEST_FAILURE_LOGS_SCHEMA,
            QUERY_CI_RUN_HISTORY_SCHEMA,
            QUERY_FLAKINESS_HISTORY_SCHEMA,
        ],
        tool_registry=tool_registry,
        backend=args.backend,
        model=model,
        timeout=args.llm_timeout,
    )

    print("\n=== FINAL BELIEF STATE ===")
    print(json.dumps(final.current_belief, indent=2))
    print(f"\nConclusion:        {final.conclusion}")
    print(f"Conclusion source: {final.conclusion_source}")
    print(f"Self-check:        {final.self_check_argument}")
    if final.pre_override_belief is not None:
        print("\n=== PRE-OVERRIDE BELIEF (diagnostic only — not for metrics) ===")
        print(json.dumps(final.pre_override_belief, indent=2))
    print(f"Tool calls used:  {final.tool_calls_used} / {final.tool_call_budget}")
    print(f"Trajectory depth: {len(final.belief_trajectory)} snapshots")
    print("\n=== BELIEF TRAJECTORY (leading hypothesis per turn) ===")
    for i, snapshot in enumerate(final.belief_trajectory):
        top = max(snapshot, key=lambda h: h["confidence"])
        print(f"  [{i}] {top['label']} ({top['confidence']:.2f})")
