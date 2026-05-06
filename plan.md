# MSD MAP Business Architecture — Implementation Plan

## Overview

Four Python scripts run in sequence against a shared `/workspace` directory. The pipeline reads `map-sitemap.md`, builds an ordered queue and name→ID lookup, scrapes each URL with headless Chromium, makes two Claude API calls per page (Infer + Format), and writes typed JSON entities to `output/`. A final report script surfaces anything needing human review.

---

## Directory Setup

```
msd-business-architecture/
├── map-sitemap.md          # read-only input
├── 1_build_queue.py
├── 2_build_lookup.py
├── 3_pipeline.py
├── 4_report.py
├── 5_to_archimate.py
├── workspace/
│   ├── queue.json
│   ├── lookup.json
│   ├── run_log.json
│   └── msd-map.xml         # ArchiMate Open Exchange output
└── output/
    ├── products/
    ├── programmes/
    ├── policies/
    ├── processes/
    ├── social_housing/
    ├── actors/
    ├── cards/
    └── concepts/
```

Create these at the start of any script that writes to them:

```python
from pathlib import Path

WORKSPACE = Path("workspace")
OUTPUT = Path("output")
WORKSPACE.mkdir(exist_ok=True)
for t in ["products", "programmes", "policies", "processes",
          "social_housing", "actors", "cards", "concepts"]:
    (OUTPUT / t).mkdir(parents=True, exist_ok=True)
```

---

## Shared Utility: ID Derivation

Used by both `2_build_lookup.py` and `3_pipeline.py`. Extract into a shared module `utils.py` or inline into each script.

### Rules
1. Strip `https://www.workandincome.govt.nz/map/`
2. Drop trailing `index.html`
3. Take the final non-empty path segment
4. If two URLs share the same final segment, prefix with the parent segment using `--`

```python
from urllib.parse import urlparse

BASE = "https://www.workandincome.govt.nz/map/"

def derive_id(url: str, seen_ids: dict[str, str]) -> str:
    """
    Derive a collision-aware ID from a MAP URL.
    seen_ids maps id_candidate -> first url that claimed it.
    Mutates seen_ids in place.
    Returns the final ID string.
    """
    path = url.replace(BASE, "").rstrip("/")
    path = path.removesuffix("index.html").rstrip("/")

    # Strip .html extension; the filename itself is the ID segment
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
        return candidate  # same URL seen twice, idempotent

    # Collision: prefix with parent segment
    prefixed = f"{parent}--{candidate}" if parent else candidate
    seen_ids[prefixed] = url
    return prefixed
```

### Examples

| URL | ID |
|-----|-----|
| `.../main-benefits/jobseeker-support/index.html` | `jobseeker-support` |
| `.../extra-help/housing-support-products/bond-grant/index.html` | `bond-grant` |
| `.../income-support/core-policy/agents/index.html` | `core-policy--agents` (parent of `agents` is `core-policy`) |
| `.../students/agents/index.html` | `students--agents` |

---

## Script 1 — `1_build_queue.py`

**Input:** `map-sitemap.md`  
**Output:** `workspace/queue.json`

### Sitemap Markdown Format

The sitemap uses headings for sections and markdown links for pages:

```
## [Section Name](url)         ← top-level section
### [Subsection](url)          ← subsection (e.g. Core Policy under Income Support)
- [Page Name](url)             ← page at any indentation depth
  - [Child Page](url)
    - [Grandchild Page](url)
```

### Section → Slug Mapping

Queue entries carry a `section` field used by `--section` filtering:

| Heading path | section slug |
|---|---|
| Income support > Core policy | `income-support-core-policy` |
| Income support > Extra help | `income-support-extra-help` |
| Income support > Main benefits | `income-support-main-benefits` |
| Employment and training | `employment-and-training` |
| Social housing | `social-housing` |
| Students | `students` |
| Card services | `card-services` |
| To or from overseas | `to-or-from-overseas` |
| Youth service | `youth-service` |

### Queue Order

Process in this order (Core Policy strictly first):

```python
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
```

### Excluded Top-Level Sections

```python
EXCLUDED_SECTIONS = {"deskfile", "legislation"}
```

### Implementation

```python
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
    """
    Returns list of {url, name, section} dicts for all non-excluded pages.
    Section headings themselves are not included — only leaf pages.
    """
    entries: list[dict] = []
    current_h2 = ""
    current_h3 = ""
    excluded = False

    link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    for line in text.splitlines():
        stripped = line.strip()

        # H2 section heading
        if stripped.startswith("## "):
            m = link_re.search(stripped)
            if m:
                current_h2 = slugify(m.group(1))
                current_h3 = ""
                excluded = current_h2 in EXCLUDED_SECTIONS
            continue

        # H3 subsection heading
        if stripped.startswith("### "):
            m = link_re.search(stripped)
            if m:
                current_h3 = slugify(m.group(1))
            continue

        # Skip if in excluded section
        if excluded:
            continue

        # List item (leaf page at any depth)
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
    """Sort entries by QUEUE_ORDER; discard any section not in the order list."""
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
```

---

## Script 2 — `2_build_lookup.py`

**Input:** `map-sitemap.md`  
**Output:** `workspace/lookup.json`

Parses the **full** sitemap (all sections, including Deskfile and Legislation) to generate IDs for every page. This ensures the lookup is complete even for pages we won't process — their IDs may appear as relationships in processed pages.

### Implementation

```python
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
    """Returns (name, url) for every page in the sitemap, no filtering."""
    pages = []
    link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("## ", "### ")):
            continue  # skip section headings
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
```

