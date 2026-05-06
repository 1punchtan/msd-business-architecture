# MSD MAP Business Architecture — Extraction Pipeline Plan

## Overview

This project extracts structured business architecture data from the MSD Social Development Policy Guide (MAP) at `https://www.workandincome.govt.nz/map/`.

Each page in the MAP wiki is scraped via headless Chromium, passed through two Claude API calls, and output as a typed JSON file. The result is a hierarchy of interlinked JSON files representing MSD's business architecture, loosely aligned with ArchiMate 3.2.

These JSON files will then be read by a script to convert them to ArchiMate Open Exchange XML files, ready to import to Archi, or any ArchiMate compliant tool.

---

## Excluded Sections

Do not process pages from these sections:
- **Deskfile** — reference/rate tables only, no architectural value
- **Legislation** — acts, regulations, ministerial directions

Also exclude from every page before passing to Claude:
- "For more information see:" footer section
- "Legislation" footer section

---

## Entity Types

| Type | ArchiMate 3.2 Alignment | Description |
|---|---|---|
| `product` | BusinessService | A benefit, payment, or allowance MSD delivers |
| `programme` | BusinessFunction | A funded initiative or scheme |
| `policy` | BusinessConstraint / Driver | Rules, eligibility criteria, definitions |
| `process` | BusinessProcess | Steps, procedures, application flows |
| `social_housing` | BusinessService (domain) | Housing-specific services and assessments |
| `actor` | BusinessActor / BusinessRole | Agents, providers, roles |
| `card` | BusinessService | Card-based entitlements |
| `concept` | BusinessObject | Catch-all for definitions, statuses, and anything that doesn't fit |

Each JSON file includes an optional `archimate_type` field with the ArchiMate value as a string.

---

## ID Derivation Rules

IDs are derived from the source URL:
- Strip the base URL: `https://www.workandincome.govt.nz/map/`
- Drop `index.html`
- Take the final meaningful path segment

Examples:
- `.../main-benefits/jobseeker-support/index.html` → `jobseeker-support`
- `.../extra-help/housing-support-products/bond-grant/index.html` → `bond-grant`

**Collision rule:** if two pages share the same final path segment, prefix with the parent segment using `--`:
- `.../income-support/core-policy/agents/index.html` → `income-support--agents`
- `.../students/agents/index.html` → `students--agents`

---

## Base JSON Schema

Every entity file shares this envelope regardless of type:

```json
{
  "id": "",
  "type": "",
  "archimate_type": "",
  "name": "",
  "description": "",
  "source_url": "",
  "tags": [],
  "related": {
    "products": [],
    "programmes": [],
    "policies": [],
    "processes": [],
    "actors": [],
    "concepts": [],
    "cards": [],
    "social_housing": []
  },
  "attributes": {}
}
```

- `description` — one sentence, factual
- `tags` — Claude infers relevant tags from page content
- `related` — arrays of ID strings, drawn strictly from the lookup table
- `attributes` — type-specific fields (see below)

---

## Attribute Schema Per Entity Type

All types include a `summary` field: a 1–2 paragraph plain-English summary of the entity, written by Claude from the page content.

### `product`
```json
{
  "summary": "",
  "eligibility": "",
  "payment_details": "",
  "obligations": "",
  "stand_down": "",
  "income_test": "",
  "application_process": ""
}
```
`application_process`: ID of a linked `process` entity if one exists in the lookup table. If the page describes application steps inline but no process entity exists, populate with freetext and flag for review.

### `programme`
```json
{
  "summary": "",
  "eligibility": "",
  "target_group": "",
  "provider": "",
  "funding_type": "",
  "application_process": ""
}
```
Same `application_process` rule as `product`.

### `policy`
```json
{
  "summary": "",
  "rules": "",
  "exceptions": "",
  "scope": "",
  "effective_date": ""
}
```

### `process`
```json
{
  "summary": "",
  "steps": [],
  "triggers": "",
  "inputs": "",
  "outputs": "",
  "decision_points": ""
}
```

### `social_housing`
```json
{
  "summary": "",
  "eligibility": "",
  "assessment_criteria": "",
  "obligations": "",
  "application_process": ""
}
```
Same `application_process` rule as `product`.

### `actor`
```json
{
  "summary": "",
  "role": "",
  "responsibilities": "",
  "interactions": ""
}
```

