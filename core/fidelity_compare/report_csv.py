"""CSV report (spec section 58) - a flat, spreadsheet-friendly table of
every difference (spec section 61: "every item must be clickable" in the
GUI; the CSV export is the same data as a flat table for outside tools)."""
import csv

_COLUMNS = ["id", "type", "category", "severity", "confidence", "confidence_score",
            "comparison_stage", "result_kind", "original_page", "converted_page",
            "original_text", "converted_text", "explanation"]


def write(differences: list, out_path: str):
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for d in differences:
            writer.writerow(d.to_dict())
    return out_path
