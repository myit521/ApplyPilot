"""The retired command must not silently resume paid, unreviewed generation."""

from pathlib import Path
import runpy

import httpx
import pytest


def test_legacy_smoke_exits_without_http_or_interactive_confirmation(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("retired entry must not contact a service or prompt for approval")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    script = Path(__file__).resolve().parents[1] / "scripts/smoke_e2e.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")
    assert exit_info.value.code == 2
    assert "docs/delivery-checklist.md" in capsys.readouterr().err
