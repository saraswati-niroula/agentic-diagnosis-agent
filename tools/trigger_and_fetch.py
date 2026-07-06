"""
trigger_and_fetch.py

Proof-of-concept for the core mechanic your agent's tool layer will depend
on: programmatically trigger a CI run with a specific injected failure type,
then poll until it completes and fetch the result + logs.

This is deliberately a standalone script, not yet wired into the agent loop.
The goal right now is just to prove the GitHub API round-trip works reliably
before building anything more elaborate on top of it.

Requirements:
    pip install requests

Usage:
    export GITHUB_TOKEN=ghp_xxxxxxxxxxxx   # needs repo + workflow scope
    python scripts/trigger_and_fetch.py --owner YOUR_USERNAME --repo toy-repo-ci-test --failure-type flaky_test

Notes:
- Requires a Personal Access Token (classic or fine-grained) with permission
  to trigger workflow_dispatch events and read Actions runs on this repo.
- The workflow file must already be pushed to the default branch (main)
  before workflow_dispatch will accept a dispatch request for it.
"""

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

GITHUB_API = "https://api.github.com"
WORKFLOW_FILENAME = "test.yml"


def trigger_run(owner, repo, failure_type, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILENAME}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "ref": "master",
        "inputs": {"failure_type": failure_type},
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 204:
        raise RuntimeError(f"Failed to trigger workflow: {resp.status_code} {resp.text}")
    print(f"Triggered workflow_dispatch with failure_type={failure_type}")


def get_latest_run(owner, repo, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, params={"per_page": 1})
    resp.raise_for_status()
    runs = resp.json()["workflow_runs"]
    if not runs:
        return None
    return runs[0]


def poll_until_complete(owner, repo, token, run_id, timeout_seconds=180, interval_seconds=8):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    waited = 0
    while waited < timeout_seconds:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        run = resp.json()
        status = run["status"]  # queued, in_progress, completed
        print(f"  status={status} (waited {waited}s)")
        if status == "completed":
            return run
        time.sleep(interval_seconds)
        waited += interval_seconds
    raise TimeoutError("Workflow run did not complete within the timeout window")


def fetch_logs_url(owner, repo, token, run_id):
    # GitHub returns a redirect to a zip download of all logs for the run.
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, allow_redirects=False)
    if resp.status_code == 302:
        return resp.headers.get("Location")
    return None


def download_zip(presigned_url):
    """Download the log zip from the GitHub-issued presigned URL (no auth needed)."""
    resp = requests.get(presigned_url, timeout=60)
    resp.raise_for_status()
    return resp.content


# GitHub Actions log lines are prefixed with a timestamp, e.g.:
# 2024-01-01T00:00:00.0000000Z FAILED tests/...
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?")


def _strip_timestamps(text):
    return "\n".join(_TIMESTAMP_RE.sub("", line) for line in text.splitlines())


def parse_pytest_output(text):
    """
    Parse pytest -v --tb=short output and return a structured evidence dict:
        had_failures    bool
        failures_detail list of {test_id, message}
        short_summary   str  (the FAILED/ERROR lines from pytest's summary block)
        summary_line    str  (e.g. "1 failed, 7 passed in 0.23s")
    """
    text = _strip_timestamps(text)
    lines = text.splitlines()

    failures_detail = []
    short_summary_lines = []
    summary_line = ""
    in_short_summary = False

    for line in lines:
        if "short test summary info" in line:
            in_short_summary = True
            continue
        if in_short_summary:
            if line.startswith("="):
                in_short_summary = False
                continue
            if line.startswith("FAILED ") or line.startswith("ERROR "):
                short_summary_lines.append(line)
                m = re.match(r"(?:FAILED|ERROR)\s+(\S+)\s+-\s+(.*)", line)
                if m:
                    failures_detail.append({
                        "test_id": m.group(1),
                        "message": m.group(2).strip(),
                    })

    # Final summary line: "== 1 failed, 7 passed in 0.23s =="
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("=") and re.search(r"\d+ (?:failed|passed|error)", stripped):
            summary_line = re.sub(r"^=+\s*|\s*=+$", "", stripped).strip()
            break

    had_failures = bool(failures_detail) or "failed" in summary_line or "error" in summary_line

    return {
        "had_failures": had_failures,
        "failures_detail": failures_detail,
        "short_summary": "\n".join(short_summary_lines),
        "summary_line": summary_line,
    }


def extract_evidence_from_zip(zip_bytes, run_conclusion=None):
    """
    Unzip the GitHub Actions log archive, locate the pytest step log, and
    return parsed evidence.  The zip contains one .txt per step named like:
        test/3_Run test suite.txt

    If the "Run test suite" step log is absent (pytest never ran because a
    pre-test step failed), returns a distinct evidence shape with
    no_test_output=True — that absence is itself a diagnostic signal.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        target = next(
            (n for n in names if "run test suite" in n.lower()),
            None,
        )
        if target is None:
            return {
                "had_failures": run_conclusion == "failure",
                "failures_detail": [],
                "short_summary": "",
                "summary_line": "",
                "no_test_output": True,
                "available_step_logs": sorted(names),
            }
        log_text = zf.read(target).decode("utf-8", errors="replace")
        evidence = parse_pytest_output(log_text)
        evidence["log_file"] = target
        return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, help="GitHub username or org")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument(
        "--failure-type",
        required=True,
        choices=["none", "flaky_test", "real_regression", "env_dependency", "schema_change", "ci_infra_issue"],
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN environment variable first.", file=sys.stderr)
        sys.exit(1)

    trigger_run(args.owner, args.repo, args.failure_type, token)

    print("Waiting a few seconds for the run to register...")
    time.sleep(6)

    run = get_latest_run(args.owner, args.repo, token)
    if not run:
        print("No runs found yet — try again in a few seconds.", file=sys.stderr)
        sys.exit(1)

    run_id = run["id"]
    print(f"Found run id={run_id}, polling until complete...")

    completed_run = poll_until_complete(args.owner, args.repo, token, run_id)

    print("\n--- RESULT ---")
    print(f"Run ID:      {completed_run['id']}")
    print(f"Conclusion:  {completed_run['conclusion']}")  # success / failure
    print(f"HTML URL:    {completed_run['html_url']}")

    logs_url = fetch_logs_url(args.owner, args.repo, token, run_id)
    if not logs_url:
        print("Logs URL not available — check the HTML URL above manually.")
        return

    print("Downloading logs zip...")
    zip_bytes = download_zip(logs_url)
    evidence = extract_evidence_from_zip(zip_bytes, run_conclusion=completed_run["conclusion"])

    print("\n--- EVIDENCE SUMMARY ---")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
