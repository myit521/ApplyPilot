"""Bounded production-parser probe, separating natural and seeded repair cases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from applypilot.jd_parser import JDParseError, _parse_checked, _parse_output, parse_jd
from applypilot.matching import _contains_term
from applypilot.model_adapter import ModelError
from scripts.evaluate_t14_live_probe import request_once
from scripts.evaluate_t18_live import _save, _source_commit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/t22_repair_cases.json"
SOURCE = ROOT / "docs/evaluation/t18-live.json"


def obligations(case: dict, observed: dict) -> list[str]:
    failures = []
    required = " | ".join(observed["required"])
    promoted = " | ".join(observed["required"] + observed["preferred"]
                          + [observed["experience_requirement"]])
    promoted += " | " + " | ".join(keyword["term"] for keyword in observed["keywords"]
                                   if keyword["importance"] != "mentioned")
    for term in case.get("must_required", []):
        if not _contains_term(required, term):
            failures.append(f"missing_required:{term}")
    for term in case.get("not_promoted", []):
        if _contains_term(promoted, term):
            failures.append(f"promoted_nonrequirement:{term}")
    for field, term in case.get("must_fields", {}).items():
        if term.casefold() not in observed[field].casefold():
            failures.append(f"missing_field:{field}:{term}")
    return failures


def run(api_key: str, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if not api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY is required")
    fixture_bytes, source_bytes = FIXTURE.read_bytes(), SOURCE.read_bytes()
    cases = json.loads(fixture_bytes)["cases"]
    source = {row["id"]: row for row in json.loads(source_bytes)["cases"]}
    if (len(cases) != 10 or len({case["id"] for case in cases}) != 10
            or sum(case["mode"] == "natural" for case in cases) != 8
            or sum(case["mode"] == "seeded_repair" for case in cases) != 2
            or any(case.get("source_id") not in source for case in cases if "source_id" in case)):
        raise ValueError("T22 requires the frozen eight-natural/two-seeded fixture")
    if not json.loads(source_bytes)["run"]["synthetic_only"]:
        raise ValueError("T22 accepts only the synthetic T18 report")
    for case in cases:
        if case["mode"] == "seeded_repair":
            original = source[case["source_id"]]
            try:
                _parse_checked(json.dumps(original["observed"], ensure_ascii=False), original["jd_text"])
            except JDParseError:
                continue
            raise ValueError("Seeded response must trigger the production repair gate")
    report = {
        "run": {"at_utc": datetime.now(timezone.utc).isoformat(),
                "source_commit": _source_commit(), "model_requested": "deepseek-chat",
                "temperature": 0.2, "request_cap": 18, "attempted_requests": 0,
                "max_output_tokens_per_request": 1000, "transport_retries": 0,
                "status": "running", "synthetic_only": True},
        "fixtures_sha256": {"t22_cases": hashlib.sha256(fixture_bytes).hexdigest(),
                            "t18_report": hashlib.sha256(source_bytes).hexdigest()},
        "cases": [],
        "limits": ["Selected known regressions cannot estimate natural error rates.",
                   "Seeded repair replaces the first response with frozen T18 output.",
                   "Obligations check selected substrings, not full semantic correctness.",
                   "Raw completion, credentials, and real candidate information are not stored."],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for case in cases:
        original = source.get(case.get("source_id"))
        jd = original["jd_text"] if original else case["jd_text"]
        seeded = case["mode"] == "seeded_repair"
        row = {"id": case["id"], "mode": case["mode"], "jd_text": jd,
               "seeded_first_response": seeded, "parser_calls": 0, "calls": []}
        report["cases"].append(row)

        class ProbeAdapter:
            def complete(self, system: str, user: str) -> str:
                row["parser_calls"] += 1
                if seeded and row["parser_calls"] == 1:
                    return json.dumps(original["observed"], ensure_ascii=False)
                if report["run"]["attempted_requests"] >= 18 or row["parser_calls"] > 2:
                    raise ModelError("request_cap")
                report["run"]["attempted_requests"] += 1
                response = request_once(api_key, system, user, max_tokens=1000)
                call = {key: response.get(key) for key in
                        ("http_status", "model", "finish_reason", "usage")}
                call["parser_call"] = row["parser_calls"]
                call["user_prompt_chars"] = len(user)
                if "error" in response:
                    call["error"] = response["error"]
                else:
                    try:
                        call["parsed_structure"] = _parse_output(response["content"]).model_dump(mode="json")
                    except JDParseError:
                        call["structure_error"] = "JDParseError"
                row["calls"].append(call)
                _save(output, report)
                if "error" in call:
                    raise ModelError(call["error"])
                return response["content"]

        try:
            observed = parse_jd(jd, ProbeAdapter()).model_dump(mode="json")
            row["outcome"] = "accepted"
            row["observed"] = observed
            row["obligation_failures"] = obligations(case, observed)
        except JDParseError as error:
            row["outcome"] = "rejected"
            row["gate_error"] = str(error)
        except ModelError:
            row["outcome"] = "transport_failed"
            report["run"]["status"] = "partial"
        except Exception as error:
            row["outcome"] = "runner_failed"
            row["error_type"] = type(error).__name__
            report["run"]["status"] = "partial"
        row["repair_attempted"] = row["parser_calls"] > 1
        _save(output, report)
        print(f"{case['id']}: {row['outcome']}; parser_calls={row['parser_calls']}", flush=True)
        if report["run"]["status"] == "partial":
            return report
    report["run"]["status"] = "complete"
    _save(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(os.environ.get("DEEPSEEK_API_KEY", ""), args.output)
    print(f"Recorded {len(report['cases'])}/10 cases; requests={report['run']['attempted_requests']}")
    if report["run"]["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
