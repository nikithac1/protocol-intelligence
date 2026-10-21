"""Evidence ledger: every passage a tool returns gets a short reference id ([S1], [S2], ...).

The model cites those ids; after the answer we map them back to full citations and flag any
id the model invented. This keeps citations verifiable instead of free-text (plan section 7.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_REF = re.compile(r"\[(S\d+)\]")


def citation_label(p: dict) -> str:
    if p["source_type"] == "registry_query":
        return p["section_path"]
    if p["source_type"] == "registry":
        where = "ClinicalTrials.gov record"
    else:
        pages = (f"p. {p['page_start']}" if p["page_start"] == p["page_end"]
                 else f"pp. {p['page_start']}-{p['page_end']}")
        where = f"{p['document_title']}, {pages}"
    return f"{p['nct_id']}, {where}, section \"{p['section_path']}\""


@dataclass
class EvidenceLedger:
    by_ref: dict[str, dict] = field(default_factory=dict)
    _by_chunk: dict[str, str] = field(default_factory=dict)

    def register(self, passage: dict) -> str:
        """Return the existing ref for this chunk, or assign the next one."""
        cid = passage["chunk_id"]
        if cid not in self._by_chunk:
            ref = f"S{len(self.by_ref) + 1}"
            self._by_chunk[cid] = ref
            self.by_ref[ref] = passage
        return self._by_chunk[cid]

    def resolve(self, answer: str) -> dict:
        """Citations actually used in the answer, in order of first appearance."""
        used, unknown = [], []
        for ref in dict.fromkeys(_REF.findall(answer)):
            (used if ref in self.by_ref else unknown).append(ref)
        return {
            "citations": [{
                "ref": ref, "label": citation_label(p), "nct_id": p["nct_id"],
                "source_type": p["source_type"], "document_title": p["document_title"],
                "section_path": p["section_path"], "page_start": p["page_start"],
                "page_end": p["page_end"], "source_url": p["source_url"], "chunk_id": p["chunk_id"],
                "quote": p["text"][:300],
            } for ref, p in ((r, self.by_ref[r]) for r in used)],
            "unknown_refs": unknown,  # cited ids no tool returned: treat as a grounding failure
        }
