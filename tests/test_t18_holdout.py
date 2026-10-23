"""Freeze T18 source cases before the paid probe."""

import copy
import json

import pytest

from scripts.validate_t18_holdout import HOLDOUT, T11_JDS, validate


def _inputs():
    return HOLDOUT.read_bytes(), T11_JDS.read_bytes()


def test_holdout_is_new_and_source_grounded():
    result = validate(*_inputs())
    assert result["cases"] == 20
    assert result["source_atoms"] >= 80
    assert len(result["focuses"]) == 20


def test_duplicate_old_jd_is_rejected():
    holdout_bytes, t11_bytes = _inputs()
    data = json.loads(holdout_bytes)
    data["cases"][0]["jd_text"] = json.loads(t11_bytes)["cases"][0]["jd_text"]
    with pytest.raises(ValueError, match="repeated holdout JD"):
        validate(json.dumps(data, ensure_ascii=False).encode(), t11_bytes)


def test_fabricated_source_span_is_rejected():
    holdout_bytes, t11_bytes = _inputs()
    data = copy.deepcopy(json.loads(holdout_bytes))
    data["cases"][0]["atoms"][1]["source_span"] = "not in source"
    with pytest.raises(ValueError, match="invalid source atom"):
        validate(json.dumps(data, ensure_ascii=False).encode(), t11_bytes)
