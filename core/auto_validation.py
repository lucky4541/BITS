"""Detailed, multi-stage VALIDATION REPORT for an auto-zoned / auto-tagged
project (BITS / JATS).

Stages:
  1. text integrity        - page text not covered by any zone; zones whose
                             text differs from the PDF characters under them
  2. layout / zones        - existing core.validation checks (tags, bboxes,
                             overlaps, parents) + empty text zones
  3. reading order         - gaps / duplicates per page; zones out of
                             layout order
  4. formatting            - malformed / crossing / empty formatting ranges,
                             whitespace-only decorations
  5. tag structure         - tags unknown to the profile, NEEDS REVIEW and
                             low-confidence decisions
  6. parent / child        - nested zones the DTD forbids
  7. attributes            - DTD-required attributes on generated elements
  8. DTD                   - full DTD validation of the generated tree (when a
                             DTD for its root exists)
  9. reference patterns    - successions the reference corpus never shows,
                             Mapping.xml family members with no family
                             neighbour
"""
import json
import re
from dataclasses import dataclass, field

from core import validation as base_validation
from core.formatting_ranges import parse_ranges

STAGES = ["text", "zones", "reading_order", "formatting", "tags", "parent_child", "attributes", "dtd", "patterns"]
STAGE_TITLES = {
    "text": "1. Text integrity", "zones": "2. Layout / zone validation", "reading_order": "3. Reading order",
    "formatting": "4. Formatting", "tags": "5. Tag structure", "parent_child": "6. Parent / child",
    "attributes": "7. Attributes", "dtd": "8. DTD validation", "patterns": "9. Reference patterns",
}
_WS_RE = re.compile(r"\s+")


@dataclass
class Issue:
    stage: str
    level: str          # "error" | "warning" | "info"
    message: str
    zone_id: str = None
    page: int = None

    def to_dict(self):
        return {"stage": self.stage, "level": self.level, "message": self.message,
                "zone_id": self.zone_id, "page": self.page}


@dataclass
class ValidationReport:
    issues: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, stage, level, message, zone=None, page=None):
        self.issues.append(Issue(stage, level, message, getattr(zone, "zone_id", None),
                                 page if page is not None else getattr(zone, "page", None)))

    def count(self, level=None, stage=None) -> int:
        return sum(1 for i in self.issues if (level is None or i.level == level) and (stage is None or i.stage == stage))

    @property
    def ok(self) -> bool:
        return self.count("error") == 0

    def to_text(self) -> str:
        lines = ["VALIDATION REPORT", "=" * 60]
        lines.append(f"Errors: {self.count('error')}   Warnings: {self.count('warning')}   Info: {self.count('info')}")
        for k, v in self.stats.items():
            lines.append(f"{k}: {v}")
        for stage in STAGES:
            items = [i for i in self.issues if i.stage == stage]
            lines.append("")
            lines.append(f"{STAGE_TITLES[stage]}  -  {'OK' if not any(i.level == 'error' for i in items) else 'FAILED'}"
                         f" ({len(items)} item(s))")
            for i in items:
                where = f"p{i.page}" if i.page else ""
                if i.zone_id:
                    where += f" {i.zone_id}"
                lines.append(f"  [{i.level.upper():7s}] {where:14s} {i.message}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({"stats": self.stats, "issues": [i.to_dict() for i in self.issues]}, indent=2,
                          ensure_ascii=False)


def _norm(text: str) -> str:
    return _WS_RE.sub("", text or "")


def _plain(zone_text: str) -> str:
    return parse_ranges(zone_text or "")[0]


