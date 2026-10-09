"""Evaluate T11's labeled synthetic cases without model or network calls."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from applypilot.matching import build_match_report
from applypilot.schemas import ErrorCode, Fact, JobRequirements, ResumeClaim
from applypilot.validation import validate_claims

ROOT = Path(__file__).resolve().parents[1]
JD_FIXTURE = ROOT / "tests/fixtures/t11_jds.json"
CLAIM_FIXTURE = ROOT / "tests/fixtures/t11_claims.json"
MATCH_STATUSES = {"supported", "partial", "no_evidence", "unknown"}
CLAIM_LABELS = {"supported", "overclaim", "unsupported", "uncertain"}


def _ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def _nonblank(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be nonblank")
    return value


def _unique_cases(cases: object, location: str) -> list[dict]:
    if not isinstance(cases, list):
        raise ValueError(f"{location} must be a list")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError(f"{location} contains a non-object")
        case_id = _nonblank(case.get("id"), f"{location}.id")
        if case_id in ids:
            raise ValueError(f"duplicate {location} id: {case_id}")
        ids.add(case_id)
    return cases


def _facts(rows: object, location: str) -> list[Fact]:
    if not isinstance(rows, list):
        raise ValueError(f"{location} must be a list")
    facts = [Fact.model_validate(row) for row in rows]
    ids = [fact.id for fact in facts]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate fact id in {location}")
    return facts


def evaluate(jd_fixture: dict, claim_fixture: dict) -> dict:
    """Return observed rule results; labels remain independent gold data."""
    if jd_fixture.get("schema_version") != 1 or claim_fixture.get("schema_version") != 1:
        raise ValueError("fixture schema_version must be 1")
    jd_cases = _unique_cases(jd_fixture.get("cases"), "JD cases")
    claim_cases = _unique_cases(claim_fixture.get("cases"), "claim cases")
    shared_facts = _facts(jd_fixture.get("facts"), "JD facts")
    active_ids = {fact.id for fact in shared_facts if fact.enabled and fact.status == "confirmed"}

    match_rows = []
    status_hits = status_total = evidence_hits = evidence_total = predicted_evidence_total = 0
    for case in jd_cases:
        _nonblank(case.get("jd_text"), f"JD {case['id']}.jd_text")
        requirements = JobRequirements.model_validate(case.get("requirements"))
        observed = build_match_report(requirements, shared_facts)["requirements"]
        observed_by_key = {(row["category"], row["text"]): row for row in observed}
        if len(observed_by_key) != len(observed):
            raise ValueError(f"JD {case['id']} has duplicate parsed requirements")
        gold = case.get("expected_requirements")
        if not isinstance(gold, list) or not gold:
            raise ValueError(f"JD {case['id']} needs expected_requirements")
        gold_keys = set()
        rows = []
        for expected in gold:
            if not isinstance(expected, dict):
                raise ValueError(f"JD {case['id']} has a non-object gold requirement")
            key = (_nonblank(expected.get("category"), "category"),
                   _nonblank(expected.get("text"), "text"))
            _nonblank(expected.get("reason"), "gold reason")
            if key in gold_keys or key not in observed_by_key:
                raise ValueError(f"JD {case['id']} duplicate or unmatched gold requirement: {key}")
            gold_keys.add(key)
            expected_status = expected.get("expected_status")
            if expected_status not in MATCH_STATUSES:
                raise ValueError(f"JD {case['id']} invalid expected_status")
            gold_ids = expected.get("supporting_fact_ids")
            if not isinstance(gold_ids, list) or len(gold_ids) != len(set(gold_ids)):
                raise ValueError(f"JD {case['id']} invalid supporting_fact_ids")
            if not set(gold_ids) <= active_ids:
                raise ValueError(f"JD {case['id']} cites missing or inactive gold facts")
            actual = observed_by_key[key]
            actual_ids = {item["fact_id"] for item in actual["evidence"]}
            hits = len(actual_ids.intersection(gold_ids))
            evidence_hits += hits
            evidence_total += len(gold_ids)
            predicted_evidence_total += len(actual_ids)
            status_hits += actual["status"] == expected_status
            status_total += 1
            rows.append({"category": key[0], "text": key[1],
                         "expected_status": expected_status, "observed_status": actual["status"],
                         "gold_fact_ids": gold_ids, "observed_fact_ids": sorted(actual_ids),
                         "status_correct": actual["status"] == expected_status})
        if gold_keys != set(observed_by_key):
            raise ValueError(f"JD {case['id']} has unlabelled parsed requirements")
        match_rows.append({"id": case["id"], "requirements": rows})

    claim_rows = []
    detectable_hits = detectable_total = supported_blocks = supported_total = 0
    code_hits = code_total = 0
    semantic_cases = []
    manual_cases = []
    for case in claim_cases:
        label = case.get("label")
        if label not in CLAIM_LABELS:
            raise ValueError(f"claim {case['id']} has invalid label")
        _nonblank(case.get("reason"), f"claim {case['id']}.reason")
        facts = _facts(case.get("facts"), f"claim {case['id']}.facts")
        claim = ResumeClaim.model_validate(case.get("claim"))
        expected_codes = case.get("expected_error_codes")
        if (not isinstance(expected_codes, list) or len(expected_codes) != len(set(expected_codes))
                or not set(expected_codes) <= {code.value for code in ErrorCode}):
            raise ValueError(f"claim {case['id']} has invalid expected_error_codes")
        actual_codes = sorted({error.code.value for error in validate_claims([claim], facts)})
        if label == "supported":
            supported_total += 1
            supported_blocks += bool(actual_codes)
        elif label in ("overclaim", "unsupported"):
            if expected_codes:
                detectable_total += 1
                detectable_hits += bool(actual_codes)
                code_total += len(expected_codes)
                code_hits += len(set(expected_codes).intersection(actual_codes))
            else:
                semantic_cases.append(case["id"])
        if label == "uncertain" or (label == "overclaim" and not expected_codes):
            manual_cases.append(case["id"])
        claim_rows.append({"id": case["id"], "label": label,
                           "expected_error_codes": expected_codes,
                           "observed_error_codes": actual_codes,
                           "rule_code_match": set(expected_codes) == set(actual_codes)})

    return {
        "dataset": {"jd_cases": len(jd_cases), "claim_cases": len(claim_cases)},
        "matching": {"cases": match_rows, "status_accuracy": _ratio(status_hits, status_total),
                     "evidence_recall": _ratio(evidence_hits, evidence_total),
                     "evidence_precision": _ratio(evidence_hits, predicted_evidence_total)},
        "claims": {"cases": claim_rows,
                   "rule_detectable_violation_recall": _ratio(detectable_hits, detectable_total),
                   "expected_code_recall": _ratio(code_hits, code_total),
                   "supported_false_blocks": _ratio(supported_blocks, supported_total),
                   "semantic_overclaim_cases": semantic_cases,
                   "manual_review_cases": manual_cases},
        "limits": ["parsed JD is supplied as gold input; JD parser quality is not measured",
                   "keyword evidence does not prove semantic support",
                   "deterministic claim rules do not measure semantic-review quality",
                   "workflow recovery and real model calls are excluded from these scores"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    args = parser.parse_args()
    jd_bytes = JD_FIXTURE.read_bytes()
    claim_bytes = CLAIM_FIXTURE.read_bytes()
    report = evaluate(json.loads(jd_bytes), json.loads(claim_bytes))
    if report["dataset"]["jd_cases"] < 10 or report["dataset"]["claim_cases"] < 20:
        raise ValueError("T11 baseline requires at least 10 JDs and 20 claims")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip())
    report["run"] = {
        "mode": "deterministic_no_model", "model_id": None, "model_calls": 0,
        "prompt_version": None, "parameters": {}, "cost": None,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "commit": commit,
        "working_tree_dirty": dirty,
        "fixtures_sha256": {"jds": hashlib.sha256(jd_bytes).hexdigest(),
                            "claims": hashlib.sha256(claim_bytes).hexdigest()},
    }
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
