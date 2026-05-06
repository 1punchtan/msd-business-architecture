import re
import json
from pathlib import Path

SITEMAP = Path("map-sitemap.md")
WORKSPACE = Path("workspace")
WORKSPACE.mkdir(exist_ok=True)

EXCLUDED_SECTIONS = {"deskfile", "legislation"}

QUEUE_ORDER = [
    "income-support-core-policy",
    "income-support-extra-help",
    "income-support-main-benefits",
    "employment-and-training",
    "social-housing",
    "students",
    "card-services",
    "to-or-from-overseas",
    "youth-service",
]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_sitemap(text: str) -> list[dict]:
    entries: list[dict] = []
    current_h2 = ""
    current_h3 = ""
    excluded = False

    link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("## "):
            m = link_re.search(stripped)
            if m:
                current_h2 = slugify(m.group(1))
                current_h3 = ""
                excluded = current_h2 in EXCLUDED_SECTIONS
            continue

        if stripped.startswith("### "):
            m = link_re.search(stripped)
            if m:
                current_h3 = slugify(m.group(1))
            continue

        if excluded:
            continue

        if stripped.startswith("- "):
            m = link_re.search(stripped)
            if m:
                name, url = m.group(1), m.group(2)
                section = (
                    f"{current_h2}-{current_h3}" if current_h3 else current_h2
                )
                entries.append({"url": url, "name": name, "section": section})

    return entries


def build_queue(entries: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {s: [] for s in QUEUE_ORDER}
    for e in entries:
        if e["section"] in buckets:
            buckets[e["section"]].append(e)
    return [e for section in QUEUE_ORDER for e in buckets[section]]


if __name__ == "__main__":
    text = SITEMAP.read_text(encoding="utf-8")
    entries = parse_sitemap(text)
    queue = build_queue(entries)
    (WORKSPACE / "queue.json").write_text(
        json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Queue built: {len(queue)} pages")
