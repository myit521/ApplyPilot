"""DOCX 导出。

对应 docs/design.md 第 3.1 节：批准后的简历版本导出为可编辑
DOCX，结构化 JSON 保留为版本事实源。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document

from .schemas import ResumeClaim


def render_docx(job_title: str, claims: list[ResumeClaim]) -> bytes:
    doc = Document()
    doc.add_heading(f"应聘简历 - {job_title}", level=0)
    doc.add_heading("工作与项目经历", level=1)
    for claim in claims:
        doc.add_paragraph(claim.text, style="List Bullet")

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
