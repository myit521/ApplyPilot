"""Checkpoint constructors stay bounded independently of global strict flags."""

from unittest.mock import Mock

from langgraph.checkpoint.serde import jsonplus
import ormsgpack
import pytest

from applypilot.checkpoint_serde import checkpoint_serializer


@pytest.mark.parametrize("code", [
    jsonplus.EXT_CONSTRUCTOR_SINGLE_ARG,
    jsonplus.EXT_CONSTRUCTOR_POS_ARGS,
    jsonplus.EXT_CONSTRUCTOR_KW_ARGS,
    jsonplus.EXT_PYDANTIC_V2,
])
def test_unknown_constructor_in_known_module_is_not_imported(code, monkeypatch):
    # ProfileData lives beside registered models but is not checkpoint state.
    # A harmless import spy proves blocking without executing arbitrary code.
    importer = Mock(side_effect=AssertionError("must not import a blocked constructor"))
    monkeypatch.setattr(jsonplus.importlib, "import_module", importer)
    raw = {"name": "合成资料"}
    encoded = ormsgpack.packb(ormsgpack.Ext(code, ormsgpack.packb(
        ("applypilot.schemas", "ProfileData", raw))))
    result = checkpoint_serializer().loads_typed(("msgpack", encoded))
    importer.assert_not_called()
    assert result == raw


def test_pickle_never_falls_back_to_loader(monkeypatch):
    loader = Mock(side_effect=AssertionError("pickle must not execute"))
    monkeypatch.setattr(jsonplus.pickle, "loads", loader)
    with pytest.raises(NotImplementedError, match="Unknown serialization type"):
        checkpoint_serializer().loads_typed(("pickle", b"synthetic-not-pickle"))
    loader.assert_not_called()


@pytest.mark.parametrize("strict", [False, True])
def test_explicit_policy_is_independent_of_global_strict_setting(monkeypatch, strict):
    monkeypatch.setattr(jsonplus._lg_msgpack, "STRICT_MSGPACK_ENABLED", strict)
    serializer = checkpoint_serializer()
    assert serializer._allowed_msgpack_modules is not True
    assert serializer.pickle_fallback is False
    assert ("applypilot.schemas", "Fact") in serializer._allowed_msgpack_modules
    assert ("applypilot.schemas", "ProfileData") not in serializer._allowed_msgpack_modules
