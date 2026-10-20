"""Shipped startup logging must actually emit observations without caplog setup."""

import json
from pathlib import Path
import subprocess
import sys


def test_shipped_config_emits_model_events_without_exposing_payload():
    script = """
import json, logging.config, sys
from pathlib import Path
sys.path.insert(0, 'src')
logging.config.dictConfig(json.loads(Path('src/applypilot/logging.json').read_text()))
import httpx
from applypilot.deepseek_adapter import DeepSeekAdapter
httpx.post = lambda *a, **k: httpx.Response(200, json={
    'choices': [{'finish_reason': 'stop', 'message': {'content': 'private-output'}}]})
assert DeepSeekAdapter(api_key='private-key').complete('private-system', 'private-user') == 'private-output'
"""
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert [e["event"] for e in records] == ["model_call_started", "model_call_finished"]
    assert records[-1]["usage_status"] == "missing"
    assert "private-" not in result.stderr
    assert result.stdout == ""
