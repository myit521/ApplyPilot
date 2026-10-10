"""Run two capped DeepSeek calls against synthetic T11 cases, without retries."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx

from applypilot.jd_parser import _SYSTEM_PROMPT as JD_PROMPT, _parse_output
from applypilot.semantic_check import _SYSTEM_PROMPT as SEMANTIC_PROMPT, _build_user_prompt, semantic_check
from applypilot.schemas import Fact, ResumeClaim

ROOT = Path(__file__).resolve().parents[1]
JD_FIXTURE = ROOT / "tests/fixtures/t11_jds.json"
CLAIM_FIXTURE = ROOT / "tests/fixtures/t11_claims.json"
API_URL = "https://api.deepseek.com/chat/completions"


def request_once(api_key: str, system: str, user: str, *, max_tokens: int) -> dict:
    """Return only response fields needed for evaluation; never include HTTP error bodies."""
    try:
        response = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
            },
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        return {"error": type(exc).__name__}
    if response.status_code != 200:
        return {"http_status": response.status_code, "error": "http_error"}
    try:
        payload = response.json()
        choice = payload["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason != "stop":
            return {"http_status": 200, "finish_reason": finish_reason,
                    "error": "incomplete_completion"}
        content = choice["message"]["content"]
        if not isinstance(content, str) or len(content) > 8192:
            return {"http_status": 200, "finish_reason": finish_reason,
                    "error": "oversized_completion"}
        model = payload.get("model")
        usage = payload.get("usage")
        if (not isinstance(model, str) or len(model) > 100
                or not isinstance(usage, dict)
                or any(not isinstance(usage.get(key), int) or usage[key] < 0
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens"))):
            return {"http_status": 200, "error": "invalid_response_envelope"}
        return {
            "http_status": 200,
            "model": model,
            "finish_reason": finish_reason,
            "usage": {key: usage[key] for key in
                      ("prompt_tokens", "completion_tokens", "total_tokens")},
            "content": content,
        }
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return {"http_status": 200, "error": "invalid_response_envelope"}


def run(api_key: str) -> dict:
    jd_bytes = JD_FIXTURE.read_bytes()
    claim_bytes = CLAIM_FIXTURE.read_bytes()
    jd_case = next(row for row in json.loads(jd_bytes)["cases"] if row["id"] == "jd01")
    claim_case = next(row for row in json.loads(claim_bytes)["cases"] if row["id"] == "cl08")
    facts = [Fact.model_validate(row) for row in claim_case["facts"]]
    claim = ResumeClaim.model_validate(claim_case["claim"])
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    report = {
        "run": {"at_utc": datetime.now(timezone.utc).isoformat(), "source_commit": commit,
                "model_requested": "deepseek-chat", "temperature": 0.2,
                "requests": 2, "retries": 0, "synthetic_only": True},
        "fixtures_sha256": {"t11_jds": hashlib.sha256(jd_bytes).hexdigest(),
                            "t11_claims": hashlib.sha256(claim_bytes).hexdigest()},
        "cases": [],
    }
    requests = [
        ("jd01", JD_PROMPT, jd_case["jd_text"], 1000),
        ("cl08", SEMANTIC_PROMPT, _build_user_prompt([claim], {fact.id: fact for fact in facts}), 300),
    ]
    for case_id, system, user, max_tokens in requests:
        response = request_once(api_key, system, user, max_tokens=max_tokens)
        row = {"id": case_id, "max_output_tokens": max_tokens,
               "http_status": response.get("http_status"), "model": response.get("model"),
               "finish_reason": response.get("finish_reason"), "usage": response.get("usage")}
        if "error" in response:
            row["error"] = response["error"]
        elif case_id == "jd01":
            try:
                parsed = _parse_output(response["content"])
                row["observed"] = parsed.model_dump(mode="json")
            except Exception as exc:
                row["error"] = type(exc).__name__
        else:
            class FixedAdapter:
                def complete(self, system: str, user: str) -> str:
                    return response["content"]

            errors = semantic_check([claim], facts, FixedAdapter())
            row["observed_error_codes"] = [error.code.value for error in errors]
            row["observed_reasons"] = [error.detail for error in errors]
        report["cases"].append(row)
    report["limits"] = [
        "Two selected synthetic examples are not a quality or recall estimate.",
        "No production candidate information or recruitment site was sent.",
        "Raw model response and credentials are not stored in this report.",
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    report = run(api_key)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}; cases: " + ", ".join(
        f"{row['id']}={row.get('error', row.get('http_status'))}" for row in report["cases"]
    ))


if __name__ == "__main__":
    main()
