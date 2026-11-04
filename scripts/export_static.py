"""Export the live dashboard as a static snapshot (HTML + JS + CSS + JSON), for hosting without the API.

    python -m scripts.export_static --out dist/dashboard \\
        --dataset real=postgresql://election:change_me@localhost:5432/elections \\
        --dataset sim=postgresql://election:change_me@localhost:5432/elections_demo

Each dataset becomes data/<name>/{summary,races-*,details-*}.json. The page runs in
snapshot mode: no live stream, with a switch between the exported datasets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from election_data import live_api

OFFICES = ("senate", "house", "governor")
WEB = Path(live_api.__file__).resolve().parent / "web"


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower())


def dump(path: Path, data) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"), default=str)
    path.write_text(text, encoding="utf-8")
    return len(text)


def export_dataset(name: str, url: str, out: Path) -> dict[str, int]:
    os.environ["DATABASE_URL"] = url
    base = out / "data" / name
    sizes = {"summary.json": dump(base / "summary.json", live_api.summary())}
    for office in OFFICES:
        races = live_api.races(office=office)
        sizes[f"races-{office}.json"] = dump(base / f"races-{office}.json", races)
        details = {slug(r["contest_key"]): live_api.race_detail(r["contest_key"]) for r in races["races"]}
        sizes[f"details-{office}.json"] = dump(base / f"details-{office}.json", details)
    return sizes


PAGE = """<title>Midterms 2026 Live</title>
<meta name="claude:viewport-fit-sides" content="cover">
<meta name="dashboard-mode" content="static">
<link rel="stylesheet" href="styles.css">
{body}
"""


def build_page(out: Path) -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    body = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    body = body.replace('src="/static/app.js"', 'src="app.js"')
    (out / "index.html").write_text(PAGE.format(body=body.strip()), encoding="utf-8")
    shutil.copy(WEB / "app.js", out / "app.js")
    shutil.copy(WEB / "styles.css", out / "styles.css")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dataset", action="append", required=True, help="name=database_url (names: sim, real)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    build_page(out)
    for spec in args.dataset:
        name, url = spec.split("=", 1)
        sizes = export_dataset(name, url, out)
        print(name, {k: f"{v / 1024:.0f} KB" for k, v in sizes.items()})


if __name__ == "__main__":
    main()
