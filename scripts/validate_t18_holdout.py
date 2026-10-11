"""Validate the synthetic T18 holdout before any model request."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HOLDOUT = ROOT / "tests/fixtures/t18_holdout_jds.json"
T11_JDS = ROOT / "tests/fixtures/t11_jds.json"
KINDS = {"title", "location", "required", "preferred", "responsibility",
         "education", "graduation", "experience", "one_of", "optional",
         "unspecified", "non_requirement"}
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def validate(holdout_bytes: bytes, t11_bytes: bytes) -> dict:
    data = json.loads(holdout_bytes)
    if data.get("schema_version") != 1 or "Synthetic JDs" not in data.get("origin", ""):
        raise ValueError("holdout origin or schema is missing")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("T18 requires exactly 20 JDs")
    old_texts = {row["jd_text"] for row in json.loads(t11_bytes)["cases"]}
    ids, focuses, texts = set(), set(), set()
    atoms_count = 0
    for case in cases:
        case_id = case.get("id")
        focus = case.get("focus")
        jd = case.get("jd_text")
        if (not isinstance(case_id, str) or not re.fullmatch(r"h\d\d", case_id)
                or case_id in ids or not isinstance(focus, str) or not focus.strip()
                or focus in focuses or not isinstance(jd, str) or len(jd.strip()) < 20
                or jd in old_texts or jd in texts or EMAIL.search(jd) or MOBILE.search(jd)):
            raise ValueError(f"invalid or repeated holdout JD: {case_id}")
        ids.add(case_id)
        focuses.add(focus)
        texts.add(jd)
        atoms = case.get("atoms")
        if not isinstance(atoms, list) or not atoms:
            raise ValueError(f"missing atoms: {case_id}")
        seen = set()
        for atom in atoms:
            kind, span = atom.get("kind"), atom.get("source_span")
            if (kind not in KINDS or not isinstance(span, str) or not span.strip()
                    or span not in jd or (kind, span) in seen):
                raise ValueError(f"invalid source atom: {case_id} {kind} {span}")
            seen.add((kind, span))
            atoms_count += 1
        if sum(atom["kind"] == "title" for atom in atoms) != 1:
            raise ValueError(f"expected one title atom: {case_id}")
    if ids != {f"h{i:02d}" for i in range(1, 21)}:
        raise ValueError("holdout case IDs are incomplete")
    return {"cases": len(cases), "source_atoms": atoms_count,
            "holdout_sha256": hashlib.sha256(holdout_bytes).hexdigest(),
            "t11_sha256": hashlib.sha256(t11_bytes).hexdigest(),
            "focuses": sorted(focuses)}


if __name__ == "__main__":
    print(json.dumps(validate(HOLDOUT.read_bytes(), T11_JDS.read_bytes()),
                     ensure_ascii=False, indent=2))
