"""Reads model.json into what an agent needs: a compact index, one node's details, a tour's steps.

Shared by the MCP server (its tools answer from it) and the local Ask box
(its prompt carries the index, so the agent starts with the whole map).
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEVELS = "level: 0 = the system and the outside world, 1 = modules, 2 = functions"


def load() -> dict:
    return json.loads((HERE / "model.json").read_text())


def index(model: dict) -> str:
    """Every node, edge, lens and tour, one line each."""
    nodes = "\n".join(
        f"{n['id']} | {n['kind']} | {n['label']} | {','.join(n.get('tags', []))} | {n['summary']}"
        for n in model["nodes"]
    )
    edges = "\n".join(
        f"{e['from']} > {e['to']}" + (f" ({e['label']})" if e.get("label") else "") for e in model["edges"]
    )
    lenses = "; ".join(f"{l['id']}: {l['q']}" for l in model["lenses"])
    tours = "\n".join(f"{t['id']}: {t['title']} ({len(t['steps'])} steps)" for t in model["tours"])
    return (
        f"NODES (id | kind | label | tags | summary):\n{nodes}\n\n"
        f"EDGES (caller > callee):\n{edges}\n\n"
        f"LENSES: {lenses}\n\nTOURS:\n{tours}"
    )


def node(model: dict, node_id: str) -> dict | None:
    n = next((x for x in model["nodes"] if x["id"] == node_id), None)
    if n is None:
        return None
    keep = ("id", "kind", "label", "module", "summary", "detail", "file", "line", "end", "tags", "io", "tests", "cover", "role")
    out = {k: n[k] for k in keep if n.get(k) not in (None, "", [])}
    out["calls"] = [e["to"] for e in model["edges"] if e["from"] == node_id]
    out["called_by"] = [e["from"] for e in model["edges"] if e["to"] == node_id]
    return out


def tour_step(model: dict, tour_id: str, step: int) -> dict | None:
    t = next((x for x in model["tours"] if x["id"] == tour_id), None)
    if t is None:
        return None
    step = min(max(1, step), len(t["steps"]))
    return {"tour": t["id"], "title": t["title"], "step": step, "steps": len(t["steps"]), "text": t["steps"][step - 1]["text"]}
