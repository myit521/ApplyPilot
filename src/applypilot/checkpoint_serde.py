"""Explicit application types for the locked LangGraph checkpoint serializer."""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def checkpoint_serializer() -> JsonPlusSerializer:
    # String pairs avoid importing workflow.py and opening the whole module.
    # Unknown constructors are blocked; the library may return their raw data.
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=(
            ("applypilot.schemas", "Fact"),
            ("applypilot.schemas", "FactType"),
            ("applypilot.schemas", "EvidenceType"),
            ("applypilot.schemas", "JobRequirements"),
            ("applypilot.schemas", "KeywordRequirement"),
            ("applypilot.schemas", "KeywordImportance"),
            ("applypilot.schemas", "ResumeSections"),
            ("applypilot.schemas", "ResumeClaim"),
            ("applypilot.schemas", "ValidationError"),
            ("applypilot.schemas", "ErrorCode"),
            ("applypilot.workflow", "WorkflowStatus"),
        ),
    )
