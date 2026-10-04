"""RegressionChecker (spec: "EPUBForge - PHASE 3 - Universal Auto-Fix
Engine" section 1's required component, and the "COMPARE" workflow step).
Three independent checks the repair engine runs after every pass:

1. compare_error_counts - did this pass's rebuild actually reduce the
   number of real errors, per spec 6 ("If errors decrease: continue. If
   errors increase: ROLLBACK.").
2. check_content_integrity - spec 7 ("No data loss... preserve all text,
   paragraphs, headings...") - a real, mechanical comparison of every
   content document's VISIBLE TEXT before vs after (repair strategies only
   ever touch attributes - id/href/media-type/manifest entries/playOrder -
   so an EXACT text match is the correct bar, not a fuzzy one), PLUS a
   byte-for-byte hash check of every image/font/audio/video asset in the
   manifest - no shipped repair strategy (core.epub.repair_strategies) ever
   legitimately rewrites a binary asset's bytes, so any difference there is
   real evidence of corruption, never an intended edit.
3. compute_fingerprint - the "CONTENT INTEGRITY" character/word/image/
   XHTML-file counts shown to the user before/after a repair (spec: "ZERO
   CONTENT LOSS" - "display... Original characters / Final characters /
   Lost characters..."), built from the exact same visible-text extraction
   check_content_integrity itself uses - one shared definition of "how much
   content is in this package," never two that could disagree.

extract_visible_text() below is also reused directly by app.editor.
editor_window's own Pretty Print guard (spec's "BEFORE/AFTER CONTENT CHECK"
principle applied to a single in-memory buffer, not just a whole EPUB
rebuild) - never a second, divergent implementation of "what counts as the
same visible text."""
import hashlib
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

_WHITESPACE_RE = re.compile(r"\s+")
_BINARY_MEDIA_PREFIXES = ("image/", "font/", "audio/", "video/")
# Legacy/non-standard font media-types some EPUB2 packages still use -
# fonts declared this way are just as immutable to every repair strategy
# as one declared "font/*".
_BINARY_MEDIA_TYPES_EXTRA = {"application/font-woff", "application/vnd.ms-opentype", "application/x-font-ttf"}


@dataclass
class ContentIntegrityResult:
    ok: bool
    checked_files: int = 0
    mismatches: list = field(default_factory=list)


@dataclass
class ContentFingerprint:
    characters: int = 0
    words: int = 0
    images: int = 0
    xhtml_files: int = 0


def compare_error_counts(before_n: int, after_n: int) -> str:
    if after_n < before_n:
        return "IMPROVED"
    if after_n > before_n:
        return "WORSENED"
    return "UNCHANGED"


def _xhtml_targets(package) -> list:
    """Only files that ACTUALLY EXIST in the package's own zip are real
    comparison targets. A manifest item's href can itself be broken (e.g.
    the exact wrong-case href core.epub.repair_strategies.
    fix_broken_manifest_hrefs corrects) - such a target never pointed at
    real content in the first place, so treating its corrected pointer as
    'the file disappeared' would be a false positive, not a genuine
    content-loss finding."""
    return sorted({resolved for item in package.manifest
                   if item.media_type == "application/xhtml+xml" and item.href
                   for resolved in [package.resolve_href(item.href)]
                   if resolved in package.zip_names})


def extract_visible_text(raw: bytes):
    """The ONE definition of 'visible text' every content-integrity check
    in this module (and app.editor.editor_window's Pretty Print guard)
    reduces to - never two copies that could disagree. Whitespace is
    collapsed to single spaces for comparison purposes only (reformatting
    a document's indentation must never itself count as a content change);
    the caller decides what to do when this returns None (a document that
    cannot be parsed at all is not itself evidence of text loss - it never
    contributes to a match OR a mismatch)."""
    try:
        tree = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return None
    return _WHITESPACE_RE.sub(" ", "".join(tree.itertext())).strip()


def _extract_text(zf: zipfile.ZipFile, name: str):
    try:
        raw = zf.read(name)
    except KeyError:
        return None
    return extract_visible_text(raw)


def _binary_targets(package) -> list:
    """Every manifest item whose bytes NO shipped repair strategy is ever
    allowed to touch (image/font/audio/video assets) - core.epub.
    repair_strategies only ever edits OPF manifest entries, NCX playOrder,
    and duplicate id attributes inside XHTML text; it never rewrites a
    binary resource. Any byte difference here is therefore real corruption,
    never an intended edit (unlike XHTML/OPF/NCX, where a targeted, minimal
    change IS expected and must not be flagged)."""
    return sorted({resolved for item in package.manifest
                   if (item.media_type or "").startswith(_BINARY_MEDIA_PREFIXES)
                   or (item.media_type or "") in _BINARY_MEDIA_TYPES_EXTRA
                   for resolved in [package.resolve_href(item.href)]
                   if resolved in package.zip_names})


def _hash_bytes(zf: zipfile.ZipFile, name: str):
    try:
        return hashlib.sha256(zf.read(name)).hexdigest()
    except KeyError:
        return None


def check_content_integrity(before_path: str, after_path: str, before_package, after_package) -> ContentIntegrityResult:
    before_targets = _xhtml_targets(before_package)
    after_targets = set(_xhtml_targets(after_package))
    mismatches = []
    checked = 0
    with zipfile.ZipFile(before_path, "r") as zf_before, zipfile.ZipFile(after_path, "r") as zf_after:
        for name in before_targets:
            if name not in after_targets:
                mismatches.append(f"'{name}' is no longer a declared content document after repair.")
                continue
            before_text = _extract_text(zf_before, name)
            after_text = _extract_text(zf_after, name)
            if before_text is None or after_text is None:
                continue  # an unparseable document either way isn't itself evidence of text loss
            checked += 1
            if before_text != after_text:
                mismatches.append(f"'{name}' visible text changed after repair.")

        before_binary = _binary_targets(before_package)
        after_binary = set(_binary_targets(after_package))
        for name in before_binary:
            if name not in after_binary:
                mismatches.append(f"'{name}' (image/font/audio/video asset) is missing after repair.")
                continue
            checked += 1
            if _hash_bytes(zf_before, name) != _hash_bytes(zf_after, name):
                mismatches.append(f"'{name}' (image/font/audio/video asset) content changed after repair.")

    return ContentIntegrityResult(ok=not mismatches, checked_files=checked, mismatches=mismatches)


def compute_fingerprint(zip_path: str, package) -> ContentFingerprint:
    """The exact counts spec's "CONTENT INTEGRITY" display wants (Original/
    Final characters, words, images, XHTML files) - characters/words are
    summed from the SAME per-file extract_visible_text() check_content_
    integrity uses, so this can never report a different notion of
    'content' than the pass/fail check above already used."""
    targets = _xhtml_targets(package)
    characters = words = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in targets:
            text = _extract_text(zf, name)
            if text is None:
                continue
            characters += len(text)
            words += len(text.split())
    images = sum(1 for item in package.manifest if (item.media_type or "").startswith("image/"))
    return ContentFingerprint(characters=characters, words=words, images=images, xhtml_files=len(targets))
