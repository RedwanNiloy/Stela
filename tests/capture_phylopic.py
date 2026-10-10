"""Record real PhyloPic API replies for a few taxon names (needs network): python tests/capture_phylopic.py
Writes tests/samples/phylopic.json = {"root": {...}, "nodes": {name: reply}, "node": {href: reply}}."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import geo  # noqa: E402

NAMES = ["Cephalopoda", "Bivalvia", "Gastropoda", "Anthozoa", "Schloenbachia"]  # PD/CC0, CC0, BY-NC-SA, no author, no node


def main():
    c = httpx.Client(headers=geo.UA, timeout=20, follow_redirects=True)
    out = {"root": c.get("https://api.phylopic.org/").json(), "nodes": {}, "node": {}}
    build = out["root"]["build"]
    for name in NAMES:
        nodes = c.get("https://api.phylopic.org/nodes", params={"build": build, "filter_name": name.lower(), "page": 0}).json()
        out["nodes"][name.lower()] = nodes
        items = (nodes.get("_links") or {}).get("items") or []
        for it in items[:1]:
            href = it["href"].split("?")[0]
            d = c.get("https://api.phylopic.org" + href, params={"build": build, "embed_primaryImage": "true"}).json()
            # keep only what the code reads from the (large) node reply
            out["node"][href] = {"_embedded": d.get("_embedded"), "uuid": d.get("uuid")}
    (Path(__file__).parent / "samples" / "phylopic.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print({k: len(v) if isinstance(v, dict) else v for k, v in out.items()})


if __name__ == "__main__":
    main()
