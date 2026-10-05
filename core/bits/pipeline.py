"""Generate BITS / JATS XML for a zoned PDF:

    zones -> core.xml_generator (reading order, hierarchy, figures, tables,
             lists, references, page targets, image assets)
          -> core.bits.structure (BITS 2.2 book / JATS 1.4 article)
          -> core.bits.autofix (data-safe DTD repairs)
          -> DTD validation -> <prefix>.xml with the DOCTYPE + report

The words of the document are compared after every step - a step that
would change them is reported as a failure (the file is still written, so
nothing is lost, and the report says where)."""
import os
from dataclasses import dataclass, field

from lxml import etree

from core import xml_generator
from core.bits import autofix, dtd, structure


@dataclass
class BitsResult:
    kind: str
    output_path: str = ""
    report_path: str = ""
    counters: dict = field(default_factory=dict)
    asset_counters: dict = field(default_factory=dict)
    dtd_path: str = ""
    dtd_available: bool = False
    errors_before: list = field(default_factory=list)
    errors_after: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    removed_attributes: list = field(default_factory=list)
    words_generated: int = 0
    words_output: int = 0
    text_preserved: bool = True
    notes: list = field(default_factory=list)

    @property
    def valid(self):
        return self.dtd_available and not self.errors_after

    @property
    def status(self):
        if not self.text_preserved:
            return "FAIL - text changed"
        if not self.dtd_available:
            return "NOT VALIDATED - DTD not installed"
        return "VALID" if self.valid else "NEEDS REVIEW"

    def summary(self):
        lines = [f"{self.kind} XML: {self.status}", f"Output: {self.output_path}"]
        if self.dtd_available:
            lines.append(f"DTD: {os.path.basename(self.dtd_path)}")
            lines.append(f"DTD errors: {len(self.errors_before)} before auto-fix, {len(self.errors_after)} after "
                         f"({len(self.fixes)} automatic fix(es))")
        lines.append(f"Words: {self.words_generated} generated, {self.words_output} in the output"
                     + ("" if self.text_preserved else "  <-- DIFFERENT"))
        lines += self.notes
        return "\n".join(lines)


def kind_of(profile: dict) -> str:
    out = (profile or {}).get("output") or (profile or {}).get("name") or "BITS"
    return "JATS" if "jats" in out.lower() else "BITS"


def generate(zone_manager, pdf_document, kind: str, output_path: str, assets_dir: str, prefix: str = "document",
             settings: dict = None, **gen_options) -> BitsResult:
    settings = settings or {}
    kind = kind.upper()
    res = BitsResult(kind=kind, output_path=output_path)
    # 1. the generator
    gen = xml_generator.XMLGenerator(zone_manager, pdf_document, assets_dir, prefix,
                                     gen_options.get("jpeg_quality", 95), "book", gen_options.get("image_dpi"),
                                     gen_options.get("remove_image_background", False), split_back_matter=False)
    gen_root = gen.generate_tree()
    res.counters, res.asset_counters = dict(gen.counters), dict(gen.assets.counters)
    words0 = structure.content_signature(gen_root)
    res.words_generated = len(words0)
    # 2. structure
    root = structure.build(kind, gen_root, settings, prefix=prefix)
    words1 = structure.content_signature(root)
    lost = structure.lost_words(words0, words1 + list(structure.DECLARED))
    if lost:
        res.text_preserved = False
        res.notes.append(f"TEXT LOST in the structure step: {' '.join(lost[:30])}")
    # 3. DTD repairs + validation
    res.dtd_available = dtd.available(kind, settings)
    if res.dtd_available:
        model = dtd.load(kind, settings)
        res.dtd_path = model.source
        rep = autofix.fix(root, model, id_prefix=prefix)
        res.errors_before, res.errors_after = rep.errors_before, rep.errors_after
        res.fixes, res.removed_attributes = rep.fixes, rep.removed_attributes
        res.notes += rep.reverted
    else:
        res.notes.append(f"The {kind} DTD is not installed - the XML was not validated. "
                         + ("Put the BITS 2.2 DTD files in profiles/BITS/dtd/." if kind == "BITS" else ""))
    words2 = structure.content_signature(root)
    res.words_output = len(words2)
    lost = structure.lost_words(words0, words2 + list(structure.DECLARED))
    if structure.DECLARED:
        res.notes.append("Turned into markup (kept as attributes / elements): " + " ".join(structure.DECLARED[:30]))
    if lost and res.text_preserved:
        res.text_preserved = False
        res.notes.append(f"TEXT LOST in the auto-fix step: {' '.join(lost[:30])}")
    added = structure.added_words(words0, words2)
    if added:
        res.notes.append(f"Added by the structure (labels, metadata, placeholders): {' '.join(added[:30])}")
    # 4. write
    write(root, output_path, kind, settings, model=dtd.load(kind, settings) if res.dtd_available else None)
    res.report_path = os.path.splitext(output_path)[0] + "_validation.txt"
    write_report(res, res.report_path)
    return res


