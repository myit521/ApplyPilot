"""Evaluate 10 synthetic JDs and 9 determinate semantic claims with bounded calls."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from applypilot.jd_parser import _SYSTEM_PROMPT as JD_PROMPT, _parse_output
from applypilot.semantic_check import _SYSTEM_PROMPT as SEMANTIC_PROMPT, _build_user_prompt, semantic_check
from applypilot.schemas import Fact, ResumeClaim
from scripts.evaluate_t14_live_probe import request_once

ROOT = Path(__file__).resolve().parents[1]
JD_FIXTURE = ROOT / "tests/fixtures/t11_jds.json"
CLAIM_FIXTURE = ROOT / "tests/fixtures/t11_claims.json"
SEMANTIC_IDS = {f"cl{number:02d}" for number in (1, 2, 3, 4, 5, 6, 8, 9, 10)}


def _save(output: Path, report: dict) -> None:
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def run(api_key: str, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing report: {output}")
    jd_bytes = JD_FIXTURE.read_bytes()
    claim_bytes = CLAIM_FIXTURE.read_bytes()
    jd_cases = json.loads(jd_bytes)["cases"]
    all_claims = json.loads(claim_bytes)["cases"]
    claim_cases = [case for case in all_claims if case["id"] in SEMANTIC_IDS]
    if (len(jd_cases) != 10 or len(all_claims) != 20 or len(claim_cases) != 9
            or {case["id"] for case in claim_cases} != SEMANTIC_IDS):
        raise ValueError("T15 requires the frozen T11 10-JD/20-claim fixture")
    output.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    report = {
        "run": {"at_utc": datetime.now(timezone.utc).isoformat(), "source_commit": commit,
                "model_requested": "deepseek-chat", "temperature": 0.2,
                "planned_requests": 19, "attempted_requests": 0, "status": "running",
                "retries": 0, "synthetic_only": True},
        "fixtures_sha256": {"t11_jds": hashlib.sha256(jd_bytes).hexdigest(),
                            "t11_claims": hashlib.sha256(claim_bytes).hexdigest()},
        "cases": [],
        "limits": [
            "Synthetic selected cases cannot estimate real recruitment performance.",
            "JD wording differences need human review, not exact-string accuracy.",
            "Only six supported and three semantic overclaim claims enter this model-only probe.",
            "Deterministic-only and uncertain claims are excluded from semantic binary metrics.",
            "Each response is recorded before the next call; existing reports cannot be overwritten.",
        ],
    }
    _save(output, report)
    for case in jd_cases:
        response = request_once(api_key, JD_PROMPT, case["jd_text"], max_tokens=1000)
        row = {"id": case["id"], "kind": "jd_parse", "jd_text": case["jd_text"],
               "gold": case["requirements"], "max_output_tokens": 1000,
               "http_status": response.get("http_status"), "model": response.get("model"),
               "finish_reason": response.get("finish_reason"), "usage": response.get("usage")}
        if "error" in response:
            row["error"] = response["error"]
        else:
            try:
                row["observed"] = _parse_output(response["content"]).model_dump(mode="json")
            except Exception as exc:
                row["error"] = type(exc).__name__
        report["cases"].append(row)
        report["run"]["attempted_requests"] += 1
        if "error" in response:
            report["run"]["status"] = "partial"
        _save(output, report)
        print(f"{case['id']}: {row.get('error', 'parsed')}", flush=True)
        if "error" in response:
            return report

    for case in claim_cases:
        facts = [Fact.model_validate(fact) for fact in case["facts"]]
        claim = ResumeClaim.model_validate(case["claim"])
        user = _build_user_prompt([claim], {fact.id: fact for fact in facts})
        response = request_once(api_key, SEMANTIC_PROMPT, user, max_tokens=300)
        row = {"id": case["id"], "kind": "semantic_review", "gold_label": case["label"],
               "max_output_tokens": 300, "http_status": response.get("http_status"),
               "model": response.get("model"), "finish_reason": response.get("finish_reason"),
               "usage": response.get("usage")}
        if "error" in response:
            row["error"] = response["error"]
        else:
            class FixedAdapter:
                def complete(self, system: str, user: str) -> str:
                    return response["content"]

            errors = semantic_check([claim], facts, FixedAdapter())
            row["observed_error_codes"] = [error.code.value for error in errors]
            row["observed_reasons"] = [error.detail for error in errors]
        report["cases"].append(row)
        report["run"]["attempted_requests"] += 1
        if "error" in response:
            report["run"]["status"] = "partial"
        _save(output, report)
        print(f"{case['id']}: {row.get('error', row.get('observed_error_codes'))}", flush=True)
        if "error" in response:
            return report
    report["run"]["status"] = "complete"
    _save(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    report = run(api_key, args.output)
    print(f"Recorded {len(report['cases'])}/19 cases in {args.output}")
    if report["run"]["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