---

## Script 3 — `3_pipeline.py`

**Input:** `workspace/queue.json`, `workspace/lookup.json`  
**Output:** `output/{type}/{id}.json` per page, `workspace/run_log.json`  
**Flag:** `--section` (optional, e.g. `--section income-support-core-policy`)

### Full Script Structure

```python
import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

WORKSPACE = Path("workspace")
OUTPUT = Path("output")
BASE_URL = "https://www.workandincome.govt.nz/map/"
CALL1_MODEL = os.getenv("CALL1_MODEL", "claude-sonnet-4-6")
CALL2_MODEL = os.getenv("CALL2_MODEL", "claude-haiku-4-5-20251001")

# Output subdirectory per entity type
TYPE_DIRS = {
    "product": "products",
    "programme": "programmes",
    "policy": "policies",
    "process": "processes",
    "social_housing": "social_housing",
    "actor": "actors",
    "card": "cards",
    "concept": "concepts",
}

# --- JSON Schemas ---

BASE_SCHEMA = {
    "id": "", "type": "", "archimate_type": "", "name": "",
    "description": "", "source_url": "",
    "tags": [],
    "related": {
        "products": [], "programmes": [], "policies": [],
        "processes": [], "actors": [], "concepts": [],
        "cards": [], "social_housing": []
    },
    "attributes": {}
}

ATTRIBUTE_SCHEMAS = {
    "product": {
        "summary": "", "eligibility": "", "payment_details": "",
        "obligations": "", "stand_down": "", "income_test": "",
        "application_process": ""
    },
    "programme": {
        "summary": "", "eligibility": "", "target_group": "",
        "provider": "", "funding_type": "", "application_process": ""
    },
    "policy": {
        "summary": "", "rules": "", "exceptions": "",
        "scope": "", "effective_date": ""
    },
    "process": {
        "summary": "", "steps": [], "triggers": "",
        "inputs": "", "outputs": "", "decision_points": ""
    },
    "social_housing": {
        "summary": "", "eligibility": "", "assessment_criteria": "",
        "obligations": "", "application_process": ""
    },
    "actor": {
        "summary": "", "role": "", "responsibilities": "", "interactions": ""
    },
    "card": {
        "summary": "", "eligibility": "", "benefits": "",
        "application_process": ""
    },
    "concept": {
        "summary": "", "definition": "", "context": "", "examples": ""
    },
}


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def append_log(entry: dict):
    log_path = WORKSPACE / "run_log.json"
    log = load_json(log_path) if log_path.exists() else []
    log.append(entry)
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def output_path_for(entity_type: str, entity_id: str) -> Path | None:
    dir_name = TYPE_DIRS.get(entity_type)
    if not dir_name:
        return None
    return OUTPUT / dir_name / f"{entity_id}.json"


def already_processed(entity_type: str, entity_id: str) -> bool:
    p = output_path_for(entity_type, entity_id)
    return p is not None and p.exists()


def strip_footers(text: str) -> str:
    text = re.sub(r"For more information see:.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\nLegislation\n.*", "", text, flags=re.DOTALL)
    return text.strip()


# Build a type map from existing output for use in Call 2 context
def build_id_type_map() -> dict[str, str]:
    id_to_type = {}
    for entity_type, dir_name in TYPE_DIRS.items():
        for f in (OUTPUT / dir_name).glob("*.json"):
            id_to_type[f.stem] = entity_type
    return id_to_type
```

### Step 1 — Scrape with Playwright

```python
def scrape_page(url: str, browser) -> str | None:
    """
    Returns stripped page text or None on failure.

    Index pages (ending in /index.html) use #second-level-nav with an Expand
    click. Non-index .html pages use #content directly.
    """
    try:
        page = browser.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")

        if page.query_selector("#second-level-nav"):
            expand = page.query_selector("text=Expand")
            if expand:
                expand.click()
                page.wait_for_timeout(2000)
            h1 = page.query_selector("#content h1")
            title = h1.inner_text().strip() if h1 else ""
            body = page.query_selector("#second-level-nav").inner_text()
            text = f"{title}\n\n{body}".strip() if title else body
        else:
            el = page.query_selector("#content")
            text = el.inner_text() if el else ""

        page.close()
        return strip_footers(text) if text.strip() else None
    except Exception as e:
        print(f"  Scrape failed: {e}", file=sys.stderr)
        return None
```

### Step 2 — Call 1: Infer

Uses `claude-sonnet-4-6`. The lookup JSON is passed in the system prompt with prompt caching to avoid re-tokenising it on every call.

