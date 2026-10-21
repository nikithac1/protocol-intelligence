"""Chunks built from the ClinicalTrials.gov registry record itself.

These give every study searchable text even when its PDF is scanned or sparse, and they
cite the public study page rather than a PDF page.
"""

from __future__ import annotations

from protocol_intelligence.processing.chunk import RawChunk, build_chunk_record


def _outcomes(items: list[dict]) -> str:
    return "\n".join(
        f"- {o['measure']}" + (f" (time frame: {o['time_frame']})" if o.get("time_frame") else "")
        + (f": {o['description']}" if o.get("description") else "")
        for o in items
    )


def registry_chunks(rec: dict) -> list[dict]:
    nct = rec["nct_id"]
    header = f"{nct}: {rec['brief_title']}"
    interventions = "\n".join(f"- {i['type']}: {i['name']}" + (f" — {i['description']}" if i.get("description") else "")
                              for i in rec["interventions"])
    arms = "\n".join(f"- {a['label']} ({a['type']})" + (f": {a['description']}" if a.get("description") else "")
                     for a in rec["arm_groups"])
    parts = [
        (["Registry record", "Summary"],
         f"{header}\nOfficial title: {rec['official_title']}\nConditions: {', '.join(rec['conditions'])}\n"
         f"Phase: {', '.join(rec['phases']) or 'NA'}; status: {rec['overall_status']}; "
         f"enrollment: {rec['enrollment_count']} ({rec['enrollment_type']}); sponsor: {rec['lead_sponsor']}\n"
         f"{rec.get('brief_summary') or ''}"),
        (["Registry record", "Arms and interventions"], f"{header}\nArms:\n{arms}\nInterventions:\n{interventions}"),
        (["Registry record", "Eligibility criteria"],
         f"{header}\nSex: {rec['sex']}; ages {rec['minimum_age'] or 'n/a'} to {rec['maximum_age'] or 'n/a'}; "
         f"healthy volunteers: {rec['healthy_volunteers']}\n{rec.get('eligibility_text') or ''}"),
        (["Registry record", "Outcome measures"],
         f"{header}\nPrimary outcomes:\n{_outcomes(rec['primary_outcomes'])}\n"
         f"Secondary outcomes:\n{_outcomes(rec['secondary_outcomes'])}"),
    ]
    out = []
    seq = 0
    for path, text in parts:
        # Long eligibility/outcome text is split at ~3000 chars on line boundaries.
        pieces, buf = [], ""
        for line in text.split("\n"):
            if buf and len(buf) + len(line) > 3000:
                pieces.append(buf)
                buf = f"{header} (continued)\n"
            buf += line + "\n"
        pieces.append(buf)
        for piece in pieces:
            raw = RawChunk(path=path, text=piece.strip(), page_start=0, page_end=0)
            rec_chunk = build_chunk_record(
                nct_id=nct, doc_key="registry", document_title="ClinicalTrials.gov registry record",
                document_version=rec.get("last_update") or "", source_url=rec["source_record_url"],
                source_sha256="", raw=raw, seq=seq, extraction_method="ctgov_api_v2",
                extraction_confidence="high", source_type="registry",
            )
            rec_chunk["page_start"] = rec_chunk["page_end"] = None
            if path[-1] == "Summary":  # keyword classifier would read "Registry record" as ethics
                rec_chunk["section_type"] = "synopsis"
            out.append(rec_chunk)
            seq += 1
    return out
