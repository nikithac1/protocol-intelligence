"""Documented, reproducible selection of the study corpus.

Policy: interventional studies with at least one publicly posted protocol document,
balanced across therapeutic areas, then rotated across phase and status within each
area so aggregate questions have something to compare.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from protocol_intelligence.ingestion.clinicaltrials_client import ClinicalTrialsClient
from protocol_intelligence.ingestion.normalize import protocol_docs

THERAPEUTIC_AREAS: dict[str, str] = {
    "oncology": "cancer OR neoplasm OR tumor OR lymphoma OR leukemia",
    "diabetes_metabolic": "diabetes OR obesity",
    "cardiovascular": "heart failure OR hypertension OR atrial fibrillation OR coronary artery disease",
    "infectious_disease": "HIV OR COVID-19 OR influenza OR hepatitis OR vaccine",
    "mental_health": "depression OR schizophrenia OR anxiety OR bipolar disorder OR PTSD",
}

STATUSES = ["COMPLETED", "TERMINATED", "ACTIVE_NOT_RECRUITING", "RECRUITING"]
MAX_DOC_BYTES = 25 * 1024 * 1024


@dataclass
class Candidate:
    nct_id: str
    area: str
    phase: str
    status: str
    raw: dict
    reason: str = ""


def _phase_key(raw: dict) -> str:
    phases = raw["protocolSection"].get("designModule", {}).get("phases") or ["NA"]
    return "/".join(phases)


def _usable(raw: dict) -> bool:
    docs = protocol_docs(raw)
    return bool(docs) and any((d.get("size") or 0) <= MAX_DOC_BYTES for d in docs)


def discover(client: ClinicalTrialsClient, per_area_pool: int = 150) -> list[Candidate]:
    """Pull a candidate pool per area (most recently updated first)."""
    pool: list[Candidate] = []
    for area, cond in THERAPEUTIC_AREAS.items():
        params = {
            "query.cond": cond,
            "query.term": "AREA[StudyType]INTERVENTIONAL",
            "aggFilters": "docs:prot",
            "filter.overallStatus": "|".join(STATUSES),
            "sort": "LastUpdatePostDate:desc",
        }
        for raw in client.search(params, max_records=per_area_pool):
            if not _usable(raw):
                continue
            ps = raw["protocolSection"]
            pool.append(Candidate(
                nct_id=ps["identificationModule"]["nctId"], area=area,
                phase=_phase_key(raw), status=ps["statusModule"]["overallStatus"], raw=raw,
            ))
    return pool


def select(pool: list[Candidate], total: int = 100) -> list[Candidate]:
    """Round-robin over (phase, status) buckets within each area for diversity."""
    areas = list(THERAPEUTIC_AREAS)
    quota = {a: total // len(areas) + (1 if i < total % len(areas) else 0)
             for i, a in enumerate(areas)}
    chosen: list[Candidate] = []
    taken: set[str] = set()

    for area in areas:
        buckets: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
        for c in pool:
            if c.area == area and c.nct_id not in taken:
                buckets[(c.phase, c.status)].append(c)
        order = sorted(buckets)  # deterministic
        picked = 0
        while picked < quota[area] and any(buckets.values()):
            for key in order:
                if picked >= quota[area]:
                    break
                if buckets[key]:
                    c = buckets[key].pop(0)
                    if c.nct_id in taken:
                        continue
                    c.reason = (f"area={area}; protocol document posted; "
                                f"diversity bucket phase={c.phase}, status={c.status}")
                    chosen.append(c)
                    taken.add(c.nct_id)
                    picked += 1
    return chosen