MIXED_FALLBACK = {"p", "title", "subtitle", "label", "book-title", "article-title", "caption", "td", "th",
                  "mixed-citation", "term", "kwd", "aff", "string-name", "publisher-name", "copyright-statement",
                  "preformat", "verse-line", "speaker", "named-content", "isbn", "edition", "corresp", "given-names",
                  "surname", "journal-title", "attrib", "def"}


def indent(el, model=None, level=0, space="  "):
    """Pretty-print element-only containers; elements that hold text
    (mixed content: p, title, ...) are never touched, so no whitespace is
    added inside text."""
    def mixed(e):
        if model is not None:
            d = model.elements.get(e.tag)
            if d is not None:
                return d.type in ("mixed", "any")
        return e.tag in MIXED_FALLBACK
    if not isinstance(el.tag, str) or mixed(el) or (el.text or "").strip() or \
            any((c.tail or "").strip() for c in el):
        return
    kids = [c for c in el if isinstance(c.tag, str) or True]
    if not kids:
        return
    pad = "\n" + space * (level + 1)
    el.text = pad
    for c in kids:
        indent(c, model, level + 1, space)
        c.tail = pad
    kids[-1].tail = "\n" + space * level


def write(root, path, kind, settings=None, model=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    doctype = dtd.doctype(kind, settings)
    indent(root, model)
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", doctype=doctype or None)
    with open(path, "wb") as f:
        f.write(data)
        f.write(b"\n")


def write_report(res: BitsResult, path: str):
    lines = [res.summary(), ""]
    if res.fixes:
        lines.append("AUTOMATIC FIXES")
        lines += [f"  [{rule}] {where}: {detail}" for rule, where, detail in res.fixes]
        lines.append("")
    if res.errors_after:
        lines.append("REMAINING DTD ERRORS (manual review)")
        lines += [f"  {e}" for e in res.errors_after]
        lines.append("")
    if res.removed_attributes:
        lines.append("ATTRIBUTES REMOVED (not allowed by the DTD)")
        lines += [f"  {where}: {name}=\"{value}\"" for where, name, value in res.removed_attributes]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _word_diff(step, a, b):
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            parts.append(f"{op}: '{' '.join(a[i1:i2])[:60]}' -> '{' '.join(b[j1:j2])[:60]}'")
    return f"TEXT CHANGED in {step}: " + " | ".join(parts[:5])


def validate_file(path: str, kind: str = None, settings: dict = None):
    """(kind, errors) for an existing BITS / JATS file."""
    root = etree.parse(path).getroot()
    kind = kind or ("BITS" if root.tag == "book" else "JATS")
    model = dtd.load(kind, settings)
    return kind, model.validate(root)


def fix_file(path: str, out_path: str = None, kind: str = None, settings: dict = None):
    """Validate & auto-fix an existing BITS / JATS file (never overwrites
    the input unless out_path is the same path)."""
    tree = etree.parse(path)
    root = tree.getroot()
    kind = kind or ("BITS" if root.tag == "book" else "JATS")
    model = dtd.load(kind, settings)
    before = structure.content_signature(root)
    rep = autofix.fix(root, model)
    if structure.content_signature(root) != before:
        raise RuntimeError("auto-fix would change the text - not written")
    out_path = out_path or os.path.splitext(path)[0] + ".fixed.xml"
    write(root, out_path, kind, settings, model)
    return out_path, rep
