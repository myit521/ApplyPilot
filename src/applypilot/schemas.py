"""核心数据模型。

对应 docs/design.md 第 6 节：
- Fact：个人事实库中的单条事实，是简历生成的唯一合法素材。
- ResumeClaim：简历中的单条表述，必须携带事实引用。
- ValidationError：事实校验节点输出的结构化错误。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class FactType(StrEnum):
    INTERNSHIP = "internship"
    PROJECT = "project"
    SKILL = "skill"
    AWARD = "award"
    EDUCATION = "education"


class EvidenceType(StrEnum):
    COMMIT = "commit"
    DOCUMENT = "document"
    TEST = "test"
    SELF_REPORT = "self_report"


class Fact(BaseModel):
    """一条可独立核验的个人事实。"""

    id: str
    fact_type: FactType
    source_name: str = Field(description="经历来源，如 亚信实习、商城项目")
    content: str = Field(description="不可继续拆分的事实描述")
    start_date: date | None = None
    end_date: date | None = None
    skills: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(
        default_factory=list,
        description="可使用的量化指标原文，如 '6 个批量接口'、'P95 320ms'",
    )
    evidence_type: EvidenceType = EvidenceType.SELF_REPORT
    evidence_ref: str = ""
    enabled: bool = True


class ResumeClaim(BaseModel):
    """简历中的一条生成表述及其事实引用。"""

    text: str
    fact_ids: list[str]
    matched_requirements: list[str] = Field(default_factory=list)


class KeywordImportance(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


class KeywordRequirement(BaseModel):
    """JD 技术关键词及其重要级别。"""

    term: str
    importance: KeywordImportance


class JobRequirements(BaseModel):
    """JD 解析节点的输出结构（docs/design.md 第 8.1 节）。

    模型不得把"加分项"提升为"硬性要求"，因此 required 与
    preferred 是独立字段，不存在互相转换的接口。
    """

    job_title: str = ""
    job_category: str = ""
    location: str = ""
    required: list[str] = Field(default_factory=list, description="必备条件")
    preferred: list[str] = Field(default_factory=list, description="加分条件")
    responsibilities: list[str] = Field(default_factory=list)
    keywords: list[KeywordRequirement] = Field(default_factory=list)
    education_requirement: str = ""
    graduation_time_requirement: str = ""
    experience_requirement: str = ""
    unknowns: list[str] = Field(
        default_factory=list, description="无法从 JD 原文确定的信息"
    )


class ErrorCode(StrEnum):
    MISSING_CITATION = "MISSING_CITATION"
    UNKNOWN_FACT = "UNKNOWN_FACT"
    FACT_DISABLED = "FACT_DISABLED"
    UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"
    UNSUPPORTED_SKILL = "UNSUPPORTED_SKILL"
    SEMANTIC_OVERRUN = "SEMANTIC_OVERRUN"


class ValidationError(BaseModel):
    """单条校验失败，附带错误码和修改建议。"""

    code: ErrorCode
    claim_text: str
    detail: str
    suggestion: str = ""
