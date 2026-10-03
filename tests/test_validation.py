"""事实校验模块测试。

覆盖设计文档第 8.4 节的确定性规则，包括 MVP 验收标准：
人为加入不存在的技能或量化数据时，校验必须拒绝结果。
"""

import pytest

from applypilot.schemas import Fact, FactType, ResumeClaim, ResumeSections
from applypilot.validation import validate_claims, validate_sections

FACTS = [
    Fact(
        status="confirmed",
        id="fact_intern_batch_01",
        fact_type=FactType.INTERNSHIP,
        source_name="亚信实习",
        content="承担运维工具批量执行模块开发，提供批量参数校验、预检、SQL 预览和异步执行能力",
        skills=["Java", "Spring Boot", "MySQL"],
        metrics=["6 个批量接口"],
    ),
    Fact(
        status="confirmed",
        id="fact_intern_batch_02",
        fact_type=FactType.INTERNSHIP,
        source_name="亚信实习",
        content="实现进度查询、结果查询、失败行提取和基于 source_batch_id 的失败数据修正重试",
        skills=["Java", "Redis"],
        metrics=[],
    ),
    Fact(
        status="confirmed",
        id="fact_project_mall_01",
        fact_type=FactType.PROJECT,
        source_name="商城项目",
        content="使用 Testcontainers 搭建分层集成测试基座",
        skills=["JUnit", "Testcontainers", "Docker"],
        metrics=[],
    ),
    Fact(
        status="confirmed",
        id="fact_disabled",
        fact_type=FactType.SKILL,
        source_name="旧技能",
        content="曾经接触过 Hadoop",
        skills=["Hadoop"],
        enabled=False,
    ),
]


def test_valid_claim_passes():
    claims = [
        ResumeClaim(
            text="承担运维工具批量执行模块开发，交付 6 个批量接口，覆盖参数校验、预检和异步执行",
            fact_ids=["fact_intern_batch_01"],
        )
    ]
    assert validate_claims(claims, FACTS) == []


def test_missing_citation_rejected():
    claims = [ResumeClaim(text="独立完成分布式系统设计", fact_ids=[])]
    errors = validate_claims(claims, FACTS)
    assert [e.code for e in errors] == ["MISSING_CITATION"]


def test_unknown_fact_rejected():
    claims = [ResumeClaim(text="做过批量执行", fact_ids=["fact_not_exist"])]
    errors = validate_claims(claims, FACTS)
    assert [e.code for e in errors] == ["UNKNOWN_FACT"]


def test_disabled_fact_rejected():
    claims = [ResumeClaim(text="使用 Hadoop 处理离线数据", fact_ids=["fact_disabled"])]
    errors = validate_claims(claims, FACTS)
    assert "FACT_DISABLED" in [e.code for e in errors]


def test_fabricated_number_rejected():
    """人为加入不存在的量化数据，校验必须拒绝。"""
    claims = [
        ResumeClaim(
            text="承担批量执行模块开发，接口 QPS 提升 300%",
            fact_ids=["fact_intern_batch_01"],
        )
    ]
    errors = validate_claims(claims, FACTS)
    assert "UNSUPPORTED_NUMBER" in [e.code for e in errors]


def test_number_must_match_the_complete_numeric_token():
    fact = Fact(
        status="confirmed", id="sixteen", fact_type=FactType.PROJECT,
        source_name="演示项目", content="交付 16 个接口", metrics=["16 个接口"],
    )
    claims = [ResumeClaim(text="负责 6 个接口", fact_ids=["sixteen"])]
    errors = validate_claims(claims, [fact])
    assert "UNSUPPORTED_NUMBER" in [e.code for e in errors]


def test_number_does_not_match_inside_comma_grouped_value():
    fact = Fact(
        status="confirmed", id="grouped", fact_type=FactType.PROJECT,
        source_name="演示项目", content="处理 6,000 条记录",
    )
    errors = validate_claims(
        [ResumeClaim(text="处理 6 条记录", fact_ids=["grouped"])], [fact]
    )
    assert "UNSUPPORTED_NUMBER" in [e.code for e in errors]


def test_comma_separated_claim_numbers_are_checked_individually():
    fact = Fact(
        status="confirmed", id="seven-only", fact_type=FactType.PROJECT,
        source_name="演示项目", content="负责 7 项改进",
    )
    errors = validate_claims(
        [ResumeClaim(text="负责 6, 7 项改进", fact_ids=["seven-only"])], [fact]
    )
    assert "UNSUPPORTED_NUMBER" in [e.code for e in errors]


def test_supported_comma_separated_claim_numbers_pass():
    fact = Fact(
        status="confirmed", id="six-and-seven", fact_type=FactType.PROJECT,
        source_name="演示项目", content="负责 6, 7 项改进",
    )
    errors = validate_claims(
        [ResumeClaim(text="负责 6, 7 项改进", fact_ids=["six-and-seven"])], [fact]
    )
    assert "UNSUPPORTED_NUMBER" not in [e.code for e in errors]


