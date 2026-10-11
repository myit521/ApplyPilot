"""Compare named technologies in the frozen T15 report without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JD_FIXTURE = ROOT / "tests/fixtures/t11_jds.json"
GOLD = ROOT / "tests/fixtures/t16_named_technologies.json"
REPORT = ROOT / "docs/evaluation/t15-live.json"
IMPORTANCE = {"required", "preferred", "mentioned"}


def evaluate(jd_bytes: bytes, gold: dict, report_bytes: bytes) -> dict:
    if gold.get("schema_version") != 1:
        raise ValueError("unsupported annotation schema")
    if hashlib.sha256(jd_bytes).hexdigest() != gold.get("source_fixture_sha256"):
        raise ValueError("JD fixture changed after annotation")
    if hashlib.sha256(report_bytes).hexdigest() != gold.get("source_report_sha256"):
        raise ValueError("T15 report changed after annotation")
    report = json.loads(report_bytes)
    if report.get("fixtures_sha256", {}).get("t11_jds") != gold["source_fixture_sha256"]:
        raise ValueError("T15 report uses another JD fixture")
    if report.get("run", {}).get("status") != "complete":
        raise ValueError("T15 report is incomplete")

    jds = json.loads(jd_bytes)["cases"]
    jd_by_id = {case["id"]: case for case in jds}
    gold_rows = gold["cases"]
    observed_rows = [case for case in report["cases"] if case.get("kind") == "jd_parse"]
    if (len(jd_by_id) != len(jds) or len(gold_rows) != len(jds)
            or len(observed_rows) != len(jds)):
        raise ValueError("JD cases are missing or duplicated")
    gold_by_id = {case["id"]: case for case in gold_rows}
    observed_by_id = {case["id"]: case for case in observed_rows}
    if (len(gold_by_id) != len(gold_rows) or len(observed_by_id) != len(observed_rows)
            or set(jd_by_id) != set(gold_by_id) != set(observed_by_id)):
        raise ValueError("JD case IDs do not match")

    results = []
    total = found = correct_importance = 0
    for case in jds:
        case_id = case["id"]
        expected = {}
        for item in gold_by_id[case_id]["terms"]:
            term, level, span = item["term"], item["importance"], item["source_span"]
            key = term.casefold()
            if (not term.strip() or term != term.strip() or key in expected
                    or level not in IMPORTANCE or not span.strip()
                    or span not in case["jd_text"] or term.casefold() not in span.casefold()):
                raise ValueError(f"invalid named-technology annotation: {case_id} {term}")
            expected[key] = item
        observed = observed_by_id[case_id].get("observed")
        if not isinstance(observed, dict) or not isinstance(observed.get("keywords"), list):
            raise ValueError(f"missing parsed keywords: {case_id}")
        predictions = {}
        for item in observed["keywords"]:
            term, level = item["term"], item["importance"]
            key = term.casefold()
            if not term.strip() or key in predictions or level not in IMPORTANCE:
                raise ValueError(f"invalid observed keyword: {case_id} {term}")
            predictions[key] = item
        matches = []
        for key, item in expected.items():
            prediction = predictions.get(key)
            total += 1
            found += prediction is not None
            correct_importance += prediction is not None and prediction["importance"] == item["importance"]
            matches.append({"term": item["term"], "expected_importance": item["importance"],
                            "observed_importance": prediction["importance"] if prediction else None})
        results.append({"id": case_id, "named_technologies": matches,
                        "other_extracted_terms_for_manual_review": [
                            item["term"] for key, item in predictions.items() if key not in expected]})
    return {"scope": "explicit named technology terms in 10 synthetic JDs only",
            "named_terms": total, "found": found, "correct_importance": correct_importance,
            "cases": results,
            "limits": ["Other extracted terms are unscored, not automatically false positives.",
                       "This does not assess full requirement extraction, paraphrases or real jobs.",
                       "Annotations and evaluated model output use the same small synthetic set."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(JD_FIXTURE.read_bytes(),
                      json.loads(GOLD.read_text(encoding="utf-8")),
                      REPORT.read_bytes())
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
