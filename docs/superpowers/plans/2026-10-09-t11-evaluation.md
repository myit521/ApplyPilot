# T11 Evaluation and Regression Implementation Plan

> **For agentic workers:** Use subagent-driven-development for bounded fixture work and independent review; the coordinator owns evaluator integration and final verification. Steps use checkboxes for tracking.

**Goal:** Produce a reproducible, labeled evaluation baseline and a replayable normal/failure/recovery demonstration without claiming synthetic tests measure real-model quality.

**Architecture:** Two versioned JSON fixtures hold ten JD cases and twenty claim cases. A read-only Python evaluator calls the existing matching and deterministic validation functions, emits per-case results and clearly scoped aggregate counts, while a separate command set replays existing PostgreSQL workflow regression tests. Live model evaluation remains a separate recorded mode and is not inferred from these results.

**Tech Stack:** Python 3.13, Pydantic, pytest, existing FastAPI/PostgreSQL Testcontainers tests.

**Spec:** [Roadmap T11](../../roadmap.md), [evaluation definitions](../../validation-and-demo.md).

## Global Constraints

- No real application submission, external messages, or silent model calls.
- Fixtures use only synthetic information; labels and reasons must be human-readable.
- Do not report a percentage without numerator, denominator, fixture hash, and code revision.
- The matcher measures literal keyword evidence; deterministic validation does not measure semantic overclaim detection.

## Review Focus

- A preferred JD clause must not silently count as required; fixture categories are gold labels.
- A `supported` matcher status with the wrong cited fact must count as an evidence error.
- An overclaim with no deterministic error code remains a semantic-review case, not a rules pass.
- Zero denominators must render as `null`/N/A, never 100%.
- A malformed or incomplete fixture must fail before printing a success report.

### Task 1: Freeze labeled fixtures

**Files:** Create `tests/fixtures/t11_jds.json`, `tests/fixtures/t11_claims.json`.

**Interfaces:** JD top level `schema_version`, `facts`, `cases`; each case has `id`, `jd_text`, `requirements`, `expected_requirements` (`category`, `text`, `expected_status`, `reason`, `supporting_fact_ids`). Claim top level `schema_version`, `cases`; each has `id`, `facts`, `claim`, `label`, `reason`, `expected_error_codes`.

- [ ] Write ten distinct JD cases and twenty claim cases with independently readable rationales.
- [ ] Validate JSON syntax and Pydantic `Fact`, `JobRequirements`, and `ResumeClaim` shapes.
- [ ] Review balance: supported, partial, no evidence, unknown; numeric, skill, citation, and semantic boundaries.

### Task 2: Evaluate deterministic layers without inflating metrics

**Files:** Create `scripts/evaluate_t11.py`, `tests/test_t11_evaluation.py`.

**Interfaces:** `evaluate(jd_fixture: dict, claim_fixture: dict) -> dict` returns fixture counts, per-case observed results, and aggregate numerators/denominators. CLI `python scripts/evaluate_t11.py --output <path>` writes JSON; default prints JSON and performs no model/network call.

- [ ] Write tests that reject duplicate/missing labels and check zero-denominator behavior.
- [ ] Run `python -m pytest -q tests/test_t11_evaluation.py` and confirm failure before implementation.
- [ ] Implement minimal fixture validation, matcher/validator calls, scoped metrics, fixture SHA-256, and code revision metadata.
- [ ] Run focused tests and inspect every mismatched case; preserve observed failures rather than editing gold labels to match code.

### Task 3: Replay flow failures and publish evidence

**Files:** Create `docs/t11-execution.md`; update `docs/roadmap.md`, `docs/validation-and-demo.md`, and `README.md` only for confirmed results.

- [ ] Run the existing normal-flow, validation-failure, approval rollback, and restart-recovery tests against temporary PostgreSQL; record exact commands/results.
- [ ] Run the evaluator and save a synthetic JSON report under `docs/evaluation/` with fixture hashes and code revision.
- [ ] Run full pytest, review logs for secrets and side effects, and obtain independent Agent review of labels, metrics, and changed files.
- [ ] Document unrun live-model and real-browser coverage separately; commit/push only after verification.

## Self-review

The plan separates parsed-JD matching from parser quality, deterministic claims from semantic review, and flow reliability from model accuracy. Recall@5 is omitted until a ranking-specific gold set exists; T11 will state that gap rather than invent a comparable score. This plan executes under the user's standing authorization to proceed without a confirmation pause.