### `card`
```json
{
  "summary": "",
  "eligibility": "",
  "benefits": "",
  "application_process": ""
}
```
Same `application_process` rule as `product`.

### `concept`
```json
{
  "summary": "",
  "definition": "",
  "context": "",
  "examples": ""
}
```

**General rule:** Claude populates whatever fields it finds evidence for on the page. Unknown or absent fields are left as empty strings or empty arrays — never omitted. Follow the JSON schema closely, do not include new fields or change existing field names.

---

## Scripts

Four scripts, run in order. Each reads from and writes to a shared `/workspace` directory.

### `1_build_queue.py`

**Input:** `map-sitemap.md`  
**Output:** `workspace/queue.json`

- Parses the sitemap markdown
- Filters out Deskfile and Legislation sections entirely
- Outputs an ordered list of `{url, name, section}` objects
- Queue order is fixed:
  1. Income Support → Core Policy
  2. Income Support → Extra Help
  3. Income Support → Main Benefits
  4. Employment and Training
  5. Social Housing
  6. Students
  7. Card Services
  8. To or From Overseas
  9. Youth Service

Rationale: Core Policy pages contain foundational process and policy entities. Processing them first maximises the chance that `process` IDs are already in the lookup when products and programmes are processed.

### `2_build_lookup.py`

**Input:** `map-sitemap.md`  
**Output:** `workspace/lookup.json`

- Parses the full sitemap (all sections, including excluded ones — we need all IDs)
- Derives an ID for every page using the ID derivation rules above
- Outputs a flat key-value map: `{ "Jobseeker Support": "jobseeker-support", ... }`
- This lookup is used by Claude in Call 1 to resolve relationship names to IDs
- Must be built before any scraping begins

### `3_pipeline.py`

**Input:** `workspace/queue.json`, `workspace/lookup.json`  
**Output:** `output/{entity_type}/{id}.json` per page, `workspace/run_log.json`

This is the main orchestrator. For each URL in the queue, in order:

#### Step 1 — Scrape
- Launch headless Chromium
- Fetch the page
- Extract main content as plain text
- Strip "For more information see:" and "Legislation" footer sections
- If page is empty or fetch fails: log to `run_log.json` as `failed`, skip, continue

#### Step 2 — Call 1 (Infer)
Claude API call. Prompt includes:
- The stripped page text
- The full `lookup.json` as context
- The list of valid entity types

Ask Claude to return a JSON object with:
```json
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
  "confidence": "high | medium | low",
  "confidence_notes": "",
  "application_process_freetext": "",
  "flag_for_review": false
}
```

Rules for Call 1 prompt:
- Only reference IDs that exist in `lookup.json` — no guessing
- `related_ids` must be a flat array of ID strings from the lookup
- If `application_process` can be resolved to a process entity ID, include it in `related_ids` and note it in `raw_attributes`
- If `application_process` is described inline but no process entity exists, populate `application_process_freetext` and set `flag_for_review: true`
- Set confidence based on how clearly the page maps to a single entity type and how complete the content is
- `confidence_notes` should briefly explain any uncertainty
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names

#### Step 3 — Call 2 (Format)
Claude API call. Prompt includes:
- The output from Call 1
- The base schema template
- The type-specific attribute schema for the inferred type
- Instruction to leave unknown fields as empty strings or arrays, never omit them
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names

Ask Claude to return the final JSON file, fully populated, ready to write to disk.

#### Step 4 — Write output
- Determine output path: `output/{inferred_type}/{id}.json`
- Write the JSON file
- Append to `run_log.json`:
```json
{
  "id": "",
  "url": "",
  "type": "",
  "status": "success | failed | skipped",
  "confidence": "high | medium | low",
  "confidence_notes": "",
  "flag_for_review": false,
  "output_path": ""
}
```

### `4_report.py`

**Input:** `workspace/run_log.json`  
**Output:** `workspace/review.json`

- Reads the run log
- Filters entries where `status != "success"` OR `flag_for_review == true` OR `confidence != "high"`
- Outputs `review.json` as an array of those entries, grouped by reason:
  - `failed_scrape` — page fetch failed or returned empty
  - `low_confidence` — confidence is `medium` or `low`
  - `needs_review` — `flag_for_review` is true (e.g. freetext application_process)

### `5_to_archimate.py`

