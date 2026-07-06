# agentic-diagnosis-agent

Main project repo: the agentic software-failure diagnosis system.

This repo contains the agent's reasoning loop, tool layer, evaluation
harness, and documentation. It investigates failures injected into a
separate, disposable test-subject repo: `toy-repo-ci-test`.

## Structure

- `tools/` — tool-layer scripts that gather evidence from the test-subject
  repo's CI runs (e.g. `trigger_and_fetch.py`)
- `docs/` — design docs (failure taxonomy, architecture)

## Related repo

Test subject: https://github.com/saraswati-niroula/toy-repo-ci-test
