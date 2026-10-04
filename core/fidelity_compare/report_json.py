"""JSON report (spec section 58) - the full, machine-readable dump: every
Difference exactly as produced, plus the measured score report."""
import json


def write(differences: list, scores, out_path: str):
    payload = {
        "scores": scores.to_dict(),
        "difference_count": len(differences),
        "differences": [d.to_dict() for d in differences],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path
