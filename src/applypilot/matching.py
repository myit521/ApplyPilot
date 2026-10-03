"""Deterministic JD-to-fact evidence reports."""

from __future__ import annotations

import re

from .schemas import Fact, JobRequirements


def _contains_term(text: str, term: str) -> bool:
    term = term.strip()
    if not term:
        return False
    if re.search(r"[A-Za-z0-9]", term):
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            text,
            flags=re.IGNORECASE,
        ) is not None
    return term.casefold() in text.casefold()


def _requirement_terms(text: str, keywords: list[str]) -> list[str]:
    return [term for term in keywords if _contains_term(text, term)]


def _fact_matches(fact: Fact, term: str) -> tuple[bool, str]:
    normalized = re.sub(r"\s+", "", term).casefold()
    if any(re.sub(r"\s+", "", skill).casefold() == normalized for skill in fact.skills):
        return True, "skill"
    if _contains_term(fact.content, term):
        return True, "content"
    return False, ""


def _evidence_excerpt(content: str, term: str, limit: int = 500) -> str:
    if re.search(r"[A-Za-z0-9]", term):
        match = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            content,
            flags=re.IGNORECASE,
        )
    else:
        start = content.casefold().find(term.casefold())
        match = None if start < 0 else (start, start + len(term))
    if match is None:
        return content[:limit] + ("…" if len(content) > limit else "")
    if isinstance(match, tuple):
        match_start, match_end = match
    else:
        match_start, match_end = match.span()
    start = max(0, match_start - limit // 2)
    end = min(len(content), start + limit)
    start = max(0, end - limit)
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


def _assess(category: str, text: str, terms: list[str], facts: list[Fact]) -> dict:
    evidence_by_id: dict[str, dict] = {}
    matched_terms: list[str] = []
    for term in terms:
        term_matched = False
        for fact in facts:
            matched, basis = _fact_matches(fact, term)
            if not matched:
                continue
            term_matched = True
            evidence = evidence_by_id.setdefault(
                fact.id,
                {
                    "fact_id": fact.id,
                    "revision": fact.revision,
                    "source_name": fact.source_name,
                    "fact_type": fact.fact_type.value,
                    "excerpt": _evidence_excerpt(fact.content, term),
                    "matched_terms": [],
                    "match_basis": [],
                    "matched_skills": [],
                },
            )
            if term not in evidence["matched_terms"]:
                evidence["matched_terms"].append(term)
            if basis not in evidence["match_basis"]:
                evidence["match_basis"].append(basis)
            if basis == "skill" and term not in evidence["matched_skills"]:
                evidence["matched_skills"].append(term)
        if term_matched:
            matched_terms.append(term)

    if not terms:
        status = "unknown"
        reason = "JD 解析没有为该条目提取可逐项匹配的技术关键词"
    elif not matched_terms:
        status = "no_evidence"
        reason = "已确认事实中没有找到这些关键词"
    elif len(matched_terms) < len(terms):
        status = "partial"
        reason = "已确认事实只覆盖了部分关键词"
    else:
        status = "supported"
        reason = "所有提取出的关键词都在所列事实中找到；仍需人工核对上下文"
    return {
        "category": category,
        "text": text,
        "status": status,
        "reason": reason,
        "keywords": terms,
        "matched_terms": matched_terms,
        "evidence": list(evidence_by_id.values()),
    }


def build_match_report(requirements: JobRequirements, facts: list[Fact]) -> dict:
    """Map parsed requirement clauses to exact keyword evidence in confirmed facts."""
    active_facts = [fact for fact in facts if fact.enabled and fact.status == "confirmed"]
    keywords = list(dict.fromkeys(keyword.term.strip() for keyword in requirements.keywords if keyword.term.strip()))
    entries: list[dict] = []
    covered_terms: set[str] = set()

    clauses = (
        ("required", requirements.required),
        ("preferred", requirements.preferred),
        ("responsibility", requirements.responsibilities),
    )
    for category, texts in clauses:
        for text in texts:
            terms = _requirement_terms(text, keywords)
            covered_terms.update(terms)
            entries.append(_assess(category, text, terms, active_facts))

    for term in keywords:
        if term in covered_terms:
            continue
        entries.append(_assess("keyword", term, [term], active_facts))

    unknowns = [
        {"text": text, "status": "unknown", "reason": "职位描述解析器无法确定此项"}
        for text in requirements.unknowns
    ]
    return {"requirements": entries, "unknowns": unknowns}
