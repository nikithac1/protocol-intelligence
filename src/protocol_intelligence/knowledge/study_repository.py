"""SQLite study store for deterministic metadata questions.

The agent never writes SQL: query_cohort() accepts only allowlisted filters and group-bys
and builds parameterized SQL itself (plan section 7.1, query_study_cohort guardrail).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from protocol_intelligence.config import DATA_DIR

DB_PATH = DATA_DIR / "index" / "studies.db"

SCHEMA = """
CREATE TABLE studies (
  nct_id TEXT PRIMARY KEY, brief_title TEXT, official_title TEXT, overall_status TEXT,
  phase TEXT, study_type TEXT, allocation TEXT, intervention_model TEXT, primary_purpose TEXT,
  masking TEXT, enrollment_count INTEGER, enrollment_type TEXT, sex TEXT,
  minimum_age_years REAL, maximum_age_years REAL, healthy_volunteers INTEGER,
  study_start TEXT, start_year INTEGER, primary_completion TEXT, completion TEXT,
  last_update TEXT, lead_sponsor TEXT, lead_sponsor_class TEXT, has_results INTEGER,
  therapeutic_area TEXT, brief_summary TEXT, source_record_url TEXT, protocol_doc_count INTEGER,
  retrieved_at TEXT, source_api_version TEXT
);
CREATE TABLE study_phases (nct_id TEXT, phase TEXT);
CREATE TABLE conditions (nct_id TEXT, condition TEXT, source TEXT);
CREATE TABLE interventions (nct_id TEXT, type TEXT, name TEXT, source TEXT);
CREATE TABLE outcomes (nct_id TEXT, kind TEXT, measure TEXT, time_frame TEXT, description TEXT);
CREATE INDEX ix_phase ON study_phases(phase);
CREATE INDEX ix_cond ON conditions(condition);
CREATE INDEX ix_int ON interventions(name);
"""

_AGE = re.compile(r"(\d+(?:\.\d+)?)\s*(year|month|week|day|hour|minute)", re.I)
_UNIT_YEARS = {"year": 1, "month": 1 / 12, "week": 1 / 52, "day": 1 / 365, "hour": 1 / 8760,
               "minute": 1 / 525600}


def age_years(text: str | None) -> float | None:
    m = _AGE.search(text or "")
    return round(float(m.group(1)) * _UNIT_YEARS[m.group(2).lower()], 3) if m else None


def build(studies_jsonl: Path, db_path: Path = DB_PATH) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    n = 0
    for line in studies_jsonl.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        nct = r["nct_id"]
        con.execute(
            "INSERT INTO studies VALUES (" + ",".join("?" * 30) + ")",
            (nct, r["brief_title"], r["official_title"], r["overall_status"],
             "/".join(r["phases"]) or "NA", r["study_type"], r["allocation"],
             r["intervention_model"], r["primary_purpose"], r["masking"], r["enrollment_count"],
             r["enrollment_type"], r["sex"], age_years(r["minimum_age"]), age_years(r["maximum_age"]),
             int(bool(r["healthy_volunteers"])), r["study_start"],
             int(r["study_start"][:4]) if r.get("study_start") else None,
             r["primary_completion"], r["completion"], r["last_update"], r["lead_sponsor"],
             r["lead_sponsor_class"], int(bool(r["has_results"])), r.get("therapeutic_area"),
             r.get("brief_summary"), r["source_record_url"], len(r["protocol_documents"]),
             r["retrieved_at"], r["source_api_version"]))
        con.executemany("INSERT INTO study_phases VALUES (?,?)",
                        [(nct, p) for p in (r["phases"] or ["NA"])])
        con.executemany("INSERT INTO conditions VALUES (?,?,?)",
                        [(nct, c, "registry") for c in r["conditions"]]
                        + [(nct, c, "mesh") for c in r["mesh_conditions"] if c])
        con.executemany("INSERT INTO interventions VALUES (?,?,?,?)",
                        [(nct, i["type"], i["name"], "registry") for i in r["interventions"]]
                        + [(nct, None, m, "mesh") for m in r["mesh_interventions"] if m])
        con.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?)",
                        [(nct, "primary", o["measure"], o["time_frame"], o["description"])
                         for o in r["primary_outcomes"]]
                        + [(nct, "secondary", o["measure"], o["time_frame"], o["description"])
                           for o in r["secondary_outcomes"]])
        n += 1
    con.commit()
    con.close()
    return n


# ---------------------------------------------------------------- query side

GROUP_BY = {  # public name -> SQL expression
    "phase": "sp.phase", "overall_status": "s.overall_status",
    "therapeutic_area": "s.therapeutic_area", "lead_sponsor_class": "s.lead_sponsor_class",
    "allocation": "s.allocation", "primary_purpose": "s.primary_purpose",
    "start_year": "s.start_year", "sex": "s.sex", "has_results": "s.has_results",
    "intervention_type": "i.type",
}
LIST_COLUMNS = ["nct_id", "brief_title", "overall_status", "phase", "therapeutic_area",
                "enrollment_count", "lead_sponsor", "study_start", "source_record_url"]


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # read-only for queries
    con.row_factory = sqlite3.Row
    return con


def _where(filters: dict) -> tuple[list[str], list]:
    clauses, params = [], []

    def any_in(col: str, values) -> None:
        values = [values] if isinstance(values, str | int) else list(values)
        clauses.append(f"{col} IN ({','.join('?' * len(values))})")
        params.extend(values)

    for key, val in filters.items():
        if val in (None, "", []):
            continue
        if key == "phase":  # matches multi-phase studies too (PHASE2/PHASE3 counts as PHASE3)
            vals = [val] if isinstance(val, str) else val
            clauses.append("s.nct_id IN (SELECT nct_id FROM study_phases WHERE phase IN "
                           f"({','.join('?' * len(vals))}))")
            params.extend(v.upper().replace(" ", "") for v in vals)
        elif key == "overall_status":
            any_in("s.overall_status", [v.upper() for v in ([val] if isinstance(val, str) else val)])
        elif key in ("therapeutic_area", "lead_sponsor_class", "allocation", "primary_purpose", "sex"):
            any_in(f"s.{key}", val)
        elif key == "nct_ids":
            any_in("s.nct_id", val)
        elif key == "condition":
            clauses.append("s.nct_id IN (SELECT nct_id FROM conditions WHERE condition LIKE ?)")
            params.append(f"%{val}%")
        elif key == "intervention":
            clauses.append("s.nct_id IN (SELECT nct_id FROM interventions WHERE name LIKE ?)")
            params.append(f"%{val}%")
        elif key == "intervention_type":
            clauses.append("s.nct_id IN (SELECT nct_id FROM interventions WHERE type = ?)")
            params.append(str(val).upper())
        elif key == "min_enrollment":
            clauses.append("s.enrollment_count >= ?"); params.append(int(val))
        elif key == "max_enrollment":
            clauses.append("s.enrollment_count <= ?"); params.append(int(val))
        elif key == "start_year_from":
            clauses.append("s.start_year >= ?"); params.append(int(val))
        elif key == "start_year_to":
            clauses.append("s.start_year <= ?"); params.append(int(val))
        elif key == "accepts_age":  # study's age range includes this age (years)
            clauses.append("(s.minimum_age_years IS NULL OR s.minimum_age_years <= ?) AND "
                           "(s.maximum_age_years IS NULL OR s.maximum_age_years >= ?)")
            params.extend([float(val), float(val)])
        elif key == "has_results":
            clauses.append("s.has_results = ?"); params.append(int(bool(val)))
        elif key == "healthy_volunteers":
            clauses.append("s.healthy_volunteers = ?"); params.append(int(bool(val)))
        else:
            raise ValueError(f"Unsupported filter '{key}'. Allowed: phase, overall_status, "
                             "therapeutic_area, condition, intervention, intervention_type, "
                             "lead_sponsor_class, allocation, primary_purpose, sex, nct_ids, "
                             "min_enrollment, max_enrollment, start_year_from, start_year_to, "
                             "accepts_age, has_results, healthy_volunteers")
    return clauses, params


def query_cohort(filters: dict | None = None, group_by: str | None = None,
                 limit: int = 100, db_path: Path = DB_PATH) -> dict:
    """Count/list/aggregate studies. Returns rows plus the exact filters applied."""
    filters = filters or {}
    clauses, params = _where(filters)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(db_path) as con:
        if group_by:
            if group_by not in GROUP_BY:
                raise ValueError(f"Unsupported group_by '{group_by}'. Allowed: {sorted(GROUP_BY)}")
            join = {"phase": "JOIN study_phases sp ON sp.nct_id = s.nct_id",
                    "intervention_type": "JOIN interventions i ON i.nct_id = s.nct_id "
                                         "AND i.source = 'registry'"}.get(group_by, "")
            sql = (f"SELECT {GROUP_BY[group_by]} AS {group_by}, COUNT(DISTINCT s.nct_id) AS studies, "
                   f"SUM(s.enrollment_count) AS total_enrollment, "
                   f"ROUND(AVG(s.enrollment_count), 1) AS mean_enrollment "
                   f"FROM studies s {join} {where} GROUP BY 1 ORDER BY studies DESC")
            rows = [dict(r) for r in con.execute(sql, params)]
            total = con.execute(f"SELECT COUNT(*) FROM studies s {where}", params).fetchone()[0]
            result = {"filters": filters, "group_by": group_by, "total_studies": total, "rows": rows}
            if group_by in ("phase", "intervention_type"):
                result["note"] = (f"A study with several {group_by} values is counted once under "
                                  "each, so group counts can sum to more than total_studies.")
            return result
        total = con.execute(f"SELECT COUNT(*) FROM studies s {where}", params).fetchone()[0]
        rows = [dict(r) for r in con.execute(
            f"SELECT {', '.join('s.' + c for c in LIST_COLUMNS)} FROM studies s {where} "
            f"ORDER BY s.nct_id LIMIT ?", [*params, int(limit)])]
        return {"filters": filters, "total_studies": total, "rows": rows,
                "truncated": total > len(rows)}


def get_studies(nct_ids: list[str], db_path: Path = DB_PATH) -> list[dict]:
    """Full canonical metadata for specific studies (get_study_metadata tool)."""
    with _connect(db_path) as con:
        out = []
        for nct in nct_ids:
            row = con.execute("SELECT * FROM studies WHERE nct_id = ?", [nct.upper()]).fetchone()
            if not row:
                out.append({"nct_id": nct, "error": "not in indexed corpus"})
                continue
            rec = dict(row)
            rec["conditions"] = [r[0] for r in con.execute(
                "SELECT condition FROM conditions WHERE nct_id=? AND source='registry'", [nct])]
            rec["interventions"] = [dict(r) for r in con.execute(
                "SELECT type, name FROM interventions WHERE nct_id=? AND source='registry'", [nct])]
            rec["primary_outcomes"] = [dict(r) for r in con.execute(
                "SELECT measure, time_frame FROM outcomes WHERE nct_id=? AND kind='primary'", [nct])]
            rec["secondary_outcomes"] = [dict(r) for r in con.execute(
                "SELECT measure, time_frame FROM outcomes WHERE nct_id=? AND kind='secondary'", [nct])]
            out.append(rec)
        return out
