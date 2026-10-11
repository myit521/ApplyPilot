"""Validate human JD atom mappings against the frozen T15 report, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JD_FIXTURE = ROOT / "tests/fixtures/t11_jds.json"
ANNOTATIONS = ROOT / "tests/fixtures/t17_jd_atoms.json"
REPORT = ROOT / "docs/evaluation/t15-live.json"
EXPECTED_FIELD = {
    "title": "job_title", "location": "location",
    "required": "required", "preferred": "preferred",
    "responsibility": "responsibilities", "education": "education_requirement",
    "graduation": "graduation_time_requirement", "open_question": "unknowns",
}
LIST_FIELDS = {"required", "preferred", "responsibilities", "unknowns"}


def _unique_by_id(rows: list[dict], label: str) -> dict[str, dict]:
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate {label} ID")
    return result


def review(jd_bytes: bytes, annotation: dict, report_bytes: bytes) -> dict:
    if annotation.get("schema_version") != 1:
        raise ValueError("unsupported annotation schema")
    if hashlib.sha256(jd_bytes).hexdigest() != annotation.get("source_fixture_sha256"):
        raise ValueError("JD fixture changed after annotation")
    if hashlib.sha256(report_bytes).hexdigest() != annotation.get("source_report_sha256"):
        raise ValueError("T15 report changed after annotation")
    jd_cases = json.loads(jd_bytes)["cases"]
    report = json.loads(report_bytes)
    if (report.get("run", {}).get("status") != "complete"
            or report.get("fixtures_sha256", {}).get("t11_jds") != annotation["source_fixture_sha256"]):
        raise ValueError("T15 report is incomplete or uses another fixture")
    observed_cases = [row for row in report["cases"] if row.get("kind") == "jd_parse"]
    jds = _unique_by_id(jd_cases, "fixture")
    annotations = _unique_by_id(annotation["cases"], "annotation")
    observed = _unique_by_id(observed_cases, "report")
    if len(jds) != 10 or set(jds) != set(annotations) or set(jds) != set(observed):
        raise ValueError("JD cases are missing or do not match")

    rows = []
    verdicts = {"covered": 0, "needs_review": 0}
    for case_id, jd in jds.items():
        parsed = observed[case_id].get("observed")
        if not isinstance(parsed, dict) or observed[case_id].get("jd_text") != jd["jd_text"]:
            raise ValueError(f"missing or mismatched parsed JD: {case_id}")
        seen_atoms = set()
        linked = set()
        atom_rows = []
        if not annotations[case_id]["atoms"]:
            raise ValueError(f"JD has no annotated atoms: {case_id}")
        for item in annotations[case_id]["atoms"]:
            atom = item["atom"]
            kind = item["kind"]
            field = item["observed_field"]
            value = item["observed_text"]
            verdict = item["verdict"]
            source = item["source_span"]
            if (not isinstance(atom, str) or not atom.strip() or atom in seen_atoms
                    or not isinstance(source, str) or not source.strip()
                    or source not in jd["jd_text"]
                    or not isinstance(value, str) or not value.strip()):
                raise ValueError(f"invalid source atom: {case_id} {atom}")
            seen_atoms.add(atom)
            label = atom.removeprefix("岗位：").removeprefix("地点：")
            note = item.get("normalization_note", "")
            if label not in source and not isinstance(note, str):
                raise ValueError(f"invalid normalization note: {case_id} {atom}")
            if label not in source and not note.strip():
                raise ValueError(f"missing normalization note: {case_id} {atom}")
            if kind not in EXPECTED_FIELD and kind != "unspecified":
                raise ValueError(f"invalid atom kind: {case_id} {atom}")
            if field not in EXPECTED_FIELD.values():
                raise ValueError(f"invalid observed field: {case_id} {atom}")
            if verdict not in verdicts:
                raise ValueError(f"invalid verdict: {case_id} {atom}")
            if verdict == "covered" and (kind == "unspecified" or field != EXPECTED_FIELD[kind]):
                raise ValueError(f"unjustified covered verdict: {case_id} {atom}")
            if verdict == "needs_review" and not str(item.get("reason", "")).strip():
                raise ValueError(f"review reason missing: {case_id} {atom}")
            observed_value = parsed.get(field)
            if field in LIST_FIELDS:
                if not isinstance(observed_value, list) or value not in observed_value:
                    raise ValueError(f"unmatched observed text: {case_id} {atom}")
            elif observed_value != value:
                raise ValueError(f"unmatched observed scalar: {case_id} {atom}")
            linked.add((field, value))
            verdicts[verdict] += 1
            atom_rows.append({"atom": atom, "kind": kind, "source_span": source,
                              "observed_field": field, "observed_text": value,
                              "verdict": verdict,
                              **({"normalization_note": note} if note else {}),
                              **({"reason": item["reason"]} if verdict == "needs_review" else {})})
        unlinked = [{"field": field, "text": value}
                    for field in ("required", "preferred", "responsibilities")
                    for value in parsed.get(field, []) if (field, value) not in linked]
        rows.append({"id": case_id, "atoms": atom_rows, "unlinked_output_items": unlinked})
    return {"scope": "human atom mapping of 10 previously seen synthetic JDs",
            "adjudications": verdicts, "cases": rows,
            "limits": ["Covered is a human mapping with checked source/output pointers, not model accuracy.",
                       "The validator cannot prove that a human annotated every clause.",
                       "Unlinked output may duplicate another field; it is not automatically wrong.",
                       "Same fixtures and responses as T15; no independent test set or new model call."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = review(JD_FIXTURE.read_bytes(),
                    json.loads(ANNOTATIONS.read_text(encoding="utf-8")),
                    REPORT.read_bytes())
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
