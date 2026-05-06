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
