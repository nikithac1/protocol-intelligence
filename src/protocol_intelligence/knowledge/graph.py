"""Knowledge graph over the registry records: studies linked to interventions, conditions,
sponsors and therapeutic areas.

scripts/build_graph.py writes the graph as openCypher CSV files, the format Neptune Analytics
bulk-loads from S3. The agent never writes Cypher: like query_cohort, it picks one of the fixed
templates below, which answers either from those CSV files in-process (GRAPH_BACKEND=local) or
by sending the template's openCypher to Neptune Analytics (GRAPH_BACKEND=neptune).
"""

from __future__ import annotations

import csv
import inspect
import json
import re
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

from protocol_intelligence.config import DATA_DIR, client, get_settings

GRAPH_DIR = DATA_DIR / "index" / "graph"

# node label -> (record field, edge type from the study)
LINKS = {"Intervention": ("mesh_interventions", "TESTS"),
         "Condition": ("mesh_conditions", "STUDIES"),
         "Sponsor": ("lead_sponsor", "SPONSORED_BY")}
KINDS = {"intervention": "Intervention", "condition": "Condition", "sponsor": "Sponsor"}
LINK_TYPES = [rel for _, rel in LINKS.values()]

# MeSH terms for procedures, formulations and study instruments rather than treatments. As
# nodes they would link unrelated studies (two trials that both "use tablets").
NOT_TREATMENTS = {"biopsy", "standard of care", "tablets", "surveys and questionnaires",
                  "spinal puncture", "magnetic resonance imaging", "electrocardiography",
                  "echocardiography", "injections", "pregnancy", "taxes"}

SCOPE = ("Registry links (MeSH interventions, MeSH conditions, lead sponsor) between the studies "
         "in the knowledge graph only. Studies without MeSH terms have no intervention or "
         "condition links, so a missing link is not proof that two studies are unrelated.")

MAX_HOPS = 6  # study-x-study-y-study-z-study: longer chains stop meaning "related"
_NCT = re.compile(r"^NCT\d{8}$")
_INT_PROPS = {"enrollment"}


# Graph not available
class GraphUnavailable(RuntimeError):
    """The graph has not been built, or Neptune could not be reached."""


# --- build ---------------------------------------------------------------------------------

# Create nodes, edges
def build(records: list[dict]) -> tuple[dict[str, dict[str, dict]], list[tuple[str, str, str]]]:
    """Nodes by label ({node id: properties}) and (start id, edge type, end id) edges."""
    nodes: dict[str, dict[str, dict]] = {label: {} for label in ("Study", "Area", *LINKS)}
    edges = set()
    for r in records:
        sid = f"study:{r['nct_id']}"
        area = r.get("therapeutic_area") or "other"
        nodes["Study"][sid] = {"nct_id": r["nct_id"], "title": r.get("brief_title") or "",
                               "area": area, "phase": "/".join(r.get("phases") or []) or "NA",
                               "status": r.get("overall_status") or "",
                               "enrollment": r.get("enrollment_count") or 0}
        nodes["Area"][f"area:{area}"] = {"name": area}
        edges.add((sid, "IN_AREA", f"area:{area}"))
        for label, (field, rel) in LINKS.items():
            values = r.get(field) or []
            for name in [values] if isinstance(values, str) else values:
                name = name.strip()
                if label == "Intervention":
                    name = name.lower()
                if not name or (label == "Intervention" and name in NOT_TREATMENTS):
                    continue
                nid = f"{label.lower()}:{name.lower()}"
                nodes[label].setdefault(nid, {"name": name})
                edges.add((sid, rel, nid))
    return nodes, sorted(edges)


