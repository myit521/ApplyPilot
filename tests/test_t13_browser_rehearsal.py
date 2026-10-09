"""Synthetic, local-browser rehearsal across the real HTTP server and database."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings
from applypilot.api import create_app

pytestmark = pytest.mark.integration


class DemoAdapter:
    fact_id = None

    def complete(self, system, user):
        if "职位描述解析器" in system:
            return json.dumps({
                "job_title": "Java 后端", "required": ["Java"],
                "preferred": [], "responsibilities": [],
                "keywords": [{"term": "Java", "importance": "required"}],
                "unknowns": [],
            })
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        return json.dumps({"sections": {
            "education": [], "skills": [], "experience": [{
                "text": "参与 Java 开发", "fact_ids": [self.fact_id],
                "matched_requirements": ["Java"],
            }],
        }})


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_synthetic_browser_flow_from_profile_to_manual_record(monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    if not edge.exists():
        pytest.skip("local Edge executable not installed")

    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        adapter = DemoAdapter()
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(
            create_app(dsn=dsn, adapter=adapter), host="127.0.0.1", port=port,
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
                        adapter.fact_id = facts[0]["id"]

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
                    artifact_dir = os.environ.get("APPLYPILOT_DEMO_ARTIFACT_DIR")
                    if artifact_dir:
                        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(artifact_dir) / "review.png"), full_page=True)
                    page.locator("#approve").click()
                    page.locator("#approval-complete").wait_for(state="visible", timeout=15000)
                    docx_url = page.locator("#approval-complete a[href$='/docx']").get_attribute("href")
                    response = page.request.get(base + docx_url)
                    assert response.ok and len(response.body()) > 500
                    assert response.body().startswith(b"PK\x03\x04")
                    assert "wordprocessingml.document" in response.headers["content-type"]
                    page.locator("#approval-complete a[href^='/applications']").click()
                    page.locator("#channel").fill("官网（仅合成记录）")
                    page.locator("#save").click()
                    page.locator("#records").get_by_text("官网（仅合成记录）").wait_for()
                    assert "未知" in page.locator("#records").inner_text()
                    if artifact_dir:
                        page.screenshot(path=str(Path(artifact_dir) / "applications.png"), full_page=True)

                    with db.connect(dsn) as conn:
                        row = conn.execute(
                            "SELECT a.version_id, a.status, a.source FROM applications a "
                            "JOIN resume_versions v ON v.id=a.version_id "
                            "WHERE v.status='approved'"
                        ).fetchone()
                        assert row["status"] == "unknown"
                        assert row["source"] == "user_reported"
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "local demo server did not stop"
