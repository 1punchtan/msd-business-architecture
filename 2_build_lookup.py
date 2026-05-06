import re
import json
from pathlib import Path

SITEMAP = Path("map-sitemap.md")
WORKSPACE = Path("workspace")
WORKSPACE.mkdir(exist_ok=True)

BASE = "https://www.workandincome.govt.nz/map/"


def derive_id(url: str, seen_ids: dict) -> str:
    path = url.replace(BASE, "").rstrip("/")
    path = path.removesuffix("index.html").rstrip("/")
    if path.endswith(".html"):
        path = path[:-5]
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "root"
    candidate = segments[-1]
    parent = segments[-2] if len(segments) >= 2 else ""
    if candidate not in seen_ids:
        seen_ids[candidate] = url
        return candidate
    if seen_ids[candidate] == url:
        return candidate
    prefixed = f"{parent}--{candidate}" if parent else candidate
    seen_ids[prefixed] = url
    return prefixed


def parse_all_pages(text: str) -> list[tuple[str, str]]:
    pages = []
    link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("## ", "### ")):
            continue
        if stripped.startswith("- "):
            m = link_re.search(stripped)
            if m:
                pages.append((m.group(1), m.group(2)))
    return pages


if __name__ == "__main__":
    text = SITEMAP.read_text(encoding="utf-8")
    pages = parse_all_pages(text)

    seen_ids: dict[str, str] = {}
    lookup: dict[str, str] = {}

    for name, url in pages:
        entity_id = derive_id(url, seen_ids)
        lookup[name] = entity_id

    (WORKSPACE / "lookup.json").write_text(
        json.dumps(lookup, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Lookup built: {len(lookup)} entries")