# Save graph CSVs
def write_csv(nodes: dict[str, dict[str, dict]], edges: list[tuple[str, str, str]],
              out_dir: Path = GRAPH_DIR) -> dict:
    """One openCypher CSV per node label plus edges.csv. Replaces any previous build."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.csv"):
        old.unlink()
    for label, rows in nodes.items():
        if not rows:
            continue
        props = list(next(iter(rows.values())))
        with open(out_dir / f"nodes_{label.lower()}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([":ID", ":LABEL",
                        *(f"{p}:{'Int' if p in _INT_PROPS else 'String'}" for p in props)])
            for nid, p in rows.items():
                w.writerow([nid, label, *(p[k] for k in props)])
    with open(out_dir / "edges.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([":ID", ":START_ID", ":TYPE", ":END_ID"])
        for i, (a, rel, b) in enumerate(edges):
            w.writerow([f"e{i}", a, rel, b])
    _local.cache_clear()
    return {"nodes": {label: len(rows) for label, rows in nodes.items()}, "edges": len(edges)}


# --- backends ------------------------------------------------------------------------------

# In-memory graph
class _Graph:
    """The CSV files held in memory: properties, labels and edges in both directions."""

    # Empty graph
    def __init__(self):
        self.props: dict[str, dict] = {}
        self.label: dict[str, str] = {}
        self.out: dict[str, set] = defaultdict(set)   # node -> {(edge type, end node)}
        self.into: dict[str, set] = defaultdict(set)  # node -> {(edge type, start node)}

    # Node display name
    def name(self, nid: str) -> str:
        p = self.props[nid]
        return p.get("nct_id") or p["name"]

    # Nodes by type
    def by_label(self, label: str) -> list[str]:
        return [nid for nid, lab in self.label.items() if lab == label]

    # Linked studies
    def studies_of(self, nid: str) -> list[str]:
        return [s for _, s in self.into[nid]]

    # Adjacent nodes
    def neighbours(self, nid: str) -> list[str]:
        """Study <-> linked entity, ignoring areas (every study in an area would be 2 hops)."""
        if self.label[nid] == "Study":
            return sorted(x for rel, x in self.out[nid] if rel != "IN_AREA")
        return sorted(self.studies_of(nid))


# Load CSV graph
@lru_cache
def _local(graph_dir: Path) -> _Graph:
    files = sorted(graph_dir.glob("nodes_*.csv"))
    if not files or not (graph_dir / "edges.csv").exists():
        raise GraphUnavailable(f"No graph in {graph_dir}; run scripts/build_graph.py")
    g = _Graph()
    for path in files:
        with open(path, newline="", encoding="utf-8") as f:
            rows = csv.reader(f)
            props = [h.split(":")[0] for h in next(rows)][2:]
            for nid, label, *values in rows:
                g.props[nid] = {p: int(v) if p in _INT_PROPS else v
                                for p, v in zip(props, values, strict=True)}
                g.label[nid] = label
    with open(graph_dir / "edges.csv", newline="", encoding="utf-8") as f:
        rows = csv.reader(f)
        next(rows)
        for _, a, rel, b in rows:
            g.out[a].add((rel, b))
            g.into[b].add((rel, a))
    return g


# Using Neptune?
def _neptune() -> bool:
    return get_settings().graph_backend == "neptune"


# Run Neptune query
def _cypher(query: str, **params) -> list[dict]:
    graph_id = get_settings().neptune_graph_id
    if not graph_id:
        raise GraphUnavailable("NEPTUNE_GRAPH_ID is not set in .env")
    try:
        resp = client("neptune-graph").execute_query(
            graphIdentifier=graph_id, queryString=query, language="OPEN_CYPHER", parameters=params)
    except Exception as e:  # network, permissions, graph deleted: let the agent fall back
        raise GraphUnavailable(f"Neptune query failed: {type(e).__name__}: {e}") from e
    return json.loads(resp["payload"].read())["results"]


# Graph available?
def enabled() -> bool:
    backend = get_settings().graph_backend
    return backend == "neptune" or (backend == "local" and (GRAPH_DIR / "edges.csv").exists())


# --- query templates -----------------------------------------------------------------------
# Each runs as openCypher on Neptune or in Python on the local graph and returns the same rows.
# Sorting and limits are applied afterwards in Python so both backends order rows identically.

# Check NCT ID
def _nct(value: str | None) -> str:
    nct = (value or "").strip().upper()
    if not _NCT.match(nct):
        raise ValueError(f"Invalid NCT id: {value!r}")
    return nct


# Check node kind
def _label(kind: str | None) -> str:
    label = KINDS.get((kind or "").strip().lower())
    if not label:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    return label


# Find related studies
def related_studies(nct_id: str, limit: int = 10) -> list[dict]:
    """Other studies sharing an intervention, condition or sponsor, most overlap first."""
    nct_id = _nct(nct_id)
    if _neptune():
        rows = _cypher(
            "MATCH (s:Study {nct_id: $nct_id})-[r1]->(x)<-[r2]-(o:Study) "
            "WHERE type(r1) = type(r2) AND type(r1) <> 'IN_AREA' AND o <> s "
            "RETURN o.nct_id AS nct_id, o.title AS title, o.area AS area, "
            "collect(labels(x)[0] + ': ' + x.name) AS shared", nct_id=nct_id)
    else:
        g, sid = _local(GRAPH_DIR), f"study:{nct_id}"
        shared = defaultdict(list)
        for rel, x in g.out[sid]:
            if rel == "IN_AREA":
                continue
            for rel2, o in g.into[x]:
                if rel2 == rel and o != sid:
                    shared[o].append(f"{g.label[x]}: {g.props[x]['name']}")
        rows = [{"nct_id": g.props[o]["nct_id"], "title": g.props[o]["title"],
                 "area": g.props[o]["area"], "shared": v} for o, v in shared.items()]
    for r in rows:
        r["shared"] = sorted(r["shared"])
    rows.sort(key=lambda r: (-len(r["shared"]), r["nct_id"]))
    return rows[:limit]


# Shared across areas
def shared_across_areas(kind: str, min_areas: int = 2, limit: int = 10) -> list[dict]:
    """Interventions, conditions or sponsors whose studies span several therapeutic areas."""
    label = _label(kind)
    if _neptune():
        rows = _cypher(f"MATCH (x:{label})<--(s:Study) RETURN x.name AS name, "
                       "collect(DISTINCT s.area) AS areas, collect(DISTINCT s.nct_id) AS studies")
    else:
        g = _local(GRAPH_DIR)
        rows = [{"name": g.props[x]["name"],
                 "areas": list({g.props[s]["area"] for s in g.studies_of(x)}),
                 "studies": [g.props[s]["nct_id"] for s in g.studies_of(x)]}
                for x in g.by_label(label)]
    rows = [{**r, "areas": sorted(r["areas"]), "studies": sorted(r["studies"])}
            for r in rows if len(r["areas"]) >= min_areas]
    rows.sort(key=lambda r: (-len(r["areas"]), -len(r["studies"]), r["name"]))
    return rows[:limit]


# Shared within area
def shared_within_area(therapeutic_area: str, kind: str, limit: int = 10) -> list[dict]:
    """Interventions, conditions or sponsors shared by two or more studies of one area."""
    label, area = _label(kind), (therapeutic_area or "").strip().lower()
    if _neptune():
        rows = _cypher(f"MATCH (x:{label})<--(s:Study {{area: $area}}) "
                       "RETURN x.name AS name, collect(DISTINCT s.nct_id) AS studies", area=area)
    else:
        g = _local(GRAPH_DIR)
        rows = [{"name": g.props[x]["name"],
                 "studies": [g.props[s]["nct_id"] for s in g.studies_of(x)
                             if g.props[s]["area"] == area]} for x in g.by_label(label)]
    rows = [{**r, "studies": sorted(r["studies"])} for r in rows if len(r["studies"]) >= 2]
    rows.sort(key=lambda r: (-len(r["studies"]), r["name"]))
    return rows[:limit]


# Studies for name
def studies_linked_to(kind: str, name: str, limit: int = 10) -> list[dict]:
    """Studies linked to an intervention, condition or sponsor whose name contains `name`."""
    label, text = _label(kind), (name or "").strip()
    if not text:
        raise ValueError("name is required, e.g. \"metformin\"")
    if _neptune():
        rows = _cypher(f"MATCH (x:{label})<--(s:Study) "
                       "WHERE toLower(x.name) CONTAINS toLower($text) "
                       "RETURN x.name AS name, s.nct_id AS nct_id, s.title AS title, "
                       "s.area AS area", text=text)
    else:
        g = _local(GRAPH_DIR)
        rows = [{"name": g.props[x]["name"], "nct_id": g.props[s]["nct_id"],
                 "title": g.props[s]["title"], "area": g.props[s]["area"]}
                for x in g.by_label(label) if text.lower() in g.props[x]["name"].lower()
                for s in g.studies_of(x)]
    rows.sort(key=lambda r: (r["name"], r["nct_id"]))
    return rows[:limit]


# Shortest study path
def connection(nct_id: str, other_nct_id: str) -> list[dict]:
    """Shortest chain of shared links between two studies (up to MAX_HOPS edges), as one row."""
    a, b = _nct(nct_id), _nct(other_nct_id)
    if _neptune():
        rows = _cypher(
            f"MATCH p = (a:Study {{nct_id: $a}})-[:{'|'.join(LINK_TYPES)}*1..{MAX_HOPS}]-"
            "(b:Study {nct_id: $b}) "
            "RETURN [n IN nodes(p) | {type: labels(n)[0], name: coalesce(n.nct_id, n.name)}] "
            "AS path ORDER BY length(p) LIMIT 1", a=a, b=b)
        return [{"path": r["path"], "hops": len(r["path"]) - 1} for r in rows]
    g = _local(GRAPH_DIR)
    start, goal = f"study:{a}", f"study:{b}"
    if start not in g.label or goal not in g.label:
        return []
    prev, depth, queue = {start: None}, {start: 0}, deque([start])
    while queue and goal not in prev:  # breadth-first, so the first path found is a shortest one
        node = queue.popleft()
        if depth[node] == MAX_HOPS:
            continue
        for nxt in g.neighbours(node):
            if nxt not in prev:
                prev[nxt], depth[nxt] = node, depth[node] + 1
                queue.append(nxt)
    if goal not in prev:
        return []
    path, node = [], goal
    while node:
        path.append({"type": g.label[node], "name": g.name(node)})
        node = prev[node]
    return [{"path": path[::-1], "hops": len(path) - 1}]


TEMPLATES = {f.__name__: f for f in (related_studies, shared_across_areas, shared_within_area,
                                     studies_linked_to, connection)}


# Run query template
def run(question_type: str, **args) -> dict:
    """Run one template. Arguments a template does not take are ignored."""
    fn = TEMPLATES.get((question_type or "").strip())
    if not fn:
        raise ValueError(f"question_type must be one of {sorted(TEMPLATES)}")
    params = inspect.signature(fn).parameters
    used = {k: v for k, v in args.items() if k in params and v not in (None, "")}
    missing = [k for k, p in params.items() if p.default is p.empty and k not in used]
    if missing:
        raise ValueError(f"{question_type} needs: {', '.join(missing)}")
    rows = fn(**used)
    out = {"question_type": fn.__name__, "args": used, "results": rows, "scope": SCOPE}
    if not rows:
        out["note"] = ("No links found. Check the NCT id or name, or try search_protocols: the "
                       "graph only holds registry links.")
    return out