```python
CALL1_SYSTEM = """\
You are analysing a page from the MSD Social Development Policy Guide (MAP).
Your job is to extract structured information from the page text and classify it as a business architecture entity.

Valid entity types: product, programme, policy, process, social_housing, actor, card, concept

ArchiMate 3.2 type mappings:
- product → BusinessService
- programme → BusinessFunction
- policy → BusinessConstraint
- process → BusinessProcess
- social_housing → BusinessService
- actor → BusinessActor
- card → BusinessService
- concept → BusinessObject

Rules:
- Only reference IDs that exist in the lookup below. Do not invent IDs.
- related_ids must be a flat array of ID strings from the lookup only.
- If an application process can be resolved to a process entity ID from the lookup, include it in related_ids and note it in raw_attributes.application_process as the ID string.
- If application steps are described inline but no matching process entity exists in the lookup, leave raw_attributes.application_process empty, populate application_process_freetext with the inline description, and set flag_for_review to true.
- Set confidence to "high" if the page clearly maps to one entity type and content is complete.
- Set confidence to "medium" if the type is inferred with some uncertainty or content is partial.
- Set confidence to "low" if the page is ambiguous, sparse, or could fit multiple types.
- For the "id" field: derive it from the source URL by stripping the base "https://www.workandincome.govt.nz/map/", dropping "index.html", and taking the final path segment (strip ".html" if present). Cross-reference with the lookup values to confirm — use the confirmed lookup value as the authoritative id.
- Do not include content from "For more information see:" or "Legislation" footer sections.
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names.

Return only a valid JSON object with this structure. No commentary, no markdown fences.
{
  "id": "",
  "name": "",
  "inferred_type": "",
  "archimate_type": "",
  "description": "",
  "summary": "",
  "tags": [],
  "related_ids": [],
  "raw_attributes": {},
  "confidence": "",
  "confidence_notes": "",
  "application_process_freetext": "",
  "flag_for_review": false
}

Known entity lookup (name → id):
{lookup_json}
"""

CALL1_USER = """\
Page source URL: {source_url}

Page text:
---
{page_text}
---
"""


def call1_infer(client: anthropic.Anthropic, url: str, page_text: str, lookup: dict) -> dict | None:
    lookup_json = json.dumps(lookup, indent=2)
    # Use .replace() — .format() would choke on { } inside the JSON values
    system_prompt = CALL1_SYSTEM.replace("{lookup_json}", lookup_json)
    user_content = CALL1_USER.replace("{source_url}", url).replace("{page_text}", page_text)

    try:
        response = client.messages.create(
            model=CALL1_MODEL,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},  # cache the large lookup
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        )
        raw = response.content[0].text.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Call 1 failed: {e}", file=sys.stderr)
        return None
```

### Step 3 — Call 2: Format

Uses `claude-haiku-4-5-20251001`. Receives Call 1 output + the two schemas + the id→type context for resolving `related` sub-arrays.

```python
CALL2_SYSTEM = """\
You are formatting extracted business architecture data into a structured JSON file.
Use the inferred data to populate the final JSON schema exactly.
Do not omit any fields. Leave unknown fields as empty strings or empty arrays.

Rules:
- related must use the related_ids from the inferred data, sorted into the correct sub-arrays by entity type.
- Use the id_type_map below to resolve entity types. If an ID is not in the map, infer from the ID name.
- attributes must follow the type-specific schema exactly — no extra fields, no missing fields.
- summary goes inside attributes.
- description is a single sentence in the base envelope.
- Do not add commentary. Return only valid JSON. No markdown fences.
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names.
"""

CALL2_USER = """\
Inferred data:
{call1_output}

Base schema:
{base_schema}

Type-specific attribute schema for type "{inferred_type}":
{attribute_schema}

Source URL: {source_url}

Known ID→type map (for resolving related sub-arrays):
{id_type_map}
"""


def call2_format(
    client: anthropic.Anthropic,
    call1_result: dict,
    source_url: str,
    id_to_type: dict,
) -> dict | None:
    inferred_type = call1_result.get("inferred_type", "concept")
    attr_schema = ATTRIBUTE_SCHEMAS.get(inferred_type, ATTRIBUTE_SCHEMAS["concept"])

    # Only pass types for the actual related IDs to keep prompt small
    relevant_id_type = {
        rid: id_to_type[rid]
        for rid in call1_result.get("related_ids", [])
        if rid in id_to_type
    }

    try:
        response = client.messages.create(
            model=CALL2_MODEL,
            max_tokens=4096,
            system=CALL2_SYSTEM,
            messages=[
                {
                    "role": "user",
                    # Use .replace() — .format() would choke on { } inside the JSON values
                    "content": (
                        CALL2_USER
                        .replace("{call1_output}", json.dumps(call1_result, indent=2))
                        .replace("{base_schema}", json.dumps(BASE_SCHEMA, indent=2))
                        .replace("{inferred_type}", inferred_type)
                        .replace("{attribute_schema}", json.dumps(attr_schema, indent=2))
                        .replace("{source_url}", source_url)
                        .replace("{id_type_map}", json.dumps(relevant_id_type, indent=2))
                    ),
                }
            ],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Call 2 failed: {e}", file=sys.stderr)
        return None
```

