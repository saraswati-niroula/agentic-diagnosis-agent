# Failure Taxonomy

Six injection modes are supported. This document records:
- what evidence each type produces in the structured JSON returned by `trigger_and_fetch.py`
- what makes each type ambiguous relative to others
- the overlap cases where the agent loop is genuinely necessary (not collapsible into a lookup table)

Evidence fingerprints are verified against real GitHub Actions runs (2026-07-02).
When pytest ran, `log_file` is consistently `"test/6_Run test suite.txt"`.

---

## Type 0 — Healthy baseline

**Injection:** `FAILURE_INJECTION=none`

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `false` |
| `failures_detail` | `[]` |
| `short_summary` | `""` |
| `summary_line` | `"8 passed in 0.02s"` |
| `log_file` | `"test/6_Run test suite.txt"` |
| `no_test_output` | absent |

**Role in evaluation:** the reference point for all comparisons. The agent should assign ~0 confidence to every failure hypothesis and output `conclusion: healthy`.

---

## Type 1 — Flaky / non-deterministic test failure

**Injection:** `FAILURE_INJECTION=flaky_test` → `assert random.random() < 0.5`

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `true` ~50% of runs, `false` ~50% |
| `failures_detail[].test_id` | `tests/test_calculator.py::test_injection_target` |
| `failures_detail[].message` | `AssertionError: Flaky failure injected (non-deterministic)` |
| `summary_line` | `"1 failed, 7 passed in …"` or `"8 passed in …"` |
| `log_file` | `"test/6_Run test suite.txt"` |
| `no_test_output` | absent (pytest always runs) |

**Distinguishing feature:** failure rate varies across reruns with no code or environment change between them.

---

## Type 2 — Real code regression

**Injection:** `FAILURE_INJECTION=real_regression` → `assert (multiply(4,3) - 1) == 12`

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `true` on every run |
| `failures_detail[].test_id` | `tests/test_calculator.py::test_injection_target` |
| `failures_detail[].message` | `AssertionError: Real regression injected (deterministic)` |
| `summary_line` | `"1 failed, 7 passed in …"` every time |
| `log_file` | `"test/6_Run test suite.txt"` |
| `no_test_output` | absent |

**Distinguishing feature:** fails deterministically across all reruns; a git diff shows a code change near the first failing run.

---

## Type 3 — Dependency / environment change

**Injection:** `FAILURE_INJECTION=env_dependency`, `SIMULATED_DEPENDENCY` env var absent

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | true when env var is unset, false when set |
| `failures_detail[].message` | `AssertionError: Environment/dependency failure injected` |
| `summary_line` | `1 failed, 7 passed in …` |
| `no_test_output` | absent (pytest runs; the test itself detects the missing dep) |

**Distinguishing feature:** failure is correlated with an environment/runner configuration change, not a code change; reruns on the same runner config fail consistently, but the same commit on a correctly configured runner passes.

---

## Type 4 — CI infrastructure issue

**Injection:** workflow step "Simulate CI infrastructure failure" exits 1 before pytest runs

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | true |
| `failures_detail` | `[]` |
| `short_summary` | `""` |
| `summary_line` | `""` |
| `no_test_output` | `true` |
| `available_step_logs` | list ends at the failing pre-test step; "Run test suite" is absent |

**Distinguishing feature:** pytest never ran — `no_test_output: true` combined with `had_failures: true`. The `available_step_logs` list shows exactly which pre-test step was last, pinpointing where the job stopped.

---

## Type 5 — Test outdated after intentional API change

**Injection:** `FAILURE_INJECTION=schema_change` → `calculator.add(2, 3, ndigits=0)`

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | true on every run |
| `failures_detail[].message` | `TypeError: add() got an unexpected keyword argument 'ndigits'` |
| `summary_line` | `1 failed, 7 passed in …` |
| `no_test_output` | absent |

**Distinguishing feature:** error is a `TypeError` (wrong call signature), not an `AssertionError` (wrong result). A git diff shows the function signature changed intentionally; the test was not updated to match.

---

## Overlap map — where the loop is genuinely necessary

The cells below describe evidence states that are indistinguishable without additional tool calls.

