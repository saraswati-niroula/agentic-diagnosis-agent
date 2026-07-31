# Failure Taxonomy

Eight injection modes are supported (Types 0–7). This document records:
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

**Injection:** `FAILURE_INJECTION=flaky_test` → `assert random.random() >= FLAKY_RATE` (default `FLAKY_RATE=0.5`)

Failure rate is configurable via the `FLAKY_RATE` env var (passed as a workflow input) or `--flaky-rate` in `trigger_and_fetch.py`.

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `true` at rate `FLAKY_RATE` (default ~50%), `false` at rate `1 − FLAKY_RATE` |
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

## Type 6 — Ambiguous: high-rate flakiness mimicking regression

**Injection:** `FAILURE_INJECTION=ambiguous_flaky_or_regression` → `assert random.random() >= AMBIGUOUS_RATE` (`AMBIGUOUS_RATE` env var, default `0.8`; kept separate from `FLAKY_RATE` so eval sweeps cannot silently inherit the wrong rate)

**Ground truth:** `"ambiguous"` — the correct agent output is a ranked hypothesis list where no single hypothesis exceeds ~0.6 confidence, and `conclusion: "ambiguous"`.

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `true` ~80% of runs, `false` ~20% |
| `failures_detail[].test_id` | `tests/test_calculator.py::test_injection_target` |
| `failures_detail[].message` | `AssertionError: Flaky failure injected (non-deterministic)` |
| `summary_line` | `"1 failed, 7 passed in …"` or `"8 passed in …"` |
| `log_file` | `"test/6_Run test suite.txt"` |
| `no_test_output` | absent |

**Distinguishing feature (with rerun history):** failure rate is ~80%, not 100% — eventually visible across 10+ reruns. Without rerun history, a single run is indistinguishable from `real_regression`. The error message is identical to `flaky_test`. This is deliberately the hardest ambiguity case: no single tool call resolves it on one run.

**Statistical justification for permanent "ambiguous" ground truth:** Even when rerun history is available via `query_ci_run_history` and `query_flakiness_history`, the ground truth remains `"ambiguous"` rather than `flaky_test`. A practical limit of ~10 recent workflow runs — which is what `query_ci_run_history` can return from a low-volume repository — cannot statistically distinguish an ~80%-reliability flaky test from an intermittent real bug (e.g., a race condition or environment-dependent regression) with the same empirical failure rate. Both hypotheses produce indistinguishable frequency patterns over any sample size realistically obtainable in this project. A Bayesian update on 8/10 failures cannot rule out a regression with ~80% reproduction rate: the likelihoods are identical by construction. Increasing the sample to 30 or 50 runs would narrow the confidence interval but not resolve the ambiguity at this failure rate — a regression with 75–85% reproduction probability is a legitimate and plausible failure mode, not a theoretical edge case. The ambiguity here is therefore **deliberate and permanent by design**, not a limitation expected to resolve with more tool calls. An agent that concludes `flaky_test` or `real_regression` with high confidence after checking 10 runs of this scenario is overconfident, regardless of which label it picks.

**Evaluation note:** do not suppress rerun history — let the agent use all available tools. The correct label is `"ambiguous"` regardless of what the historical sample shows. An agent that correctly identifies the ambiguity and concludes `"ambiguous"` with appropriately hedged confidence is scoring correctly on RQ3 even if the historical sample happened to show a pattern consistent with one hypothesis. Confidence above ~0.65 on either `flaky_test` or `real_regression` for this scenario should be treated as a calibration failure in the eval harness.

---

## Type 7 — Real regression with causally-inert red herring

**Injection:** `FAILURE_INJECTION=regression_with_redherring`, `SIMULATED_DEPENDENCY` absent (same env condition as `env_dependency`)

**Ground truth:** `real_regression` — the dependency absence is visible but causally inert.

**Evidence fingerprint**

