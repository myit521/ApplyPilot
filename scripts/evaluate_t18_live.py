"""Run one capped model call for each frozen synthetic T18 JD."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from applypilot.jd_parser import _SYSTEM_PROMPT as JD_PROMPT, _parse_output
from scripts.evaluate_t14_live_probe import request_once
from scripts.validate_t18_holdout import HOLDOUT, T11_JDS, ROOT, validate


def _source_commit() -> str:
    status = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "status", "--porcelain",
         "--untracked-files=normal"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("Commit or remove workspace changes before model evaluation")
    return subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _save(output: Path, report: dict) -> None:
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def run(api_key: str, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing report: {output}")
    if not api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY is required")
    holdout_bytes, t11_bytes = HOLDOUT.read_bytes(), T11_JDS.read_bytes()
    validation = validate(holdout_bytes, t11_bytes)
    cases = json.loads(holdout_bytes)["cases"]
    commit = _source_commit()
    report = {
        "run": {"at_utc": datetime.now(timezone.utc).isoformat(),
                "source_commit": commit, "model_requested": "deepseek-chat",
                "temperature": 0.2, "planned_requests": 20,
                "attempted_requests": 0, "max_output_tokens_per_case": 1000,
                "status": "running", "retries": 0, "synthetic_only": True},
        "fixtures_sha256": {"t18_holdout_jds": validation["holdout_sha256"],
                            "t11_jds": hashlib.sha256(t11_bytes).hexdigest()},
        "cases": [],
        "limits": [
            "These authored synthetic cases are not a representative quality estimate.",
            "Source atoms are human annotations, not automatic scoring labels.",
            "Observed wording and omissions require later human adjudication.",
            "No real candidate information or recruitment site is sent.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for case in cases:
        try:
            response = request_once(api_key, JD_PROMPT, case["jd_text"], max_tokens=1000)
            row = {"id": case["id"], "jd_text": case["jd_text"],
                   "max_output_tokens": 1000, "http_status": response.get("http_status"),
                   "model": response.get("model"), "finish_reason": response.get("finish_reason"),
                   "usage": response.get("usage")}
            if "error" in response:
                row["error"] = response["error"]
            else:
                try:
                    row["observed"] = _parse_output(response["content"]).model_dump(mode="json")
                except Exception as exc:
                    row["error"] = type(exc).__name__
        except Exception as exc:
            row = {"id": case["id"], "jd_text": case["jd_text"],
                   "max_output_tokens": 1000, "error": type(exc).__name__}
        report["cases"].append(row)
        report["run"]["attempted_requests"] += 1
        if "error" in row:
            report["run"]["status"] = "partial"
        _save(output, report)
        print(f"{case['id']}: {row.get('error', 'parsed')}", flush=True)
        if "error" in row:
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
    print(f"Recorded {len(report['cases'])}/20 cases in {args.output}")
    if report["run"]["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