def run(zone_manager, pdf_document, profile: dict, knowledge=None, generated_root=None, pages=None,
        thresholds=None) -> ValidationReport:
    report = ValidationReport()
    thresholds = thresholds or {"high": 90, "medium": 75}
    zones = list(zone_manager.zones.values())
    if pages is not None:
        pages = set(pages)
        zones = [z for z in zones if z.page in pages]
    by_page = {}
    for z in zones:
        by_page.setdefault(z.page, []).append(z)
    page_marker_tags = set(profile.get("page_marker_tags") or [])
    known_tags = {b["tag"] for b in profile.get("tag_buttons", [])}
    image_tags = {b["tag"] for b in profile.get("tag_buttons", []) if b.get("attrs", {}).get("asset_kind")}
    report.stats["zones"] = len(zones)
    report.stats["pages"] = len(by_page)

    # ---------------------------------------------------- 1. text integrity
    if pdf_document is not None:
        from auto_zoning.layout_engine import extract_page_lines, _signature
        for page_no in sorted(by_page):
            try:
                page = pdf_document.get_page(page_no)
                lines = extract_page_lines(page, with_decorations=False)
            except Exception as e:  # noqa: BLE001
                report.add("text", "warning", f"could not read page text: {e}", page=page_no)
                continue
            page_zones = by_page[page_no]
            uncovered = []
            for li in lines:
                cx, cy = (li.bbox[0] + li.bbox[2]) / 2, (li.bbox[1] + li.bbox[3]) / 2
                if not any(z.bbox[0] - 1 <= cx <= z.bbox[2] + 1 and z.bbox[1] - 1 <= cy <= z.bbox[3] + 1
                           for z in page_zones):
                    uncovered.append(li.text)
            if uncovered:
                report.add("text", "warning", f"{len(uncovered)} text line(s) not inside any zone "
                           f"(running heads are expected here): {uncovered[:3]}", page=page_no)
            for z in page_zones:
                if z.tag in image_tags or z.attributes.get("manual_text") or z.attributes.get("source") == "ocr":
                    continue
                try:
                    from core import text_extractor
                    pdf_text = text_extractor.extract_plain_text(page, z.bbox)
                except Exception:
                    continue
                if _norm(pdf_text) and _norm(_plain(z.text)) and _norm(pdf_text) != _norm(_plain(z.text)):
                    report.add("text", "warning", "zone text differs from the PDF characters inside its box "
                               "(re-extract or check manual edits)", zone=z)

    # ---------------------------------------------------- 2. zones
    for msg in base_validation.validate(zone_manager, page_marker_tags=page_marker_tags,
                                        footnote_flow_tags=profile.get("footnote_flow_tags"),
                                        non_flow_tags=profile.get("non_flow_tags")):
        report.add("zones", "error", msg)
    for z in zones:
        if z.tag not in image_tags and not _plain(z.text).strip() and not z.children:
            report.add("zones", "warning", f"<{z.tag}> zone has no text", zone=z)

    # ---------------------------------------------------- 3. reading order
    for page_no, page_zones in sorted(by_page.items()):
        counting = [z for z in page_zones if zone_manager._counts_in_reading_order(z)]
        serials = sorted(z.serial for z in counting if isinstance(z.serial, int))
        if serials != list(range(1, len(counting) + 1)):
            report.add("reading_order", "error", f"reading order is not a clean 1..{len(counting)} sequence: "
                       f"{serials[:12]}", page=page_no)
        markers = [z for z in counting if z.tag in page_marker_tags]
        for m in markers:
            if m.serial not in (1, None):
                report.add("reading_order", "warning", "page-number zone is not first in reading order", zone=m)
        ordered = sorted((z for z in counting if z.tag not in page_marker_tags), key=lambda z: z.serial or 0)
        for a, b in zip(ordered, ordered[1:]):
            same_column = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]) > 0.3 * min(
                a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
            if same_column and b.bbox[3] < a.bbox[1] - 2 and not a.attributes.get("manual_ro") \
                    and not b.attributes.get("manual_ro"):
                report.add("reading_order", "warning", f"{b.zone_id} is read after {a.zone_id} but sits above it "
                           "in the same column", zone=b)

    # ---------------------------------------------------- 4. formatting
    for z in zones:
        if not z.text or "<" not in z.text:
            continue
        plain, ranges, issues = parse_ranges(z.text)
        for msg in issues:
            report.add("formatting", "error", msg, zone=z)
        for r in ranges:
            seg = plain[r["start"]:r["end"]]
            if r["end"] <= r["start"]:
                report.add("formatting", "warning", f"empty <{r['style']}> range", zone=z)
            elif not seg.strip():
                report.add("formatting", "warning", f"<{r['style']}> covers only whitespace", zone=z)
    report.stats["formatting_ranges"] = sum(len(z.formatting_ranges) for z in zones if z.text)

    # ---------------------------------------------------- 5. tags
    review = 0
    for z in zones:
        if z.tag not in known_tags:
            report.add("tags", "error", f"tag <{z.tag}> is not defined by profile {profile.get('name')}", zone=z)
        if z.needs_review:
            review += 1
            report.add("tags", "warning", "NEEDS REVIEW: " + "; ".join(z.attributes.get("review_reasons", [])),
                       zone=z)
        conf = z.confidence
        if conf is not None and not z.manual_override and conf < thresholds.get("medium", 75) and not z.needs_review:
            report.add("tags", "info", f"low confidence {conf:.0f}%", zone=z)
    report.stats["needs_review"] = review

    # ---------------------------------------------------- 6/7/8 DTD
    dtds = list(getattr(knowledge, "dtds", []) or [])
    for z in zones:
        if z.parent_id and z.parent_id in zone_manager.zones:
            parent = zone_manager.zones[z.parent_id]
            for dtd in dtds:
                allowed = dtd.allows_child(parent.tag, z.tag)
                if allowed is False:
                    report.add("parent_child", "error", f"<{z.tag}> is not allowed inside <{parent.tag}> "
                               f"({dtd.source})", zone=z)
    if generated_root is not None and dtds:
        root_name = generated_root.tag.split("}")[-1] if isinstance(generated_root.tag, str) else None
        for dtd in dtds:
            if not dtd.has_element(root_name):
                report.add("dtd", "info", f"{dtd.source} does not declare <{root_name}> - not applicable")
                continue
            for msg in dtd.validate(generated_root):
                report.add("dtd", "error", msg)
            for el in generated_root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in dtd.required_attributes(el.tag):
                    if attr.name not in el.attrib:
                        report.add("attributes", "error", f"<{el.tag}> is missing required attribute "
                                   f"'{attr.name}'")
    elif not dtds:
        report.add("dtd", "info", "no DTD found for this profile (profiles/<profile>/dtd) - DTD stages skipped")

    # ---------------------------------------------------- 9. patterns
    if knowledge is not None:
        for page_no, page_zones in sorted(by_page.items()):
            ordered = sorted((z for z in page_zones if zone_manager._counts_in_reading_order(z)),
                             key=lambda z: z.serial or 0)
            for i, z in enumerate(ordered):
                prev_tag = ordered[i - 1].tag if i > 0 else None
                next_tag = ordered[i + 1].tag if i + 1 < len(ordered) else None
                verdict = knowledge.structural_verdict(z.tag, prev_tag=prev_tag, next_tag=next_tag)
                for reason in verdict.reasons:
                    if "never" in reason or "no image" in reason or "may not" in reason:
                        report.add("patterns", "warning", reason, zone=z)
    return report