| Field | Value |
|---|---|
| `had_failures` | `true` on every run |
| `failures_detail[].test_id` | `tests/test_calculator.py::test_injection_target` |
| `failures_detail[].message` | `AssertionError: Real regression injected (deterministic)` |
| `summary_line` | `"1 failed, 7 passed in …"` every run |
| `log_file` | `"test/6_Run test suite.txt"` |
| `no_test_output` | absent |

**Red herring:** `SIMULATED_DEPENDENCY` is absent in the runner environment (identical to `env_dependency`'s env state). An agent that queries environment variables or CI config may incorrectly attribute the failure to a dependency/environment change. The test fails from the code mutation; the missing `SIMULATED_DEPENDENCY` never reaches the failing assertion path — the `env_dependency` branch in `test_injection_target` is never entered.

**Distinguishing feature:** failure message is `"Real regression injected (deterministic)"` (not `"Environment/dependency failure injected"`). A correctly implemented agent should identify the code mutation via `query_git_diff` and recognise the dependency absence as a coincident but causally unrelated change. An agent that over-weights environment signals without checking the assertion message will mis-classify this as `env_dependency`.

---

## Overlap map — where the loop is genuinely necessary

The cells below describe evidence states that are indistinguishable without additional tool calls.

| | Flaky | Real regression | Dependency | CI infra | Schema change | Ambiguous (T6) | Regression+RH (T7) |
|---|---|---|---|---|---|---|---|
| **Flaky** | — | Single run: identical `AssertionError` on same test. Need rerun history to discriminate. | Single failed run: both show `AssertionError` on `test_injection_target`. Need env-change history + rerun history. | `no_test_output` separates infra immediately. | `TypeError` vs `AssertionError` separates immediately. | Indistinguishable on a single run; both are non-deterministic `AssertionError`. Need large rerun sample. | Message differs (`Real regression` vs `Flaky`); T7 fails every run. |
| **Real regression** | — | — | Both fail every run, same test. Need git diff (code change vs env change) + env-change query. | `no_test_output` separates immediately. | `TypeError` vs `AssertionError` separates immediately. | Message differs; T6 passes ~20% of runs. | Both fail every run with same message. T7 also has absent `SIMULATED_DEPENDENCY` — need to check whether failure message is the dep assertion or the regression assertion. |
| **Dependency** | — | — | — | Both are environment-level failures with no code change. Discriminator: `no_test_output` (infra) vs pytest running (dep). | `TypeError` vs `AssertionError` separates immediately. | T6 message is `Flaky failure`, not `Environment/dependency`. | T7 message is `Real regression`, not `Environment/dependency`; but env state looks identical. Agent must check assertion message before concluding env cause. |
| **CI infra** | — | — | — | — | `no_test_output` separates immediately. | `no_test_output` separates immediately. | `no_test_output` separates immediately. |
| **Schema change** | — | — | — | — | — | `TypeError` vs `AssertionError` separates immediately. | `TypeError` vs `AssertionError` separates immediately. |
| **Ambiguous (T6)** | — | — | — | — | — | — | T6 passes ~20% of runs; T7 never passes. Message differs. |
| **Regression+RH (T7)** | — | — | — | — | — | — | — |

**The hard overlaps (require multiple tool calls and backtracking):**

1. **Flaky vs. Real regression** — the core ambiguity. Both produce `AssertionError` on `test_injection_target`. A single run cannot discriminate them. The agent must call `query_flakiness_history` and `query_ci_run_history` to compare failure rates across reruns. A real regression fails 100% of the time; flakiness does not. Even then, a genuinely intermittent regression (e.g. a race condition) can mimic flakiness — this is the deliberate "cannot be resolved" ambiguous case.

2. **Flaky vs. Dependency (single-run view)** — both can appear intermittent. Dependency failures correlate with runner/config changes; flakiness does not. Agent must call `query_ci_infra_status` and compare failure timestamps against known environment changes.

3. **Real regression vs. Dependency** — both fail deterministically. Discriminator is the source of change: `query_git_diff` shows a code change for regression; `query_dependency_lockfile_diff` or `query_ci_infra_status` shows an environment change for dependency. In the ambiguous case, both happened at the same time — the agent must reason about which change is causally responsible.

4. **Ambiguous (T6) vs. Flaky / Real regression (single run)** — on one run, T6 is indistinguishable from both. The failure message is identical to `flaky_test`; the failure rate cannot be estimated from one data point. This overlap is intentionally unresolvable without rerun history — the correct response is `conclusion: "ambiguous"`, not a forced classification.

5. **Regression+RH (T7) vs. Dependency** — both show `SIMULATED_DEPENDENCY` absent and a deterministic failure. The discriminator is the `failures_detail[].message` field: T7 produces `"Real regression injected (deterministic)"`, not `"Environment/dependency failure injected"`. An agent that routes on env state before reading the assertion message will mis-classify. Resolution requires the agent to read the failure message before issuing a conclusion.

**Caveat on "separates immediately" cells:** the diagonal claims above assume the tool layer is working correctly. A distinction that is logically immediate (e.g., `no_test_output: true` rules out a test failure) is only actually immediate if the extractor surfaces that field. This project encountered this failure mode directly: an early version of the log parser silently returned empty evidence rather than populating `no_test_output`, making a CI infra failure look identical to a flaky test with no output. The "immediate" separations in this table are therefore conditional on a correctly functioning tool layer — tool reliability is itself a load-bearing assumption in the diagnosis pipeline, and a legitimate point of fragility to acknowledge in any evaluation write-up.

---

## Tool-architecture mismatch: git-diff and lockfile-diff tools

### query_git_diff — removed from tool registry

`query_git_diff` was implemented and wired into the tool registry, then removed after
confirming that git commit history is not a useful diagnostic signal for this study's
injection methodology.

**Why it does not apply here.** All injection logic in `toy-repo-ci-test` lives in
`tests/test_calculator.py` as env-var-branching committed in the repository's initial
commit:

```python
if FAILURE_MODE == "real_regression":
    assert (multiply(4, 3) - 1) == 12    # deterministic failure
elif FAILURE_MODE == "flaky_test":
    assert random.random() >= FLAKY_RATE  # stochastic
# ... etc.
```

Every scenario is triggered by `workflow_dispatch` with a `failure_type` input; the
code never changes between runs. A diff between the latest commit and its parent shows
only scaffolding changes (assert message rewording, new scenario additions) — not the
mutation that explains the current run's failure, because the mutation is not encoded
in the commit history at all. In a real codebase, by contrast, a `real_regression`
scenario would typically be caused by a recent code change, and a git diff between the
failing and last-passing commits would expose it. The tool is architecturally correct
for that use case; it is simply not applicable to this study's injection approach.

**Why this matters for result interpretation.** The absence of `query_git_diff` from
the eval harness is a scope constraint, not a finding about the tool's usefulness. Any
eval results claiming that "git-diff evidence did not help distinguish regression from
flakiness" would be vacuously true for this corpus and should not be generalized. The
overlap-map entry for "Real regression vs. Dependency" (above) lists `query_git_diff`
as a discriminator — that relationship is real; it just cannot be demonstrated with
this injection model.

**Future work.** A natural extension is a real-commit injection architecture: each
scenario is encoded as a separate branch or tag, so triggering a `real_regression` run
means checking out a commit that actually contains the regression mutation in source.
That would make `query_git_diff` load-bearing and would let the eval harness test
whether agents correctly use commit history as a causal signal — currently untestable.

---

### query_dependency_lockfile_diff — decision not to build

`query_dependency_lockfile_diff` was listed in the overlap map as a discriminator for
the "Real regression vs. Dependency" and "Flaky vs. Dependency" overlaps. The same
architectural mismatch applies.

**Why it does not apply here.** The `env_dependency` and `regression_with_redherring`
scenarios are triggered by the *absence* of the `SIMULATED_DEPENDENCY` environment
variable at runner startup — not by any change to a requirements file or lockfile.
There is no lockfile diff to fetch: the dependency "change" exists only as an env-var
state in the runner, invisible to the GitHub compare API.

In a real codebase, a dependency failure would typically be preceded by a change to
`requirements.txt`, `pyproject.toml`, or a lockfile, and `query_dependency_lockfile_diff`
could surface that. For this study, fetching such a diff would always return empty
(no file changed), which is itself a signal in a real corpus but a misleading absence
here — the env change was never recorded in version control at all.

**Decision:** do not implement `query_dependency_lockfile_diff`. Building it and then
discovering that it always returns empty results for these scenarios would introduce
spurious negative evidence (the model might incorrectly treat "no lockfile diff" as
evidence against an env-dependency hypothesis, when the correct interpretation is that
the tool is inapplicable). The same future-work extension that makes `query_git_diff`
meaningful (real-commit injection) would also make a lockfile-diff tool meaningful —
the two tools are contingent on the same architectural precondition.

---

### query_ci_infra_status — removed after empirical confirmation

`query_ci_infra_status` queried the public githubstatus.com incidents API and was
wired into the tool registry to let the agent check for a documented GitHub Actions
platform incident around a failing run's timestamp. It was exercised across 3 runs of
the `ci_infra_issue` scenario. In all 3, it correctly reported no active GitHub
incidents at the failure timestamp — and in all 3, the model treated that absence as
evidence *against* `ci_infra_issue`, converging instead on `env_dependency` (wrong in
all 3 cases).

**Why it does not apply here.** As documented above (Type 4), `ci_infra_issue` in this
study's injection methodology is triggered by the workflow step "Simulate CI
infrastructure failure" exiting 1 before pytest runs — a scripted, repository-local
failure, never a real GitHub platform incident. "No GitHub Actions incidents at this
timestamp" is therefore structurally guaranteed to be true regardless of ground truth;
it carries zero information about whether *this* run's failure is infra-caused. This is
the same architectural mismatch as `query_git_diff` and `query_dependency_lockfile_diff`
above: a tool that would be genuinely diagnostic against a real-world failure surface
(an actual GitHub outage correlated with a real incident report) is inapplicable to an
injection methodology that never produces the condition the tool is built to detect.

**Note on sequencing.** This exclusion was decided during Phase 3 development, prior to
the eval harness commit freeze and prior to any formal sweep. It is not a post-hoc,
result-driven exclusion made after seeing aggregate accuracy numbers — it follows the
same reasoning already applied to `query_git_diff` and `query_dependency_lockfile_diff`,
confirmed empirically on 3 individual runs rather than inferred by analogy alone.

**What remains available.** The taxonomy's actual documented discriminator for
`ci_infra_issue` — `no_test_output: true` combined with `had_failures: true` (Type 4,
above), sourced from `query_test_failure_logs` / `query_ci_run_history` — is unaffected
by this removal and remains the intended path to correctly identifying this scenario.

---

## Deliberately ambiguous ground-truth cases

These are scenarios where the correct label is **"ambiguous / insufficient evidence"** rather than a single clean cause. They are necessary for evaluating RQ3 (calibrated abstention).

| Scenario | Why it is ambiguous | Infrastructure needed |
|---|---|---|
| **Type 6** — high-rate flakiness (~80%) with no rerun history | Cannot distinguish from a real regression on a single run; rate is high enough that even a small sample looks deterministic | **Implemented** (`ambiguous_flaky_or_regression`). Trigger once and suppress rerun history from agent tool responses. Rate controlled by `AMBIGUOUS_RATE` env var (default `0.8`), kept separate from `FLAKY_RATE` to prevent silent drift during batch eval runs. Ground truth: `"ambiguous"`. |
| **Type 7** — real regression with causally-inert dependency red herring | `SIMULATED_DEPENDENCY` absent creates a plausible env-change hypothesis, but the failing assertion is the regression mutation, not the dependency check | **Implemented** (`regression_with_redherring`). Ground truth: `real_regression`. |

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
