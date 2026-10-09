"""DOCX export uses the frozen approval content, including contact details."""

from io import BytesIO
import json

import pytest
from docx import Document
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.api import create_app
from applypilot.docx_export import render_docx
from applypilot.schemas import ResumeClaim, ResumeSections


def _paragraphs(data: bytes) -> str:
    return "\n".join(paragraph.text for paragraph in Document(BytesIO(data)).paragraphs)


def test_render_docx_includes_confirmed_profile_and_sections():
    sections = ResumeSections(
        education=[ResumeClaim(text="某大学 · 计算机科学", fact_ids=["edu-1"])],
        skills=[ResumeClaim(text="Java、Spring Boot", fact_ids=["skill-1"])],
        experience=[ResumeClaim(text="完成批量接口开发", fact_ids=["project-1"])],
    )
    data = render_docx("Java 后端", sections, profile={
        "name": "测试候选人", "email": "candidate@example.test", "phone": "13800000000",
        "location": "上海", "website": "https://example.test/profile",
    })
    text = _paragraphs(data)
    for expected in ("测试候选人", "candidate@example.test", "13800000000", "上海",
                     "https://example.test/profile", "应聘岗位：Java 后端", "教育背景",
                     "专业技能", "工作与项目经历", "完成批量接口开发"):
        assert expected in text


@pytest.mark.integration
def test_export_uses_frozen_profile_and_rejects_draft():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
            job_id = conn.execute(
                "INSERT INTO jobs (title, raw_text) VALUES ('当前职位名', 'JD') RETURNING id"
            ).fetchone()["id"]
            frozen = {
                "job_snapshot": {"title": "冻结职位名"},
                "profile_snapshot": {"revision": 2, "data": {
                    "name": "批准时姓名", "email": "frozen@example.test", "phone": "",
                    "location": "北京", "website": "",
                }},
                "sections": {"education": [], "skills": [], "experience": [
                    {"text": "冻结的项目经历", "fact_ids": [], "matched_requirements": []},
                ]},
            }
            approved_id = conn.execute(
                "INSERT INTO resume_versions (job_id, content, status) "
                "VALUES (%s, %s::jsonb, 'approved') RETURNING id",
                (job_id, json.dumps(frozen, ensure_ascii=False)),
            ).fetchone()["id"]
            draft_id = conn.execute(
                "INSERT INTO resume_versions (job_id, content, status) "
                "VALUES (%s, %s::jsonb, 'draft') RETURNING id",
                (job_id, json.dumps(frozen, ensure_ascii=False)),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO profile (id, revision, status, data) "
                "VALUES (1, 3, 'confirmed', %s::jsonb)",
                ('{"name":"当前姓名","email":"live@example.test"}',),
            )
        with TestClient(create_app(dsn=dsn, checkpointer=object())) as client:
            response = client.get(f"/api/resume-versions/{approved_id}/docx")
            assert response.status_code == 200, response.text
            text = _paragraphs(response.content)
            assert "批准时姓名" in text and "frozen@example.test" in text
            assert "冻结职位名" in text and "冻结的项目经历" in text
            assert "当前姓名" not in text and "live@example.test" not in text
            assert "当前职位名" not in text
            assert client.get(f"/api/resume-versions/{draft_id}/docx").status_code == 404
