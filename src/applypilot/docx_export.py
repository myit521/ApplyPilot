"""DOCX 导出。

对应 docs/design.md 第 8.3、3.1 节：批准后的分区结构简历版本
按模板渲染为可编辑 DOCX，结构化 JSON 保留为版本事实源。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.shared import Cm, Pt

from .schemas import ResumeSections


def render_docx(job_title: str, sections: ResumeSections,
                profile: dict | None = None) -> bytes:
    doc = Document()
    page = doc.sections[0]
    page.top_margin = page.bottom_margin = Cm(1.8)
    page.left_margin = page.right_margin = Cm(2)
    doc.styles["Normal"].font.size = Pt(10.5)
    if profile:
        doc.add_heading(profile["name"], level=0)
        contact = [f"{label}：{profile[key]}" for key, label in (
            ("email", "邮箱"), ("phone", "电话"),
            ("location", "所在地"), ("website", "主页"),
        ) if profile.get(key)]
        if contact:
            doc.add_paragraph("  ·  ".join(contact))
        doc.add_paragraph(f"应聘岗位：{job_title}")
    else:
        doc.add_heading(f"应聘简历 - {job_title}", level=0)

    if sections.education:
        doc.add_heading("教育背景", level=1)
        for claim in sections.education:
            doc.add_paragraph(claim.text, style="List Bullet")

    if sections.skills:
        doc.add_heading("专业技能", level=1)
        for claim in sections.skills:
            doc.add_paragraph(claim.text, style="List Bullet")

    if sections.experience:
        doc.add_heading("工作与项目经历", level=1)
        for claim in sections.experience:
            doc.add_paragraph(claim.text, style="List Bullet")

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
