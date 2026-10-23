"""Read trusted legacy checkpoints with the production serializer after restart."""

from datetime import date
import json
import uuid

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from applypilot.schemas import (
    ErrorCode, Fact, FactType, JobRequirements, ResumeSections, ValidationError,
)
from applypilot.workflow import WorkflowStatus, build_graph

pytestmark = pytest.mark.integration


class Adapter:
    def __init__(self, invalid=False):
        self.invalid = invalid
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if "职位描述解析器" in system:
            return json.dumps({"required": ["Java"], "keywords": [
                {"term": "Java", "importance": "required"}]})
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        text = "参与 Java 开发，效率提升 99%" if self.invalid else "参与 Java 开发"
        return json.dumps({"sections": {"experience": [
            {"text": text, "fact_ids": ["t30_synthetic_fact"]}]}})


@pytest.fixture(scope="module")
def checkpoint_database():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.mark.parametrize("invalid", [False, True])
def test_legacy_checkpoint_state_and_pending_interrupt_survive_restart(
    checkpoint_database, invalid, caplog,
):
    from applypilot.checkpoint_serde import checkpoint_serializer

    fact = Fact(id="t30_synthetic_fact", status="confirmed", fact_type=FactType.PROJECT,
                source_name="合成测试", content="参与 Java 开发", skills=["Java"],
                start_date=date(2025, 1, 1))
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    first_adapter = Adapter(invalid)
    # Deliberately use the old permissive default to generate historical-format data.
    with psycopg.connect(checkpoint_database, autocommit=True, prepare_threshold=0) as conn:
        old_saver = PostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=True))
        old_saver.setup()
        graph = build_graph(first_adapter, lambda requirements: [fact], old_saver)
        graph.invoke({"jd_text": "招聘 Java 开发"}, config)
        before = graph.get_state(config)

    caplog.clear()
    new_adapter = Adapter()
    with psycopg.connect(checkpoint_database, autocommit=True, prepare_threshold=0) as conn:
        new_saver = PostgresSaver(conn, serde=checkpoint_serializer())
        graph = build_graph(new_adapter, lambda requirements: [fact], new_saver)
        state = graph.get_state(config)
        assert state.values == before.values
        assert isinstance(state.values["requirements"], JobRequirements)
        assert isinstance(state.values["resume"], ResumeSections)
        assert isinstance(state.values["retrieved_facts"][0], Fact)
        assert state.values["retrieved_facts"][0].start_date == date(2025, 1, 1)
        if invalid:
            assert state.values["status"] == WorkflowStatus.FAILED
            assert state.next == ()
            error = state.values["validation_errors"][0]
            assert isinstance(error, ValidationError)
            assert error.code == ErrorCode.UNSUPPORTED_NUMBER
            assert isinstance(state.values["previous_resume"], ResumeSections)
        else:
            assert state.values["status"] == WorkflowStatus.WAITING_APPROVAL
            assert state.next == ("approval",)
            assert state.tasks[0].interrupts[0].value["resume"] == state.values["resume"]
            graph.invoke(Command(resume={"approved": True, "feedback": ""}), config)
            assert graph.get_state(config).values["status"] == WorkflowStatus.READY_TO_APPLY
        assert new_adapter.calls == 0
    assert "Deserializing unregistered type" not in caplog.text
    assert "Blocked deserialization" not in caplog.text
