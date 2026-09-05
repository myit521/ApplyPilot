"""混合检索合并与打分测试（docs/design.md 第 8.2 节）。"""

from applypilot.retrieval import RetrievalWeights, hard_filter, merge_and_score
from applypilot.schemas import Fact, FactType


def make_fact(fid: str, skills: list[str], enabled: bool = True) -> Fact:
    return Fact(
        id=fid,
        fact_type=FactType.INTERNSHIP,
        source_name="测试来源",
        content=f"{fid} 的内容",
        skills=skills,
        enabled=enabled,
    )


FACTS = [
    make_fact("f_java", ["Java", "Spring Boot", "MySQL"]),
    make_fact("f_redis", ["Java", "Redis"]),
    make_fact("f_vue", ["Vue"]),
    make_fact("f_disabled", ["Java", "Redis"], enabled=False),
]


def test_hard_filter_excludes_disabled():
    assert [f.id for f in hard_filter(FACTS)] == ["f_java", "f_redis", "f_vue"]


def test_merge_dedupes_across_sources():
    results = merge_and_score(
        FACTS,
        required_skills=["Java"],
        fulltext_hits={"f_java": 0.9, "f_redis": 0.5},
        vector_hits={"f_java": 0.8},
    )
    ids = [r.fact.id for r in results]
    assert ids.count("f_java") == 1
    java = results[ids.index("f_java")]
    assert set(java.sources) == {"fulltext", "vector"}


def test_skill_coverage_drives_ranking():
    results = merge_and_score(
        FACTS,
        required_skills=["Java", "Redis"],
        fulltext_hits={"f_java": 0.6, "f_redis": 0.6},
        vector_hits={"f_java": 0.6, "f_redis": 0.6},
    )
    # f_redis 覆盖 2/2 必备技能，f_java 只覆盖 1/2
    assert results[0].fact.id == "f_redis"
    assert results[0].skill_coverage == 1.0
    assert results[1].skill_coverage == 0.5


def test_disabled_fact_never_returned():
    results = merge_and_score(
        FACTS,
        required_skills=["Java"],
        fulltext_hits={"f_disabled": 1.0},
        vector_hits={"f_disabled": 1.0},
    )
    assert results == []


def test_top_k_truncation():
    facts = [make_fact(f"f{i}", ["Java"]) for i in range(10)]
    hits = {f"f{i}": 0.5 for i in range(10)}
    results = merge_and_score(facts, ["Java"], hits, {}, top_k=5)
    assert len(results) == 5


def test_custom_weights_applied():
    facts = [make_fact("a", []), make_fact("b", [])]
    fts = {"a": 1.0, "b": 0.0}
    vec = {"a": 0.0, "b": 1.0}
    weights = RetrievalWeights(skill=0.0, fulltext=0.1, vector=0.9)
    results = merge_and_score(facts, [], fts, vec, weights=weights)
    assert results[0].fact.id == "b"


def test_score_is_explainable_sum():
    results = merge_and_score(
        FACTS,
        required_skills=["Java", "Spring Boot"],
        fulltext_hits={"f_java": 0.5},
        vector_hits={"f_java": 0.4},
    )
    r = results[0]
    expected = 1.0 * 0.5 + 0.5 * 0.2 + 0.4 * 0.3
    assert abs(r.score - round(expected, 6)) < 1e-6
