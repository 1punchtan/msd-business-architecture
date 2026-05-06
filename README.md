# MSD MAP Business Architecture

A pipeline that extracts structured business architecture data from the [MSD Social Development Policy Guide (MAP)](https://www.workandincome.govt.nz/map/) and converts it to ArchiMate Open Exchange XML, ready to import into [Archi](https://www.archimatetool.com/) or any compliant modelling tool.

Disclaimer: This work is NOT officially endorsed by MSD. Only publicly available information was used, and no confidential information was shared. No rabbits were harmed in this project.

![Jobseeker Support](https://github.com/1punchtan/msd-business-architecture/blob/main/Jobseeker%20Support.png)

## What it does

Each page in the MAP wiki is scraped with headless Chromium, classified by two Claude API calls, and written as a typed JSON entity. A final script converts the full entity set to a single ArchiMate 3.0 XML file.

The pipeline produces:
- **127 entities** across 8 types (products, processes, policies, concepts, programmes, social housing, cards, actors)
- **977 relationships** derived from cross-references between entities
- A fully importable [`workspace/msd-map.xml`](https://github.com/1punchtan/msd-business-architecture/blob/main/workspace/msd-map.xml) in ArchiMate Open Exchange 3.0 format

## Entity types

| Type | ArchiMate type | Description |
|---|---|---|
| `product` | BusinessService | Benefits, payments, and allowances MSD delivers |
| `programme` | BusinessFunction | Funded initiatives and schemes |
| `policy` | Constraint | Rules, eligibility criteria, definitions |
| `process` | BusinessProcess | Steps, procedures, application flows |
| `social_housing` | BusinessService | Housing-specific services and assessments |
| `actor` | BusinessActor | Agents, providers, roles |
| `card` | BusinessService | Card-based entitlements |
| `concept` | BusinessObject | Definitions, statuses, and anything that doesn't fit elsewhere |

## Pipeline

Five scripts run in sequence against a shared `workspace/` directory:

| Script | Input | Output | Description |
|---|---|---|---|
| `1_build_queue.py` | `map-sitemap.md` | `workspace/queue.json` | Ordered list of 128 URLs to process, excluding Deskfile and Legislation |
| `2_build_lookup.py` | `map-sitemap.md` | `workspace/lookup.json` | Full name→ID map (262 entries) used by Claude to resolve relationships |
| `3_pipeline.py` | queue + lookup | `output/{type}/{id}.json`, `workspace/run_log.json` | Scrape, infer, format, write — two Claude API calls per page |
| `4_report.py` | `workspace/run_log.json` | `workspace/review.json` | Groups flagged entries: failed scrapes, low confidence, needs review |
| `5_to_archimate.py` | `output/` tree | `workspace/msd-map.xml` | Converts JSON entities to ArchiMate Open Exchange 3.0 XML |

## Setup

**Requirements:** Python 3.10+, an Anthropic API key.

```bash
python -m venv .venv
source .venv/bin/activate
pip install playwright anthropic python-dotenv lxml
playwright install chromium
```

Create a `.env` file:

```
ANTHROPIC_API_KEY=your_key_here
CALL1_MODEL=claude-sonnet-4-6
CALL2_MODEL=claude-haiku-4-5-20251001
```

## Usage

```bash
# Build queue and lookup (fast, safe to re-run)
python 1_build_queue.py
python 2_build_lookup.py

# Run the pipeline — Core Policy must come first
python 3_pipeline.py --section income-support-core-policy
python 3_pipeline.py --section income-support-extra-help
python 3_pipeline.py --section income-support-main-benefits
python 3_pipeline.py --section employment-and-training
python 3_pipeline.py --section social-housing
python 3_pipeline.py --section students
python 3_pipeline.py --section card-services
python 3_pipeline.py --section to-or-from-overseas
python 3_pipeline.py --section youth-service

# Or run all sections in one pass
python 3_pipeline.py

# Generate the review report
python 4_report.py

# Convert to ArchiMate XML
python 5_to_archimate.py
```

The pipeline is crash-recoverable. Re-running any section skips pages that already have an output file.

## Output structure

```
workspace/
├── queue.json       # Ordered URL list
├── lookup.json      # name → ID map
├── run_log.json     # Per-page result log
└── msd-map.xml      # ArchiMate Open Exchange output

output/
├── products/        # Benefits, payments, allowances
├── programmes/      # Funded initiatives
├── policies/        # Rules and eligibility criteria
├── processes/       # Application and assessment procedures
├── social_housing/  # Housing services
├── actors/          # Agents and roles
├── cards/           # Card-based entitlements
└── concepts/        # Definitions and reference data
```

## Model strategy

Call 1 (Infer) uses `claude-sonnet-4-6` — entity classification and relationship resolution against a 262-entry lookup requires strong reasoning. Call 2 (Format) uses `claude-haiku-4-5-20251001` — purely a schema-formatting task that runs at significantly lower cost. The lookup JSON is prompt-cached across all Call 1 requests in a session to avoid re-tokenising it on every page.

## License

MIT — see [LICENSE](LICENSE).

---

*Built with the help of [Claude Code](https://claude.ai/code) by Anthropic.*
