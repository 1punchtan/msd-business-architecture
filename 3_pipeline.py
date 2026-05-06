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


def find_existing_output(entity_id: str) -> Path | None:
    for dir_name in TYPE_DIRS.values():
        p = OUTPUT / dir_name / f"{entity_id}.json"
        if p.exists():
            return p
    return None


def derive_id_from_url(url: str) -> str:
    path = url.replace(BASE_URL, "").rstrip("/")
    path = path.removesuffix("index.html").rstrip("/")
    if path.endswith(".html"):
        path = path[:-5]
    segments = [s for s in path.split("/") if s]
    return segments[-1] if segments else "root"


def build_id_type_map() -> dict[str, str]:
    id_to_type = {}
    for entity_type, dir_name in TYPE_DIRS.items():
        for f in (OUTPUT / dir_name).glob("*.json"):
            id_to_type[f.stem] = entity_type
    return id_to_type


def strip_footers(text: str) -> str:
    text = re.sub(r"For more information see:.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\nLegislation\n.*", "", text, flags=re.DOTALL)
    return text.strip()


def scrape_page(url: str, browser) -> str | None:
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


CALL1_SYSTEM = """\
You are analysing a page from the MSD Social Development Policy Guide (MAP).
Your job is to extract structured information from the page text and classify it as a business architecture entity.

Valid entity types: product, programme, policy, process, social_housing, actor, card, concept

ArchiMate 3.2 type mappings:
- product → BusinessService
- programme → BusinessFunction
- policy → Constraint
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
                    "cache_control": {"type": "ephemeral"},
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
        raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Call 1 failed: {e}", file=sys.stderr)
        return None


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

    client = anthropic.Anthropic()
    id_to_type = build_id_type_map()

    for dir_name in TYPE_DIRS.values():
        (OUTPUT / dir_name).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        for i, entry in enumerate(queue, 1):
            url = entry["url"]
            name = entry["name"]
            section = entry["section"]
            print(f"[{i}/{len(queue)}] {name} ({section})")

            entity_id = derive_id_from_url(url)

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

            call1 = call1_infer(client, url, page_text, lookup)
            if not call1:
                append_log({
                    "id": entity_id, "url": url, "type": None,
                    "status": "failed", "confidence": None,
                    "confidence_notes": "Call 1 failed", "flag_for_review": False,
                    "output_path": None
                })
                continue

            entity_id = call1.get("id") or entity_id
            inferred_type = call1.get("inferred_type", "concept")

            final = call2_format(client, call1, url, id_to_type)
            if not final:
                append_log({
                    "id": entity_id, "url": url, "type": inferred_type,
                    "status": "failed", "confidence": call1.get("confidence"),
                    "confidence_notes": "Call 2 failed", "flag_for_review": False,
                    "output_path": None
                })
                continue

            out_path = output_path_for(inferred_type, entity_id)
            if out_path:
                out_path.write_text(
                    json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8"
                )
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