### Main Loop

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", default=None,
                        help="Process only this section slug, e.g. income-support-core-policy")
    args = parser.parse_args()

    queue: list[dict] = load_json(WORKSPACE / "queue.json")
    lookup: dict = load_json(WORKSPACE / "lookup.json")

    if args.section:
        queue = [e for e in queue if e["section"] == args.section]
        if not queue:
            print(f"No entries for section: {args.section}")
            sys.exit(1)
        print(f"Processing section '{args.section}': {len(queue)} pages")
    else:
        print(f"Processing full queue: {len(queue)} pages")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from .env via load_dotenv()
    id_to_type = build_id_type_map()

    # Ensure output dirs exist
    for dir_name in TYPE_DIRS.values():
        (OUTPUT / dir_name).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        for i, entry in enumerate(queue, 1):
            url = entry["url"]
            name = entry["name"]
            section = entry["section"]
            print(f"[{i}/{len(queue)}] {name} ({section})")

            # Derive ID from URL using same logic as lookup builder
            seen_tmp: dict[str, str] = {}  # local; real collision-check uses lookup
            entity_id = derive_id_from_url(url)  # see note below

            # Skip-if-exists check (need type, so check all output dirs)
            existing = find_existing_output(entity_id)
            if existing:
                print(f"  Skipping — already exists at {existing}")
                append_log({
                    "id": entity_id, "url": url, "type": None,
                    "status": "skipped", "confidence": None,
                    "confidence_notes": None, "flag_for_review": False,
                    "output_path": str(existing)
                })
                continue

            # Step 1: Scrape
            page_text = scrape_page(url, browser)
            if not page_text:
                print("  Failed — empty or fetch error")
                append_log({
                    "id": entity_id, "url": url, "type": None,
                    "status": "failed", "confidence": None,
                    "confidence_notes": "Scrape returned empty", "flag_for_review": False,
                    "output_path": None
                })
                continue

            # Step 2: Call 1 — Infer
            call1 = call1_infer(client, url, page_text, lookup)
            if not call1:
                append_log({
                    "id": entity_id, "url": url, "type": None,
                    "status": "failed", "confidence": None,
                    "confidence_notes": "Call 1 failed", "flag_for_review": False,
                    "output_path": None
                })
                continue

            # Use Claude's inferred ID (may differ from our derivation for .html paths)
            entity_id = call1.get("id") or entity_id
            inferred_type = call1.get("inferred_type", "concept")

            # Step 3: Call 2 — Format
            final = call2_format(client, call1, url, id_to_type)
            if not final:
                append_log({
                    "id": entity_id, "url": url, "type": inferred_type,
                    "status": "failed", "confidence": call1.get("confidence"),
                    "confidence_notes": "Call 2 failed", "flag_for_review": False,
                    "output_path": None
                })
                continue

            # Step 4: Write output
            out_path = output_path_for(inferred_type, entity_id)
            if out_path:
                out_path.write_text(
                    json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                # Update runtime id→type map for subsequent Call 2s
                id_to_type[entity_id] = inferred_type

            log_entry = {
                "id": entity_id,
                "url": url,
                "type": inferred_type,
                "status": "success",
                "confidence": call1.get("confidence"),
                "confidence_notes": call1.get("confidence_notes"),
                "flag_for_review": call1.get("flag_for_review", False),
                "output_path": str(out_path) if out_path else None,
            }
            append_log(log_entry)
            print(f"  Done — {inferred_type}/{entity_id}.json  [{call1.get('confidence')}]")

        browser.close()


if __name__ == "__main__":
    main()
```

### Helper: `find_existing_output`

```python
def find_existing_output(entity_id: str) -> Path | None:
    for dir_name in TYPE_DIRS.values():
        p = OUTPUT / dir_name / f"{entity_id}.json"
        if p.exists():
            return p
    return None
```

### Helper: `derive_id_from_url` (simplified inline version)

```python
BASE = "https://www.workandincome.govt.nz/map/"

def derive_id_from_url(url: str) -> str:
    path = url.replace(BASE, "").rstrip("/")
    path = path.removesuffix("index.html").rstrip("/")
    if path.endswith(".html"):
        path = path[:-5]
    segments = [s for s in path.split("/") if s]
    return segments[-1] if segments else "root"
```

> **Note:** The pipeline uses Claude's own inferred `id` field from Call 1 as the authoritative ID, with the URL-derived ID as a fallback. Call 1 receives the full `lookup.json` and is instructed to pick the correct ID from it — Claude will be more accurate for `.html` pages and edge cases than a pure regex derivation.

---

## Script 4 — `4_report.py`

**Input:** `workspace/run_log.json`  
**Output:** `workspace/review.json`

```python
import json
from pathlib import Path

WORKSPACE = Path("workspace")


def main():
    log_path = WORKSPACE / "run_log.json"
    if not log_path.exists():
        print("No run_log.json found.")
        return

    log: list[dict] = json.loads(log_path.read_text(encoding="utf-8"))
    review: dict[str, list] = {
        "failed_scrape": [],
        "low_confidence": [],
        "needs_review": [],
    }

    for entry in log:
        if entry.get("status") == "failed":
            review["failed_scrape"].append(entry)
        if entry.get("confidence") in ("medium", "low"):
            review["low_confidence"].append(entry)
        if entry.get("flag_for_review"):
            review["needs_review"].append(entry)

    total = sum(len(v) for v in review.values())
    (WORKSPACE / "review.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Review report written: {total} items flagged")
    print(f"  failed_scrape:  {len(review['failed_scrape'])}")
    print(f"  low_confidence: {len(review['low_confidence'])}")
    print(f"  needs_review:   {len(review['needs_review'])}")


if __name__ == "__main__":
    main()
```

---

## Script 5 — `5_to_archimate.py`

**Input:** `output/` directory tree, `workspace/run_log.json`  
**Output:** `workspace/msd-map.xml`

Reads all JSON entity files and converts them to a single ArchiMate Open Exchange 3.0 XML file, ready to import into Archi or any compliant tool.

### Relationship Type Mapping

| Source `archimate_type` | Related category | Relationship `xsi:type` |
|---|---|---|
| `BusinessService` | `processes` | `ServingRelationship` |
| `BusinessService` | `policies` | `InfluenceRelationship` |
| `BusinessService` | `actors` | `AssociationRelationship` |
| `BusinessService` | `products`, `cards`, `social_housing` | `AssociationRelationship` |
| `BusinessFunction` | `processes` | `TriggeringRelationship` |
| `BusinessFunction` | `policies` | `InfluenceRelationship` |
| `BusinessFunction` | `actors` | `AssociationRelationship` |
| `BusinessProcess` | `products`, `cards`, `social_housing` | `ServingRelationship` |
| `BusinessProcess` | `actors` | `AssociationRelationship` |
| `BusinessProcess` | `policies` | `InfluenceRelationship` |
| `BusinessConstraint` | *(any)* | `InfluenceRelationship` |
| `BusinessActor` | *(any)* | `AssociationRelationship` |
| `BusinessObject` | *(any)* | `AssociationRelationship` |
| *(any)* | `concepts` | `AssociationRelationship` |

Fallback for unmatched combinations: `AssociationRelationship`.

### Relationship Identifier Scheme

`rel-{source_id}--{target_id}--{rel_type_abbrev}`

Abbreviations: `serving`, `triggering`, `influence`, `association`.

### Deduplication

Track every `(source_id, target_id, xsi:type)` tuple in a `seen` set. Skip already-seen tuples. `A→B` and `B→A` are distinct.

### Skip Conditions

Skip any JSON file where `id` or `archimate_type` is empty/missing, or where the entity's `id` has `status == "failed"` in `run_log.json`.

### Implementation

```python
import json
import glob
from pathlib import Path
from lxml import etree

WORKSPACE = Path("workspace")
OUTPUT = Path("output")
ARCHIMATE_NS = "http://www.opengroup.org/xsd/archimate/3.0/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

REL_TYPE_ABBREV = {
    "ServingRelationship": "serving",
    "TriggeringRelationship": "triggering",
    "InfluenceRelationship": "influence",
    "AssociationRelationship": "association",
}

def infer_rel_type(src_archimate: str, related_category: str) -> str:
    rules = {
        ("BusinessService", "processes"): "ServingRelationship",
        ("BusinessService", "policies"): "InfluenceRelationship",
        ("BusinessProcess", "products"): "ServingRelationship",
        ("BusinessProcess", "cards"): "ServingRelationship",
        ("BusinessProcess", "social_housing"): "ServingRelationship",
        ("BusinessProcess", "policies"): "InfluenceRelationship",
        ("BusinessFunction", "processes"): "TriggeringRelationship",
        ("BusinessFunction", "policies"): "InfluenceRelationship",
        ("BusinessConstraint", "*"): "InfluenceRelationship",
    }
    # Wildcard target: concepts always association
    if related_category == "concepts":
        return "AssociationRelationship"
    # Specific match
    key = (src_archimate, related_category)
    if key in rules:
        return rules[key]
    # Wildcard source
    if (src_archimate, "*") in rules:
        return rules[(src_archimate, "*")]
    return "AssociationRelationship"


def main():
    # Load failed IDs from run_log
    log_path = WORKSPACE / "run_log.json"
    failed_ids = set()
    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
        failed_ids = {e["id"] for e in log if e.get("status") == "failed" and e.get("id")}

    # Glob all JSON files
    json_files = glob.glob(str(OUTPUT / "**" / "*.json"), recursive=True)

    nsmap = {None: ARCHIMATE_NS, "xsi": XSI_NS}
    model = etree.Element("model", nsmap=nsmap)
    model.set("{{{}}}schemaLocation".format(XSI_NS),
              "http://www.opengroup.org/xsd/archimate/3.0/ archimate3_Diagram.xsd")
    model.set("identifier", "model-msd-map")
    etree.SubElement(model, "name").text = "MSD MAP Business Architecture"

    elements_el = etree.SubElement(model, "elements")
    relationships_el = etree.SubElement(model, "relationships")

    entities = []
    skipped = 0

    for fpath in sorted(json_files):
        data = json.loads(Path(fpath).read_text(encoding="utf-8"))
        eid = data.get("id", "").strip()
        atype = data.get("archimate_type", "").strip()

        if not eid or not atype:
            print(f"  Skipping {fpath} — missing id or archimate_type")
            skipped += 1
            continue
        if eid in failed_ids:
            print(f"  Skipping {eid} — status=failed in run_log")
            skipped += 1
            continue

        el = etree.SubElement(elements_el, "element")
        el.set("identifier", eid)
        el.set("{{{}}}type".format(XSI_NS), atype)
        etree.SubElement(el, "name").text = data.get("name", "")
        desc = data.get("description", "").strip()
        if desc:
            etree.SubElement(el, "documentation").text = desc

        entities.append(data)

    # Build set of valid IDs — only emit relationships to elements that were actually written
    valid_ids = {data["id"] for data in entities}

    seen = set()
    rel_count = 0
    dup_count = 0

    for data in entities:
        src_id = data["id"]
        src_atype = data.get("archimate_type", "")
        related = data.get("related", {})

        for category, target_ids in related.items():
            rel_type = infer_rel_type(src_atype, category)
            abbrev = REL_TYPE_ABBREV.get(rel_type, "association")

            for tgt_id in target_ids:
                if not tgt_id or tgt_id not in valid_ids:
                    continue
                triple = (src_id, tgt_id, rel_type)
                if triple in seen:
                    dup_count += 1
                    continue
                seen.add(triple)

                rel_id = f"rel-{src_id}--{tgt_id}--{abbrev}"
                rel_el = etree.SubElement(relationships_el, "relationship")
                rel_el.set("identifier", rel_id)
                rel_el.set("{{{}}}type".format(XSI_NS), rel_type)
                rel_el.set("source", src_id)
                rel_el.set("target", tgt_id)
                rel_count += 1

    tree = etree.ElementTree(model)
    out_path = WORKSPACE / "msd-map.xml"
    tree.write(str(out_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)

    print(f"Elements:      {len(entities)}")
    print(f"Relationships: {rel_count} ({dup_count} duplicates skipped)")
    print(f"Skipped files: {skipped}")
    print(f"Output:        {out_path}")


if __name__ == "__main__":
    main()
```

---

## Dependencies and Setup

```bash
pip install playwright anthropic python-dotenv lxml
playwright install chromium
```

Note: `glob`, `json`, `re`, `os`, `pathlib` are Python stdlib — no installation needed.

Copy `.env` and fill in your key:
```
ANTHROPIC_API_KEY=your_key_here
CALL1_MODEL=claude-sonnet-4-6
CALL2_MODEL=claude-haiku-4-5-20251001
```

Python 3.10+ required (uses `dict | list` union syntax and `str.removesuffix`).

---

## Run Order

```bash
# Once — fast, safe to re-run
python 1_build_queue.py
python 2_build_lookup.py

# Mandatory first batch
python 3_pipeline.py --section income-support-core-policy

# Subsequent batches in any order
python 3_pipeline.py --section income-support-extra-help
python 3_pipeline.py --section income-support-main-benefits
python 3_pipeline.py --section employment-and-training
python 3_pipeline.py --section social-housing
python 3_pipeline.py --section students
python 3_pipeline.py --section card-services
python 3_pipeline.py --section to-or-from-overseas
python 3_pipeline.py --section youth-service

# Or run all at once (full queue in order)
python 3_pipeline.py

# After all batches complete
python 4_report.py

# Convert JSON output to ArchiMate Open Exchange XML
python 5_to_archimate.py
```

---

## Key Implementation Notes

### Prompt Caching
Call 1 uses `cache_control: ephemeral` on the system prompt to cache the lookup JSON across all pages in a single run. The lookup is identical for every page, so this avoids re-tokenising ~1000 name→ID pairs each time. Haiku does not use caching in Call 2 since inputs are all unique.

### Skip-If-Exists
The pipeline checks all `output/*/id.json` paths before scraping. A page already on disk is logged as `skipped` and not re-processed. This makes it safe to re-run a section after a crash.

### Footer Stripping
Two footer patterns confirmed on live pages:

- `"For more information see:"` — appears as a standalone heading in a `.block` div at the end of non-index pages, followed by a list of links.
- `"\nLegislation\n"` — appears immediately after the "For more information see:" block, listing act/regulation citations. Use a newline-bounded pattern to avoid false positives against in-body text like "Social Security Legislation Act".

Both always appear at the tail of the content and always co-occur (Legislation follows For more information see:). Stripping from "For more information see:" is sufficient in practice, but the Legislation pattern is kept as an independent guard:

```python
def strip_footers(text: str) -> str:
    text = re.sub(r"For more information see:.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\nLegislation\n.*", "", text, flags=re.DOTALL)
    return text.strip()
```

### Playwright Selector
Two distinct page structures exist:

**Index pages** (URL ends in `/index.html`, have `#second-level-nav`): policy text lives inside `#second-level-nav`, which starts collapsed. Must click the `Expand` button and wait ~2s before calling `inner_text()`. The H1 title is a sibling element inside `#content` and should be prepended to the extracted text. The `#related-col-wrap` sidebar ("Related information" links) is a sibling of `#second-level-nav` inside `#content` and is automatically excluded by targeting `#second-level-nav` directly.

**Non-index pages** (URL ends in `.html`, no `#second-level-nav`): policy text is in `.block` div children of `#content`. Use `#content` directly; no expand step needed.

Scraper logic:
```python
if page.query_selector("#second-level-nav"):
    expand = page.query_selector("text=Expand")
    if expand:
        expand.click()
        page.wait_for_timeout(2000)
    h1 = page.query_selector("#content h1")
    title = h1.inner_text().strip() if h1 else ""
    body = page.query_selector("#second-level-nav").inner_text()
    text = f"{title}\n\n{body}".strip() if title else body
else:
    text = page.query_selector("#content").inner_text()
```

### ID Authority
Call 1 returns an `"id"` field. The pipeline uses this as the authoritative ID (Claude has the full lookup and picks the right one). The URL-derived ID is only a fallback if Call 1 fails. This handles non-standard URLs like `.../cost-of-living-payment.html` where the segment is already the ID.

### run_log.json Resilience
The log is appended after every page. A mid-run crash is recoverable — re-run the same section and the skip-if-exists check will skip already-written files.

### Lookup Table Size
The full lookup will be ~400–600 entries. At roughly 30 tokens/entry that's ~15,000–18,000 tokens per Call 1 system prompt — within context limits for `claude-sonnet-4-6` and cache-eligible on the first call per session TTL.

---

## Todo List

### Phase 0 — Environment Setup

- [x] Create and activate a Python 3.10+ virtual environment (Python 3.12 via `/opt/homebrew/opt/python@3.12`, venv at `.venv/`)
- [x] Install dependencies: `pip install playwright anthropic python-dotenv`
- [x] Install Chromium: `playwright install chromium`
- [x] **Fill in `ANTHROPIC_API_KEY` in `.env`** — validated against `claude-sonnet-4-6`
- [x] Manually open one MAP page in a browser and inspect the HTML — identified two page types: index pages use `#second-level-nav` (requires Expand click); non-index `.html` pages use `#content` directly
- [x] Note which element contains the "For more information see:" and "Legislation" footers — confirmed in `.block` divs at tail of non-index pages; regex patterns `For more information see:.*` and `\nLegislation\n.*` confirmed correct

---

### Phase 1 — `1_build_queue.py`

- [x] Create `1_build_queue.py`
- [x] Implement `slugify(text)` — lowercase, replace non-alphanumeric runs with `-`, strip leading/trailing `-`
- [x] Implement `parse_sitemap(text)` — line-by-line parser tracking current H2 and H3 headings
  - [x] Detect `## [Name](url)` lines — extract name, set `current_h2 = slugify(name)`, reset `current_h3`, set `excluded` flag if name is in `EXCLUDED_SECTIONS`
  - [x] Detect `### [Name](url)` lines — extract name, set `current_h3 = slugify(name)`
  - [x] Detect `- [Name](url)` list items — if not excluded, append `{url, name, section}` where `section` is `f"{h2}-{h3}"` if h3 is set, else just `h2`
  - [x] Skip all list items when `excluded` is True
- [x] Define `QUEUE_ORDER` list and `EXCLUDED_SECTIONS` set as module-level constants
- [x] Implement `build_queue(entries)` — bucket entries by section, concatenate buckets in QUEUE_ORDER, discard entries whose section is not in the order list
- [x] Write sorted list to `workspace/queue.json`
- [x] **Validate:** run the script, check total page count is reasonable (~200–350 pages), verify each section slug appears as expected, spot-check that Deskfile and Legislation URLs are absent

---

### Phase 2 — `2_build_lookup.py`

- [x] Create `2_build_lookup.py`
- [x] Implement `derive_id(url, seen_ids)` — strip base URL, remove `index.html`, strip trailing `/`, handle `.html` non-index pages by taking the parent segment, extract final path segment as candidate, apply `parent--candidate` prefix on collision, mutate `seen_ids` in place, return final ID string
- [x] Implement `parse_all_pages(text)` — extract `(name, url)` from every `- [Name](url)` list item in the full sitemap with **no** section filtering
- [x] Initialise a `seen_ids` dict, iterate all pages, call `derive_id()` for each, build `{name: id}` map
- [x] Write map to `workspace/lookup.json`
- [x] **Validate:** spot-check known entries:
  - [x] `"Jobseeker Support"` → `"jobseeker-support"` ✓
  - [x] `"Bond Grant"` → `"bond-grant"` ✓
  - [x] Both "Agents" pages → first (core-policy) gets `agents`, second (students) gets `students--agents` ✓
  - [x] Confirm no duplicate IDs in the values — 0 duplicates ✓

---

### Phase 3 — `3_pipeline.py`

#### 3A — Shared Infrastructure

- [x] Create `3_pipeline.py` with all imports (`argparse`, `json`, `re`, `sys`, `pathlib`, `anthropic`, `playwright`)
- [x] Define module-level constants: `WORKSPACE`, `OUTPUT`, `BASE_URL`, `TYPE_DIRS`
- [x] Define `BASE_SCHEMA` dict and `ATTRIBUTE_SCHEMAS` dict for all 8 entity types
- [x] Implement `load_json(path)` — read and parse JSON from a Path
- [x] Implement `append_log(entry)` — read existing `run_log.json` (or start with `[]`), append entry, write back immediately after every page
- [x] Implement `output_path_for(entity_type, entity_id)` — look up `TYPE_DIRS`, return `output/{dir}/{id}.json` Path or None for unknown type
- [x] Implement `find_existing_output(entity_id)` — scan all `output/*/` dirs for `{id}.json`, return Path or None
- [x] Implement `derive_id_from_url(url)` — simplified inline version (no collision tracking; used as fallback only)
- [x] Implement `build_id_type_map()` — glob `output/{dir}/*.json` for all dirs, build `{id: type}` dict from existing files at startup

#### 3B — Playwright Scraper

- [x] Implement `scrape_page(url, browser)` — open new page, navigate to URL with `wait_until="domcontentloaded"`, try content selectors in order, call `inner_text()`, close page, return stripped text or None on exception/empty
- [x] Implement `strip_footers(text)` — apply two regex passes: one for `"For more information see:"` onward, one for `"Legislation"` section heading onward; both case-insensitive, DOTALL
- [x] **Validate:** scraped Jobseeker Support index page (25,869 chars, no footers) and a non-index .html page (2,835 chars, no footers) — both clean
- [x] Adjust CSS selector and/or footer regex patterns based on actual page output if needed

#### 3C — Call 1 (Infer)

- [x] Write `CALL1_SYSTEM` string template — includes entity type list, ArchiMate mappings, all rules, the schema structure, and `{lookup_json}` placeholder at the end
- [x] Write `CALL1_USER` string template — `{source_url}` and `{page_text}` placeholders
- [x] Implement `call1_infer(client, url, page_text, lookup)`:
  - [x] Format `CALL1_SYSTEM` with serialised `lookup` dict
  - [x] Call `client.messages.create()` with `model="claude-sonnet-4-6"`, `max_tokens=4096`
  - [x] Pass system as a list with one block, adding `"cache_control": {"type": "ephemeral"}` to cache the lookup across calls
  - [x] Strip any markdown code fences from response text with regex
  - [x] Parse with `json.loads()`, return dict or None on any exception
- [x] **Validate:** all 13 fields present, `inferred_type=policy`, `archimate_type=BusinessConstraint`, `confidence=high`, no hallucinated related_ids
- [x] Confirm prompt cache hit on second call — `cache_read_input_tokens=5729` confirmed on both calls

#### 3D — Call 2 (Format)

- [x] Write `CALL2_SYSTEM` string — rules for field placement, related sorting, no omissions, no commentary
- [x] Write `CALL2_USER` string template — placeholders for `call1_output`, `base_schema`, `inferred_type`, `attribute_schema`, `source_url`, `id_type_map`
- [x] Implement `call2_format(client, call1_result, source_url, id_to_type)`:
  - [x] Resolve `inferred_type` from call1_result (default `"concept"`)
  - [x] Look up `ATTRIBUTE_SCHEMAS[inferred_type]`
  - [x] Build `relevant_id_type` — filter `id_to_type` to only IDs in `call1_result["related_ids"]`
  - [x] Call `client.messages.create()` with `model="claude-haiku-4-5-20251001"`, `max_tokens=4096`
  - [x] Strip fences and parse JSON, return dict or None on exception
- [x] **Validate:** all base schema keys present, no extras; policy attribute keys match exactly (`effective_date`, `exceptions`, `rules`, `scope`, `summary`) — no missing, no extra

#### 3E — Main Loop

- [x] Implement `argparse` with `--section` argument (optional, default None)
- [x] Load `queue.json` and `lookup.json` at startup; abort with a clear message if either is missing
- [x] If `--section` provided, filter queue; print count and exit with error if zero entries found
- [x] Initialise `anthropic.Anthropic()` client (raises clear error if `ANTHROPIC_API_KEY` not set)
- [x] Call `build_id_type_map()` to seed the runtime type resolver
- [x] Create all `output/{dir}/` directories with `mkdir(parents=True, exist_ok=True)`
- [x] Open Playwright browser with `sync_playwright` context manager
- [x] Iterate queue entries with `enumerate(queue, 1)`, printing `[i/total] name (section)` per page
- [x] Per page: derive fallback ID from URL → check skip-if-exists → scrape → Call 1 → Call 2 → write JSON → append log; `continue` at each failure point after logging
- [x] After writing a successful output file, update `id_to_type[entity_id] = inferred_type`
- [x] Close browser in `finally` block (or rely on context manager exit)
- [x] Guard `main()` with `if __name__ == "__main__":`

---

### Phase 4 — `4_report.py`

- [x] Create `4_report.py`
- [x] Load `workspace/run_log.json`; handle missing file gracefully with a printed message
- [x] Iterate log entries, assigning each to one or more buckets:
  - [x] `failed_scrape` — `status == "failed"`
  - [x] `low_confidence` — `confidence in ("medium", "low")`
  - [x] `needs_review` — `flag_for_review == True`
- [x] Write `workspace/review.json` as `{failed_scrape: [...], low_confidence: [...], needs_review: [...]}`
- [x] Print a summary line for each bucket with its count

---

### Phase 5 — Integration Testing

- [x] Run `python 1_build_queue.py`, open `workspace/queue.json` — verify total page count, section distribution, ordering (core-policy entries come first)
- [x] Run `python 2_build_lookup.py`, open `workspace/lookup.json` — spot-check 10 known entries, verify collision IDs are correct
- [x] Run `python 3_pipeline.py --section income-support-core-policy` for a single page (temporarily slice `queue = queue[:1]` or pick a small section) — inspect all intermediate outputs:
  - [x] Confirm scraped text is clean (no footers, reasonable length)
  - [x] Confirm Call 1 JSON has all 12 fields, valid type, IDs from lookup
  - [x] Confirm Call 2 JSON matches full base schema, attributes match type schema
  - [x] Confirm `output/{type}/{id}.json` written to correct directory
  - [x] Confirm `workspace/run_log.json` has one entry with all fields
- [x] Run full `income-support-core-policy` section — confirm process and policy entities land in `output/processes/` and `output/policies/`
- [x] Run `income-support-extra-help` section — verify that `application_process` fields in product entities resolve to IDs that now exist in `output/processes/`, reducing freetext fallbacks
- [x] Run all remaining sections in order
- [x] Run `python 4_report.py` — open `workspace/review.json`, verify bucket groupings are correct and counts match expectations

---

### Phase 6 — `5_to_archimate.py`

- [x] Create `5_to_archimate.py`
- [x] Load failed IDs from `workspace/run_log.json` into a set; handle missing file gracefully
- [x] Glob all `.json` files from `output/**/*.json` recursively
- [x] Per JSON file: skip if `id` or `archimate_type` is empty, or if `id` is in the failed set; log reason to stdout
- [x] Build the XML envelope (`<model>`, `<elements>`, `<relationships>`) using `lxml.etree` with ArchiMate Open Exchange 3.0 namespace
- [x] Emit one `<element>` per valid entity: `identifier` = `id`, `xsi:type` = `archimate_type`, `<name>`, `<documentation>` (omit if empty)
- [x] Implement `infer_rel_type(src_archimate, related_category)` — apply the 14-rule mapping table with `AssociationRelationship` fallback
- [x] For each entity, iterate all `related.*` arrays; call `infer_rel_type`; build `(source, target, xsi:type)` tuple; skip if already in `seen` set; emit `<relationship>` with deterministic `identifier` pattern `rel-{src}--{tgt}--{abbrev}`
- [x] Write output to `workspace/msd-map.xml` (overwrite if exists); print summary: element count, relationship count, duplicates skipped, skipped files
- [x] **Validate:** import `workspace/msd-map.xml` into Archi (or validate against the ArchiMate 3.0 XSD); confirm elements load with correct types and relationships are present

---

### Phase 7 — Output QA

- [x] Spot-check 5–10 output JSON files across different entity types — assess summary quality, relationship accuracy, and field completeness
- [x] Review all `needs_review` entries in `review.json` — manually validate freetext `application_process` descriptions and decide whether any warrant re-running after adding a process entity
- [x] Review `low_confidence` entries — decide per-entry whether to accept, re-run with a revised prompt, or reclassify manually
- [x] Investigate `failed_scrape` entries — check whether pages are live, require JavaScript rendering delays, or have changed structure; retry with adjusted scraper settings if needed