def test_blank_claim_is_rejected():
    claims = [ResumeClaim(text=" \n ", fact_ids=["fact_intern_batch_01"])]
    errors = validate_claims(claims, FACTS)
    assert "EMPTY_CLAIM" in [e.code for e in errors]


def test_resume_with_no_claims_is_rejected():
    errors = validate_sections(ResumeSections(), FACTS)
    assert "EMPTY_RESUME" in [e.code for e in errors]


def test_fabricated_skill_rejected():
    """人为加入引用事实之外的技能，校验必须拒绝。"""
    claims = [
        ResumeClaim(
            text="使用 Java 和 Testcontainers 完成批量执行模块测试",
            fact_ids=["fact_intern_batch_01"],  # 该事实不含 Testcontainers
        )
    ]
    errors = validate_claims(claims, FACTS)
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


def test_java_does_not_support_javascript_claim():
    facts = [Fact(
        status="confirmed", id="java-only", fact_type=FactType.SKILL,
        source_name="技能", content="掌握 Java", skills=["Java"],
    )]
    errors = validate_claims([ResumeClaim(text="熟悉 JavaScript", fact_ids=["java-only"])], facts)
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


def test_sql_does_not_support_mysql_claim():
    facts = [Fact(
        status="confirmed", id="sql-only", fact_type=FactType.SKILL,
        source_name="技能", content="编写 SQL 查询", skills=["SQL"],
    )]
    errors = validate_claims([ResumeClaim(text="熟悉 MySQL", fact_ids=["sql-only"])], facts)
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


@pytest.mark.parametrize("skill", ["C++", "C#"])
def test_c_does_not_support_punctuated_c_skill(skill):
    facts = [Fact(
        status="confirmed", id="c-only", fact_type=FactType.SKILL,
        source_name="技能", content="掌握 C", skills=["C"],
    )]
    errors = validate_claims(
        [ResumeClaim(text=f"熟悉 {skill}", fact_ids=["c-only"])], facts
    )
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


@pytest.mark.parametrize("skill", ["C++", "C#"])
def test_punctuated_c_skill_does_not_support_generic_c(skill):
    facts = [Fact(
        status="confirmed", id="punctuated-c", fact_type=FactType.SKILL,
        source_name="技能", content=f"掌握 {skill}", skills=[skill],
    )]
    errors = validate_claims(
        [ResumeClaim(text="熟悉 C", fact_ids=["punctuated-c"])], facts
    )
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


def test_exact_cplusplus_skill_is_supported():
    facts = [Fact(
        status="confirmed", id="cplusplus", fact_type=FactType.SKILL,
        source_name="技能", content="掌握 C++", skills=["C++"],
    )]
    assert validate_claims(
        [ResumeClaim(text="熟悉 C++", fact_ids=["cplusplus"])], facts
    ) == []


def test_skill_supported_by_any_cited_fact_passes():
    claims = [
        ResumeClaim(
            text="使用 Java、Redis 实现失败数据修正重试，交付 6 个批量接口",
            fact_ids=["fact_intern_batch_01", "fact_intern_batch_02"],
        )
    ]
    assert validate_claims(claims, FACTS) == []


def test_metric_must_be_verbatim():
    """6 个批量接口 不能被改写为 8 个批量接口。"""
    claims = [
        ResumeClaim(
            text="承担批量执行模块开发，交付 8 个批量接口",
            fact_ids=["fact_intern_batch_01"],
        )
    ]
    errors = validate_claims(claims, FACTS)
    assert "UNSUPPORTED_NUMBER" in [e.code for e in errors]


def test_lexicon_catches_skill_outside_vocabulary():
    """事实库技能标签中没有的技术词（如 Kafka）出现在表述中，
    也必须被拦截——词表漏洞回归测试。"""
    claims = [
        ResumeClaim(
            text="使用 Kafka 实现异步消息解耦",
            fact_ids=["fact_intern_batch_01"],
        )
    ]
    errors = validate_claims(claims, FACTS)
    assert "UNSUPPORTED_SKILL" in [e.code for e in errors]


def test_specific_skill_tag_covers_generic_term():
    """更具体的技能标签（SQL预览）覆盖通用词（SQL）。"""
    facts = [
        Fact(
        status="confirmed",
            id="f_sql",
            fact_type=FactType.INTERNSHIP,
            source_name="亚信实习",
            content="提供 SQL 预览能力",
            skills=["SQL预览"],
        )
    ]
    claims = [ResumeClaim(text="提供 SQL 预览能力", fact_ids=["f_sql"])]
    assert validate_claims(claims, facts) == []
