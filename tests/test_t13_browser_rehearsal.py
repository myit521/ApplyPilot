"""Synthetic, local-browser rehearsal across the real HTTP server and database."""

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from io import BytesIO

import httpx
import pytest
import uvicorn
from docx import Document
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings
from applypilot.api import create_app

pytestmark = pytest.mark.integration


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_default_adapter_browser_flow_from_profile_to_manual_record(monkeypatch, caplog):
    playwright = pytest.importorskip("playwright.sync_api")
    edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    if not edge.exists():
        pytest.skip("local Edge executable not installed")

    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-browser-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "synthetic-browser-model")
    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
    supplier_steps = []
    fact_id = None

    def synthetic_post(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        assert kwargs["json"]["model"] == "synthetic-browser-model"
        system = kwargs["json"]["messages"][0]["content"]
        if "职位描述解析器" in system:
            step = "parse_jd"
            content = {
                "job_title": "Java 后端", "required": ["Java"],
                "preferred": [], "responsibilities": [],
                "keywords": [{"term": "Java", "importance": "required"}],
                "unknowns": [],
            }
        elif "事实一致性复核员" in system:
            step = "semantic_review"
            content = {"violations": []}
        else:
            step = "generate_resume"
            assert fact_id is not None
            content = {"sections": {
                "education": [], "skills": [], "experience": [{
                    "text": "参与 Java 开发", "fact_ids": [fact_id],
                    "matched_requirements": ["Java"],
                }],
            }}
        supplier_steps.append(step)
        assert len(supplier_steps) <= 6
        payload = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(content),
        }}]}
        if len(supplier_steps) < 6:
            payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "post", synthetic_post)
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(
            create_app(dsn=dsn), host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.started, "local demo server did not start"
            with playwright.sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = browser.new_page(accept_downloads=True)
                    page.goto(base + "/profile")
                    page.locator('#profile-form input[name="name"]').fill("合成候选人")
                    page.locator('#profile-form input[name="email"]').fill("demo@example.test")
                    page.get_by_role("button", name="保存资料草稿").click()
                    page.get_by_role("button", name="确认当前资料").click()
                    page.locator("#profile-status").get_by_text("已确认").wait_for()

                    page.locator("#new-fact").click()
                    fact_box = page.locator("#facts fieldset").first
                    fact_box.locator('input[name="source_name"]').fill("合成项目")
                    fact_box.locator('textarea[name="content"]').fill("参与 Java 开发")
                    fact_box.locator('input[name="skills"]').fill("Java")
                    fact_box.get_by_role("button", name="保存事实草稿").click()
                    page.locator("#facts fieldset").first.get_by_role("button", name="确认当前事实").click()
                    page.locator("#facts fieldset").first.get_by_text("已确认").wait_for()
                    with httpx.Client(base_url=base) as client:
                        facts = client.get("/api/facts").json()
                        assert len(facts) == 1 and facts[0]["status"] == "confirmed"
                        fact_id = facts[0]["id"]

                    page.goto(base + "/jobs")
                    page.locator("#title").fill("Java 后端")
                    page.locator("#company").fill("合成公司")
                    page.locator("#raw_text").fill("招聘 Java 后端，要求 Java。")
                    page.locator("#save-job").click()
                    page.locator("#save-status").get_by_text("职位已保存").wait_for()
                    page.locator("#parse-job").click()
                    page.locator("#detail-parsed").get_by_text("Java").wait_for()
                    page.locator("#match-job").click()
                    page.locator("#match-status").get_by_text("报告已生成").wait_for()
                    assert "合成项目" in page.locator("#match-requirements").inner_text()
                    page.locator("#start-workflow").click()
                    page.locator("#review-link").wait_for(state="visible", timeout=30000)
                    page.locator("#review-link").click()
                    page.locator("#claim-experience-0").wait_for()
                    assert "参与 Java 开发" in page.locator("#claim-experience-0").input_value()
                    run_id = page.url.rsplit("/", 1)[-1]
                    with db.connect(dsn) as conn:
                        job_id = conn.execute("SELECT id FROM jobs").fetchone()["id"]
                    assert supplier_steps == ["parse_jd", "generate_resume", "semantic_review"]
                    page.locator("#feedback").fill("请用简洁的项目表述")
                    page.locator("#reject").click()
                    page.get_by_text("草稿修订 2", exact=False).wait_for()
                    assert page.locator("#claim-experience-0").input_value() == "参与 Java 开发"
                    page.locator("#claim-experience-0").fill("参与 Java 项目开发")
                    page.locator("#save-edit").click()
                    page.get_by_text("草稿修订 3", exact=False).wait_for()
                    assert page.locator("#claim-experience-0").input_value() == "参与 Java 项目开发"
                    assert page.locator(".fact-id").inner_text() == fact_id
                    assert page.locator("#draft-diff del").inner_text() == "参与 Java 开发"
                    assert page.locator("#draft-diff ins").inner_text() == "参与 Java 项目开发"
                    state_response = page.request.get(base + f"/api/workflows/{run_id}")
                    assert state_response.ok
                    draft = state_response.json()
                    assert draft["draft_revision"] == 3 and draft["status"] == "WAITING_APPROVAL"
                    assert draft["sections"]["experience"][0]["fact_ids"] == [fact_id]
                    with db.connect(dsn) as conn:
                        for table in ("workflow_approvals", "resume_versions", "applications"):
                            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
                    expected_steps = ["parse_jd", "generate_resume", "semantic_review",
                                      "generate_resume", "semantic_review", "semantic_review"]
                    assert supplier_steps == expected_steps
                    artifact_dir = os.environ.get("APPLYPILOT_DEMO_ARTIFACT_DIR")
                    if artifact_dir:
                        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(artifact_dir) / "review.png"), full_page=True)
                    page.locator("#approve").click()
                    page.locator("#approval-complete").wait_for(state="visible", timeout=15000)
                    assert supplier_steps == expected_steps
                    docx_url = page.locator("#approval-complete a[href$='/docx']").get_attribute("href")
                    version_id = int(docx_url.split("/")[-2])
                    response = page.request.get(base + docx_url)
                    assert response.ok and len(response.body()) > 500
                    assert response.body().startswith(b"PK\x03\x04")
                    assert "wordprocessingml.document" in response.headers["content-type"]
                    doc = Document(BytesIO(response.body()))
                    document_text = "\n".join(p.text for p in doc.paragraphs)
                    for text in ("合成候选人", "demo@example.test", "参与 Java 项目开发"):
                        assert text in document_text
                    assert supplier_steps == expected_steps
                    with db.connect(dsn) as conn:
                        assert conn.execute("SELECT count(*) AS n FROM resume_versions").fetchone()["n"] == 1
                        approval = conn.execute(
                            "SELECT run_id, version_id, draft_revision FROM workflow_approvals"
                        ).fetchone()
                        assert str(approval["run_id"]) == run_id
                        assert approval["version_id"] == version_id and approval["draft_revision"] == 3
                        version = conn.execute("SELECT id, job_id, status FROM resume_versions").fetchone()
                        assert version["id"] == version_id and version["job_id"] == job_id
                        assert version["status"] == "approved"
                        assert conn.execute("SELECT count(*) AS n FROM applications").fetchone()["n"] == 0
                    page.locator("#approval-complete a[href^='/applications']").click()
                    page.locator("#channel").fill("官网（仅合成记录）")
                    page.locator("#save").click()
                    page.locator("#records").get_by_text("官网（仅合成记录）").wait_for()
                    assert "未知" in page.locator("#records").inner_text()
                    if artifact_dir:
                        page.screenshot(path=str(Path(artifact_dir) / "applications.png"), full_page=True)

                    with db.connect(dsn) as conn:
                        rows = conn.execute(
                            "SELECT a.version_id, a.job_id, a.status, a.source FROM applications a "
                            "JOIN resume_versions v ON v.id=a.version_id "
                            "WHERE v.status='approved'"
                        ).fetchall()
                        assert len(rows) == 1
                        row = rows[0]
                        assert row["version_id"] == version_id and row["job_id"] == job_id
                        assert row["status"] == "unknown"
                        assert row["source"] == "user_reported"
                    assert supplier_steps == expected_steps
                    events = [json.loads(record.getMessage()) for record in caplog.records
                              if record.name == "applypilot.model_usage"]
                    started = [e for e in events if e["event"] == "model_call_started"]
                    finished = [e for e in events if e["event"] == "model_call_finished"]
                    assert len(started) == len(finished) == 6
                    assert len({e["call_id"] for e in started}) == 6
                    assert {e["call_id"] for e in started} == {e["call_id"] for e in finished}
                    assert [e["call_id"] for e in started] == [e["call_id"] for e in finished]
                    event_keys = {"provider", "call_id", "run_id", "job_id", "attempt_count", "step",
                                  "request_started", "event", "usage_status", "prompt_tokens",
                                  "completion_tokens", "total_tokens", "outcome", "duration_ms"}
                    assert all(set(event) <= event_keys for event in events)
                    for stream in (started, finished):
                        assert [e["step"] for e in stream] == [None, *expected_steps[1:]]
                        assert [e["attempt_count"] for e in stream] == [None, 1, 1, None, None, None]
                        assert stream[0]["run_id"] is None and stream[0]["job_id"] is None
                        assert all(e["run_id"] == run_id and e["job_id"] == job_id for e in stream[1:])
                    assert all(e["outcome"] == "success" for e in finished)
                    assert [e["usage_status"] for e in finished] == ["reported"] * 5 + ["missing"]
                    for event in finished[:5]:
                        assert (event["prompt_tokens"], event["completion_tokens"], event["total_tokens"]) == (10, 4, 14)
                    assert all(finished[-1][key] is None for key in
                               ("prompt_tokens", "completion_tokens", "total_tokens"))
                    model_log = json.dumps(events, ensure_ascii=False)
                    for private_text in ("synthetic-browser-key", "合成候选人", "demo@example.test",
                                         "招聘 Java 后端", "参与 Java", "请用简洁", "messages", "content", "Authorization"):
                        assert private_text not in model_log
                    if artifact_dir:
                        safe_keys = ("step", "outcome", "usage_status", "call_id", "run_id", "job_id", "attempt_count")
                        summary = {"counts": {"supplier": len(supplier_steps), "started": len(started),
                                              "finished": len(finished)},
                                   "calls": [{key: event[key] for key in safe_keys} for event in finished]}
                        (Path(artifact_dir) / "observation-summary.json").write_text(
                            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "local demo server did not stop"