| | Flaky | Real regression | Dependency | CI infra | Schema change |
|---|---|---|---|---|---|
| **Flaky** | — | Single run: identical `AssertionError` on same test. Need rerun history to discriminate. | Single failed run: both show `AssertionError` on `test_injection_target`. Need env-change history + rerun history. | `no_test_output` separates infra immediately. | `TypeError` vs `AssertionError` separates immediately. |
| **Real regression** | — | — | Both fail every run, same test. Need git diff (code change vs env change) + env-change query. | `no_test_output` separates immediately. | `TypeError` vs `AssertionError` separates immediately. |
| **Dependency** | — | — | — | Both are environment-level failures with no code change. Discriminator: `no_test_output` (infra) vs pytest running (dep). | `TypeError` vs `AssertionError` separates immediately. |
| **CI infra** | — | — | — | — | `no_test_output` separates immediately. |
| **Schema change** | — | — | — | — | — |

**The hard overlaps (require multiple tool calls and backtracking):**

1. **Flaky vs. Real regression** — the core ambiguity. Both produce `AssertionError` on `test_injection_target`. A single run cannot discriminate them. The agent must call `query_flakiness_history` and `query_ci_run_history` to compare failure rates across reruns. A real regression fails 100% of the time; flakiness does not. Even then, a genuinely intermittent regression (e.g. a race condition) can mimic flakiness — this is the deliberate "cannot be resolved" ambiguous case.

2. **Flaky vs. Dependency (single-run view)** — both can appear intermittent. Dependency failures correlate with runner/config changes; flakiness does not. Agent must call `query_ci_infra_status` and compare failure timestamps against known environment changes.

3. **Real regression vs. Dependency** — both fail deterministically. Discriminator is the source of change: `query_git_diff` shows a code change for regression; `query_dependency_lockfile_diff` or `query_ci_infra_status` shows an environment change for dependency. In the ambiguous case, both happened at the same time — the agent must reason about which change is causally responsible.

**Caveat on "separates immediately" cells:** the diagonal claims above assume the tool layer is working correctly. A distinction that is logically immediate (e.g., `no_test_output: true` rules out a test failure) is only actually immediate if the extractor surfaces that field. This project encountered this failure mode directly: an early version of the log parser silently returned empty evidence rather than populating `no_test_output`, making a CI infra failure look identical to a flaky test with no output. The "immediate" separations in this table are therefore conditional on a correctly functioning tool layer — tool reliability is itself a load-bearing assumption in the diagnosis pipeline, and a legitimate point of fragility to acknowledge in any evaluation write-up.

---

## Deliberately ambiguous ground-truth cases

These are scenarios where the correct label is **"ambiguous / insufficient evidence"** rather than a single clean cause. They are necessary for evaluating RQ3 (calibrated abstention).

| Scenario | Why it is ambiguous | Infrastructure needed |
|---|---|---|
| Intermittent failure with no rerun history | Cannot distinguish flaky from a real intermittent regression without reruns | None — trigger `flaky_test` exactly once and suppress rerun history from the agent's tool responses. Already producible. |
| Failure on a run where both a code change and a dependency update landed simultaneously | Cannot determine whether the code change or the env change caused the failure without a bisect | New injection mode required: current `FAILURE_INJECTION` values are mutually exclusive single modes. Need a combined mode (e.g., `FAILURE_INJECTION=regression_plus_env`) that applies both a code mutation and unsets `SIMULATED_DEPENDENCY` in the same workflow run. |
| A test that sometimes passes, sometimes fails, with a failure rate of ~80% | High enough to suggest regression, low enough that flakiness cannot be ruled out | Parameterized flakiness rate required: `flaky_test` is hardcoded to `random.random() < 0.5`. Add a `FLAKY_RATE` env var read by the injected assertion so the rate can be set to 0.8 (or any target) without editing source. |

In these cases the correct agent output is a ranked hypothesis list where no single hypothesis exceeds a confidence threshold (e.g., 0.6), and the `conclusion` field is `"ambiguous"` rather than a named failure type.

---

## Evidence fields reference

All fields are present in the JSON dict returned by `extract_evidence_from_zip` in `scripts/trigger_and_fetch.py`.

| Field | Type | Present when |
|---|---|---|
| `had_failures` | bool | always |
| `failures_detail` | list of `{test_id, message}` | always |
| `short_summary` | str | always |
| `summary_line` | str | always |
| `log_file` | str | pytest ran |
| `no_test_output` | bool (True) | pytest did not run |
| `available_step_logs` | list of str | `no_test_output` is True |
