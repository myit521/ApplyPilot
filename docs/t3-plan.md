# T3 职位录入与历史 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline; user already authorized implementation. Do not commit, push or merge.

**Goal:** Save manually entered jobs independently of models and expose history, details and explicit parsing.
**Architecture:** Extend existing FastAPI jobs routes and request model, retain jobs schema, add an inert-text browser UI.
**Tech Stack:** FastAPI, Pydantic, psycopg, Jinja2, pytest, temporary PostgreSQL testcontainers.
**Spec:** docs/design.md T3; approved scope supplied by parent.

## Global Constraints
No dependencies or migration. Preserve raw_text exactly. Required trimmed title/company <=200, source <=100 default paste; raw_text nonblank <=30000. Optional URL <=2048: http/https, hostname, no credentials, whitespace or controls. Forbid extras and stored NUL. No automatic URL fetch, workflow, edit or delete. Historical empty metadata remains readable.

## Review Focus
Missing model must not prevent save. Failed parsing must retain old parsed JSON. User title must survive parsing. Outer whitespace must persist. User content must render as inert text.

### Task 1: API and validation
**Files:** src/applypilot/api.py; tests/test_t3_jobs.py; tests/fixtures/java_backend_jd.txt; tests/fixtures/ai_application_jd.txt.
**Interfaces:** POST /api/jobs -> full job; GET /api/jobs -> items,total,limit,offset; GET /api/jobs/{id} -> job; POST /api/jobs/{id}/parse -> job.
- [x] Write parameterized validation tests and real PostgreSQL save/history/parse retention tests.
- [x] Run TDD RED: 28 failed, 6 passed before implementation; after implementation, focused suite 34 passed (then URL-control regression added).
- [x] Add strict request validators and context-managed insert/list/detail/parse routes; `INSERT ... RETURNING *`, `ORDER BY id DESC`, `UPDATE jobs SET parsed=%s` only after successful parsing.
- [x] Run focused tests; 34 passed; final full suite includes all latest boundary tests.

### Task 2: Manual page and existing callers
**Files:** src/applypilot/templates/jobs.html; src/applypilot/templates/index.html; src/applypilot/api.py; tests/test_api.py; tests/test_t2_stale_workflow.py; scripts/smoke_e2e.py.
**Interfaces:** /jobs form submits Task 1 API, history paginates and opens detail, explicit parse button.
- [x] Write page rendering and XSS escape tests; verify HTML payloads remain inert.
- [x] Add semantic form labels, history previous/next, detail textContent and loading/errors; homepage link.
- [x] Update existing job callers with metadata and explicit parse before matching.
- [x] Run full `.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q`; 108 passed, 2 existing deprecation warnings; browser workflow also verified.