**Input:** `output/` directory tree  
**Output:** `workspace/msd-map.xml`

Converts all JSON entity files to a single ArchiMate Open Exchange XML file, ready to import into Archi or any compliant tool.

#### What it does

1. Globs all `.json` files from every subdirectory under `output/`
2. Emits one `<element>` node per entity, using `archimate_type` for the `xsi:type` attribute and `id` as the identifier
3. Builds relationships from each entity's `related.*` arrays, applying a deterministic relationship type based on source and target entity types (see mapping table below)
4. Deduplicates relationships — tracks every `(source, target, xsi:type)` triple in a set and skips any that have already been emitted
5. Wraps everything in a valid ArchiMate Open Exchange 3.0 XML envelope

#### Relationship type mapping

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

If a source/target combination doesn't match any rule, fall back to `AssociationRelationship`.

#### Deduplication logic

- Each relationship candidate is represented as a `(source_id, target_id, xsi:type)` tuple
- Before emitting, check the tuple against a `seen` set
- If already present, skip — do not emit a duplicate
- `(A → B)` and `(B → A)` are treated as distinct and both emitted if they arise independently

#### Relationship identifier generation

Relationship `identifier` attributes use the pattern `rel-{source_id}--{target_id}--{rel_type_abbrev}`, where `rel_type_abbrev` is a short lowercase token (e.g. `serving`, `triggering`, `influence`, `association`). This keeps identifiers deterministic and human-readable.

#### XML envelope

```xml
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://www.opengroup.org/xsd/archimate/3.0/"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://www.opengroup.org/xsd/archimate/3.0/ archimate3_Diagram.xsd"
       identifier="model-msd-map">
  <name>MSD MAP Business Architecture</name>
  <elements>
    <!-- one <element> per entity -->
  </elements>
  <relationships>
    <!-- one <relationship> per deduplicated pair -->
  </relationships>
</model>
```

#### Element format

```xml
<element identifier="jobseeker-support" xsi:type="BusinessService">
  <name>Jobseeker Support</name>
  <documentation>Provides income support to people who are unemployed or unable to work full-time.</documentation>
</element>
```

- `identifier` → `id` from the JSON envelope
- `xsi:type` → `archimate_type` from the JSON envelope
- `<name>` → `name` from the JSON envelope
- `<documentation>` → `description` from the JSON envelope (single sentence); omit the tag if empty

#### Relationship format

```xml
<relationship identifier="rel-jobseeker-support--application-for-benefit--serving"
              xsi:type="ServingRelationship"
              source="jobseeker-support"
              target="application-for-benefit"/>
```

#### Skipping entities

Skip any JSON file where:
- `id` is empty or missing
- `archimate_type` is empty or missing
- `status` in `run_log.json` is `failed` (cross-reference by id)

Log skipped files to stdout with reason.

#### Usage

```bash
python 5_to_archimate.py
```

Output is always written to `workspace/msd-map.xml`. Overwrites any existing file. Prints a summary on completion:

```
Elements:      142
Relationships: 387 (41 duplicates skipped)
Skipped files: 3
Output:        workspace/msd-map.xml
```


---

## Workspace / Output Folder Structure

```
/
├── map-sitemap.md               # Source sitemap (input, read-only)
├── 1_build_queue.py
├── 2_build_lookup.py
├── 3_pipeline.py
├── 4_report.py
├── 5_to_archimate.py
│
├── workspace/
│   ├── queue.json               # Ordered list of URLs to process
│   ├── lookup.json              # name → ID lookup table
│   └── run_log.json             # Per-page result log
│   └── msd-map.xml              # ArchiMate Open Exchange output
│
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

---

## Claude API Prompt Templates

### Call 1 — Infer

```
You are analysing a page from the MSD Social Development Policy Guide (MAP).
Your job is to extract structured information from the page text and classify it as a business architecture entity.

Page source URL: {source_url}
Page text:
---
{page_text}
---

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

Known entity lookup (name → id):
{lookup_json}

