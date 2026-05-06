import json
import glob
from pathlib import Path
from lxml import etree

WORKSPACE = Path("workspace")
OUTPUT = Path("output")
ARCHIMATE_NS = "http://www.opengroup.org/xsd/archimate/3.0/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

REL_TYPE_ABBREV = {
    "Serving": "serving",
    "Triggering": "triggering",
    "Influence": "influence",
    "Association": "association",
}


def infer_rel_type(src_archimate: str, related_category: str) -> str:
    rules = {
        ("BusinessService", "processes"): "Serving",
        ("BusinessService", "policies"): "Influence",
        ("BusinessProcess", "products"): "Serving",
        ("BusinessProcess", "cards"): "Serving",
        ("BusinessProcess", "social_housing"): "Serving",
        ("BusinessProcess", "policies"): "Influence",
        ("BusinessFunction", "processes"): "Triggering",
        ("BusinessFunction", "policies"): "Influence",
        ("Constraint", "*"): "Influence",
    }
    if related_category == "concepts":
        return "Association"
    key = (src_archimate, related_category)
    if key in rules:
        return rules[key]
    if (src_archimate, "*") in rules:
        return rules[(src_archimate, "*")]
    return "Association"


def main():
    log_path = WORKSPACE / "run_log.json"
    failed_ids = set()
    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
        failed_ids = {e["id"] for e in log if e.get("status") == "failed" and e.get("id")}

    json_files = glob.glob(str(OUTPUT / "**" / "*.json"), recursive=True)

    nsmap = {None: ARCHIMATE_NS, "xsi": XSI_NS}
    model = etree.Element("model", nsmap=nsmap)
    model.set("{%s}schemaLocation" % XSI_NS,
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
        el.set("{%s}type" % XSI_NS, atype)
        etree.SubElement(el, "name").text = data.get("name", "")
        desc = data.get("description", "").strip()
        if desc:
            etree.SubElement(el, "documentation").text = desc

        entities.append(data)

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
                rel_el.set("{%s}type" % XSI_NS, rel_type)
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
