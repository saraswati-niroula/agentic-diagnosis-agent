"""
agent/baselines.py

RQ1 baselines — single-shot (no iterative tool use) and fixed-order
(hardcoded tool sequence, no dynamic hypothesis-driven selection).

Both share BeliefState / _apply_conclusion / _force_belief_update with the
main loop (agent/loop.py), unmodified, so all four arms (full, no_backtrack,
single_shot, fixed_order) produce directly comparable result records for the
RQ1 comparison table.
"""

from __future__ import annotations

import json
from typing import Callable

from agent.loop import (
    BeliefState,
    _apply_conclusion,
    _force_belief_update,
    _hypotheses_bullets,
    _snapshot,
    call_llm,
)

# Single LLM call over all up-front evidence. self_check_argument is
# requested in this same pass — without it, mismatch_type's
# deliberate_abstention branch (which requires a non-empty self-check) could
# never fire for this baseline, making its calibrated-abstention rate
# structurally zero rather than a real result. The differentiator vs. the
# full loop stays "one pass, no iterative gathering, no separate adversarial
# turn" — not "no self-justification at all."
SINGLE_SHOT_PROMPT = """\
You are investigating the root cause of a software failure. You have been
given ALL available evidence up front. There are no tool calls available in
this task — you must reach a conclusion from this evidence alone, in a
single response.

ALLOWED HYPOTHESES (choose only from this list):
{allowed_hypotheses_bullets}

{healthy_hint}

EVIDENCE LOG:
{evidence_log_json}

Provide a full hypothesis list with confidence scores (0.0 to 1.0, must sum
to 1.0) and a short rationale per hypothesis, your final conclusion, and a
self-check: briefly argue against your own leading hypothesis using the
evidence above. If you can construct a plausible counter-argument you cannot
rule out, your conclusion should reflect that (e.g. "ambiguous") rather than
overclaiming confidence.

Respond ONLY in this exact JSON format, nothing else:
{{
  "action": "conclude",
  "hypotheses": [{{"label": "...", "confidence": 0.0, "rationale": "..."}}, ...],
  "conclusion": "..." (one of the allowed hypotheses or "ambiguous"),
  "self_check_argument": "..."
}}\
"""

# Tools already gathered, in a fixed sequence, with a forced belief update
# after each (no model tool-choice) — then one plain conclude turn.
FIXED_ORDER_CONCLUDE_PROMPT = """\
You have gathered the following evidence in a fixed sequence and now must
conclude. There are no further tool calls available.

ALLOWED HYPOTHESES (choose only from this list):
{allowed_hypotheses_bullets}

{healthy_hint}

CURRENT BELIEF STATE:
{belief_state_json}

EVIDENCE LOG SO FAR:
{evidence_log_json}

Choose the single best-supported hypothesis given the evidence gathered. If
the evidence is genuinely insufficient to discriminate between competing
hypotheses even in principle, conclude "ambiguous".

Before finalizing, briefly argue against your own leading hypothesis using
the evidence log in self_check_argument.

Respond ONLY in this exact JSON format, nothing else:
{{
  "action": "conclude",
  "hypotheses": [{{"label": "...", "confidence": 0.0, "rationale": "..."}}, ...],
  "conclusion": "..." (one of the allowed hypotheses or "ambiguous"),
  "self_check_argument": "..."
}}\
"""


def _strip_code_fence(raw: str) -> str:
    if raw.startswith("```"):
        return "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("```")
        ).strip()
    return raw


def _run_fixed_calls(
    belief_state: BeliefState,
    tool_registry: dict[str, Callable],
    calls: list[tuple[str, Callable]],
    force_belief_update: bool,
    backend: str = "",
    model: str = "",
    timeout: int = 300,
) -> None:
    """Execute a fixed (tool_name, param_fn) sequence directly, bypassing
    model tool-choice. param_fn receives (evidence_log_so_far, run_id) and
    returns the params dict for that call.

    force_belief_update=True runs _force_belief_update after each call
    (baseline 2's behaviour); False just appends evidence with no belief
    update in between (baseline 1's "hand over everything up front").
    """
    for turn, (tool_name, param_fn) in enumerate(calls):
        params = param_fn(belief_state.evidence_log, belief_state.run_id)
        tool_fn = tool_registry.get(tool_name)
        try:
            result = tool_fn(**params) if tool_fn else {"error": f"unknown tool: {tool_name}"}
        except Exception as exc:
            result = {"error": str(exc)}
        belief_state.tool_calls_used += 1
        belief_state.evidence_log.append({
            "tool": tool_name,
            "params": params,
            "result": result,
        })
        belief_state.belief_trajectory.append(_snapshot(belief_state))
        if force_belief_update:
            _force_belief_update(belief_state, backend, model, timeout, turn=turn)


def run_single_shot_baseline(
    belief_state: BeliefState,
    tool_registry: dict[str, Callable],
    upfront_calls: list[tuple[str, Callable]],
    backend: str,
    model: str,
    timeout: int = 300,
) -> BeliefState:
    """Baseline 1: full failure context handed over up front, one LLM call,
    no iterative gathering, no separate adversarial turn.
    """
    _run_fixed_calls(belief_state, tool_registry, upfront_calls, force_belief_update=False)

    prompt = SINGLE_SHOT_PROMPT.format(
        allowed_hypotheses_bullets=_hypotheses_bullets(belief_state.allowed_hypotheses),
        healthy_hint=belief_state.healthy_hint,
        evidence_log_json=json.dumps(belief_state.evidence_log, indent=2),
    )
    raw = _strip_code_fence(call_llm(prompt, backend, model, timeout=timeout).strip())
    step, _ = json.JSONDecoder().raw_decode(raw)
    _apply_conclusion(belief_state, step, turn=0, raw=raw)
    return belief_state


def run_fixed_order_baseline(
    belief_state: BeliefState,
    tool_registry: dict[str, Callable],
    fixed_order: list[tuple[str, Callable]],
    backend: str,
    model: str,
    timeout: int = 300,
) -> BeliefState:
    """Baseline 2: tools called in a hardcoded sequence (no dynamic,
    hypothesis-driven tool choice), a forced belief update after each, then
    one plain conclude turn.
    """
    _run_fixed_calls(
        belief_state, tool_registry, fixed_order, force_belief_update=True,
        backend=backend, model=model, timeout=timeout,
    )

    prompt = FIXED_ORDER_CONCLUDE_PROMPT.format(
        allowed_hypotheses_bullets=_hypotheses_bullets(belief_state.allowed_hypotheses),
        healthy_hint=belief_state.healthy_hint,
        belief_state_json=json.dumps(belief_state.current_belief, indent=2),
        evidence_log_json=json.dumps(belief_state.evidence_log, indent=2),
    )
    raw = _strip_code_fence(call_llm(prompt, backend, model, timeout=timeout).strip())
    step, _ = json.JSONDecoder().raw_decode(raw)
    _apply_conclusion(belief_state, step, turn=len(fixed_order), raw=raw)
    return belief_state
