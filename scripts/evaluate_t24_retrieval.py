"""Compare production keyword and hybrid retrieval in an isolated synthetic database."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re

from applypilot import db, facts_repo, search
from applypilot.embeddings import EMBEDDING_DIM, LocalEmbeddingProvider
from applypilot.retrieval import RetrievalWeights, merge_and_score
from applypilot.schemas import Fact, JobRequirements
from scripts.evaluate_t18_live import _save, _source_commit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/t24_retrieval.json"


def validate(data: dict) -> tuple[list[Fact], list[dict]]:
    if data.get("schema_version") != 1 or "Synthetic" not in data.get("origin", ""):
        raise ValueError("Synthetic fixture origin is required")
    facts = [Fact.model_validate(row) for row in data["facts"]]
    cases = data["cases"]
    if (len(facts) != 25 or len({fact.id for fact in facts}) != 25
            or len(cases) != 9 or len({case["id"] for case in cases}) != 9):
        raise ValueError("T24 requires 25 unique facts and nine unique queries")
    eligible = {fact.id for fact in facts if fact.enabled and fact.status == "confirmed"
                and fact.fact_type != "education"}
    for case in cases:
        JobRequirements.model_validate(case["requirements"])
        labels = case["relevant"]
        if len({label["fact_id"] for label in labels}) != len(labels):
            raise ValueError("Duplicate relevance label")
        if not labels and not case.get("reason", "").strip():
            raise ValueError("Empty gold set requires a reason")
        for label in labels:
            if label["fact_id"] not in eligible or not label["reason"].strip():
                raise ValueError("Missing reason or ineligible relevant fact")
    text = json.dumps(data, ensure_ascii=False)
    if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I) or re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        raise ValueError("Contact information is not allowed in this fixture")
    return facts, cases


def metrics(ranked: list[dict], gold: set[str], k: int = 5) -> dict:
    ids = [row["id"] for row in ranked]
    if len(ids) != len(set(ids)):
        raise ValueError("Ranking contains duplicate facts")
    top = ranked[:k]
    hits = len({row["id"] for row in top} & gold)
    lower = upper = hits
    tied = False
    if len(ranked) > k and ranked[k - 1]["score"] == ranked[k]["score"]:
        tied = True
        cutoff = ranked[k - 1]["score"]
        above = [row for row in ranked if row["score"] > cutoff]
        boundary = [row for row in ranked if row["score"] == cutoff]
        slots = k - len(above)
        above_hits = len({row["id"] for row in above} & gold)
        boundary_hits = len({row["id"] for row in boundary} & gold)
        lower = above_hits + max(0, slots - (len(boundary) - boundary_hits))
        upper = above_hits + min(slots, boundary_hits)
    return {"top_ids": ids[:k], "relevant_count": len(gold), "hits": hits,
            "recall_at_5": hits / len(gold) if gold else None,
            "precision_at_5": hits / k,
            "tie_at_boundary": tied,
            "recall_tie_lower": lower / len(gold) if gold else None,
            "recall_tie_upper": upper / len(gold) if gold else None,
            "ranking": ranked}


def _rank(facts, required, fts, vec, weights=None):
    scored = merge_and_score(facts, required, fts, vec, weights=weights, top_k=len(facts))
    return [{"id": row.fact.id, "score": row.score,
             "skill_coverage": row.skill_coverage, "fulltext": row.fulltext_score,
             "vector": row.vector_score} for row in scored]


def run(model_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if not model_path.is_dir():
        raise ValueError("An existing local model snapshot is required")
    fixture_bytes = FIXTURE.read_bytes()
    facts, cases = validate(json.loads(fixture_bytes))
    report = {"run": {"at_utc": datetime.now(timezone.utc).isoformat(),
                      "source_commit": _source_commit(), "status": "running",
                      "synthetic_only": True, "model_network_disabled": True,
                      "top_k": 5, "candidate_limit": 20,
                      "weights": RetrievalWeights().model_dump(),
                      "vector_only_weights": {"skill": 0, "fulltext": 0, "vector": 1},
                      "eligible_fact_count": sum(fact.enabled and fact.status == "confirmed" and fact.fact_type != "education" for fact in facts)},
              "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
              "embedding": {"model": "BAAI/bge-small-zh-v1.5", "snapshot": model_path.name,
                            "sentence_transformers": version("sentence-transformers"),
                            "files_sha256": {}}, "cases": [],
              "limits": ["Authored synthetic relevance labels are not real recruitment evidence.",
                         "Manually parsed queries isolate retrieval from JD parsing quality.",
                         "Relevance does not prove all job requirements are met.",
                         "Existing ranking ties are reported with possible recall bounds."]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    try:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        for path in sorted(model_path.rglob("*")):
            if path.is_file() and (path.suffix in (".json", ".bin", ".safetensors") or path.name == "vocab.txt"):
                report["embedding"]["files_sha256"][path.relative_to(model_path).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        provider = LocalEmbeddingProvider(str(model_path))
        probe = provider.embed("合成检索探针")
        if probe is None or len(probe) != EMBEDDING_DIM:
            raise RuntimeError("A real 512-dimensional embedding is required")
        report["embedding"]["device"] = str(provider._model.device)
        _save(output, report)
        print("Real local model loaded", flush=True)
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("pgvector/pgvector:pg16") as pg:
            dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
            with db.connect(dsn) as conn:
                db.init_schema(conn)
                for fact in facts:
                    facts_repo.create_fact(conn, fact)
                search.backfill_embeddings(conn, provider)
                print("Synthetic facts embedded in isolated database", flush=True)
                eligible = [fact for fact in facts if fact.enabled and fact.status == "confirmed"
                            and fact.fact_type != "education"]
                eligible_ids = {fact.id for fact in eligible}
                for case in cases:
                    req = JobRequirements.model_validate(case["requirements"])
                    terms, required = search.extract_terms(req), search.required_skill_terms(req)
                    fts = search.fulltext_hits(conn, terms, exclude_education=True)
                    vec = search.vector_hits(conn, provider.embed(search._query_text(req)), exclude_education=True)
                    if not (set(fts) | set(vec)) <= eligible_ids:
                        raise RuntimeError("Ineligible fact entered retrieval")
                    gold = {label["fact_id"] for label in case["relevant"]}
                    row = {"id": case["id"], "terms": terms, "required_skill_terms": required,
                           "query_text": search._query_text(req), "gold_ids": sorted(gold),
                           "keyword_candidate_count": len(fts), "vector_candidate_count": len(vec),
                           "keyword": metrics(_rank(eligible, required, fts, {}), gold),
                           "vector_only": metrics(_rank(eligible, [], {}, vec, RetrievalWeights(skill=0, fulltext=0, vector=1)), gold),
                           "hybrid": metrics(_rank(eligible, required, fts, vec), gold)}
                    report["cases"].append(row)
                    _save(output, report)
                    print(f"{case['id']}: keyword={row['keyword']['recall_at_5']}; hybrid={row['hybrid']['recall_at_5']}", flush=True)
        report["summary"] = {}
        for mode in ("keyword", "vector_only", "hybrid"):
            labeled = [row[mode] for row in report["cases"] if row[mode]["recall_at_5"] is not None]
            report["summary"][mode] = {"queries_with_gold": len(labeled),
                                       "macro_recall_at_5": sum(row["recall_at_5"] for row in labeled) / len(labeled),
                                       "macro_tie_lower": sum(row["recall_tie_lower"] for row in labeled) / len(labeled),
                                       "macro_tie_upper": sum(row["recall_tie_upper"] for row in labeled) / len(labeled)}
        report["run"]["status"] = "complete"
    except Exception as error:
        report["run"]["status"] = "failed"
        report["run"]["error_type"] = type(error).__name__
    _save(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.model_path, args.output)
    print(f"status={report['run']['status']}; cases={len(report['cases'])}")
    if report["run"]["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