Rules:
- Only reference IDs that exist in the lookup above. Do not invent IDs.
- related_ids must be a flat array of ID strings from the lookup only.
- If an application process can be resolved to a process entity ID from the lookup, include it in related_ids and note it in raw_attributes.application_process as the ID string.
- If application steps are described inline but no matching process entity exists in the lookup, leave raw_attributes.application_process empty, populate application_process_freetext with the inline description, and set flag_for_review to true.
- Set confidence to "high" if the page clearly maps to one entity type and content is complete.
- Set confidence to "medium" if the type is inferred with some uncertainty or content is partial.
- Set confidence to "low" if the page is ambiguous, sparse, or could fit multiple types.
- Do not include content from "For more information see:" or "Legislation" footer sections.
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names.

Return only a valid JSON object with this structure. Do not add or change the field names:
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
```

### Call 2 — Format

```
You are formatting extracted business architecture data into a structured JSON file.

Use the inferred data below to populate the final JSON schema exactly.
Do not omit any fields. Leave unknown fields as empty strings or empty arrays.

Inferred data:
{call_1_output}

Base schema:
{base_schema}

Type-specific attribute schema for type "{inferred_type}":
{attribute_schema}

Rules:
- related must use the related_ids from the inferred data, sorted into the correct sub-arrays by entity type
- To determine entity type for each related ID, look up the ID prefix or infer from context
- attributes must follow the type-specific schema exactly — no extra fields, no missing fields
- summary goes inside attributes
- description is a single sentence in the base envelope
- Do not add commentary. Return only valid JSON.
- Follow the provided JSON schema closely. Do not edit field names, nor create new field names
```

---

## Dependencies

- Python 3.10+
- `playwright` (headless Chromium)
- `anthropic` Python SDK
- `lxml` (XML generation for `5_to_archimate.py`)
- `json`, `re`, `os`, `pathlib`, `glob` (stdlib)

Install:
```bash
pip install playwright anthropic lxml
playwright install chromium
```

Set environment variable:
```bash
export ANTHROPIC_API_KEY=your_key_here
```

---

## Model Strategy

| Call | Model | Rationale |
|---|---|---|
| Call 1 — Infer | `claude-sonnet-4-6` | Requires strong reasoning: entity classification, relationship resolution against a large lookup table, confidence self-assessment, and unstructured policy interpretation. Haiku will produce unreliable classifications and miss relationships. |
| Call 2 — Format | `claude-haiku-4-5-20251001` | Purely a formatting task with explicit schema, structured input, and no ambiguity. Haiku handles this well and runs at significantly lower cost. |

---

## Batch Processing

`3_pipeline.py` accepts an optional `--section` argument to process one section at a time instead of the full queue. This allows cost-controlled batch runs and easier debugging.

Usage:
```bash
python 3_pipeline.py --section "income-support-core-policy"
python 3_pipeline.py --section "income-support-extra-help"
python 3_pipeline.py --section "income-support-main-benefits"
python 3_pipeline.py --section "employment-and-training"
python 3_pipeline.py --section "social-housing"
python 3_pipeline.py --section "students"
python 3_pipeline.py --section "card-services"
python 3_pipeline.py --section "to-or-from-overseas"
python 3_pipeline.py --section "youth-service"
```

When `--section` is passed, the script filters `queue.json` to only entries where the `section` field matches. All other pipeline behaviour is identical — skip-if-exists still applies, and `run_log.json` accumulates across all batch runs.

Running without `--section` processes the full queue in one pass.

### Core Policy as a strict prerequisite

**Always run the `income-support-core-policy` batch first, before any other section.**

Core Policy contains the foundational `process` and `policy` entities most referenced by products and programmes in later sections. Running it first means:
- Process entity IDs (e.g. `application-for-benefit`, `reviews-and-appeals`) are already in the output when later pages reference them
- `application_process` fields in product and programme entities are more likely to resolve to a real ID rather than falling back to freetext
- Fewer review flags overall

Recommended run order:
1. `income-support-core-policy` ← **mandatory first**
2. `income-support-extra-help`
3. `income-support-main-benefits`
4. All remaining sections in any order

---

## Notes for Implementation

- The lookup table will be large. If prompt size becomes an issue, consider passing only the lookup entries relevant to the current section being processed.
- `run_log.json` should be appended to after each page, not written in bulk at the end — this way a crashed run is recoverable.
- Scripts 1 and 2 are fast and can be re-run freely. Script 3 is the expensive one (API calls + browser). Add a check at the start of each page loop: if `output/{type}/{id}.json` already exists, skip and log as `skipped`.
- Playwright's `page.inner_text()` on the main content element is preferable to full HTML dumps — cleaner input for Claude.