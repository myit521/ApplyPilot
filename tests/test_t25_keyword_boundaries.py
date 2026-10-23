import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db, facts_repo, search
from applypilot.schemas import Fact


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
            yield conn


@pytest.fixture
def conn(database):
    database.execute("TRUNCATE facts CASCADE")
    return database


def add(conn, fact_id, content="合成经历", source="合成来源", skills=None):
    facts_repo.create_fact(conn, Fact(id=fact_id, status="confirmed", fact_type="project",
                                     source_name=source, content=content, skills=skills or []))


@pytest.mark.parametrize("term,longer", [("Java", "JavaScript"), ("SQL", "MySQL"),
                                        ("SQL", "PostgreSQL")])
def test_body_and_source_do_not_match_a_longer_technology(conn, term, longer):
    add(conn, "exact", content=f"使用{term}开发")
    add(conn, "longer-body", content=f"使用 {longer} 开发")
    add(conn, "longer-source", source=f"{longer} 合成项目")
    assert search.fulltext_hits(conn, [term]) == {"exact": 1.0}


def test_sql_skill_tag_does_not_match_postgresql_query(conn):
    add(conn, "sql-tag", skills=["SQL"])
    add(conn, "pg-tag", skills=[" postgresql "])
    assert search.fulltext_hits(conn, ["PostgreSQL"]) == {"pg-tag": 1.0}


def test_java_skill_tag_does_not_match_javascript_query(conn):
    add(conn, "java-tag", skills=["Java"])
    add(conn, "js-tag", skills=["JavaScript"])
    assert search.fulltext_hits(conn, ["JavaScript"]) == {"js-tag": 1.0}


@pytest.mark.parametrize("term,other", [("C++", "C+"), ("cache_key", "cacheXkey"),
                                      ("100%", "100X"), ("[x]", "x"),
                                      (r"a\b", "ab")])
def test_regex_and_like_metacharacters_are_literal(conn, term, other):
    add(conn, "literal", content=f"使用 {term} 完成演示")
    add(conn, "other", content=f"使用 {other} 完成演示")
    assert search.fulltext_hits(conn, [term]) == {"literal": 1.0}


def test_case_chinese_and_skill_coverage_remain_supported(conn):
    add(conn, "both", content="完成接口联调", skills=["JAVA"])
    add(conn, "chinese", content="参与接口联调过程")
    assert search.fulltext_hits(conn, ["Java", "接口联调"]) == {"both": 1.0, "chinese": 0.5}
