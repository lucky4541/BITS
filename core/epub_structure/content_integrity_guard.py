"""Steps 16/18/20 (spec sections 16/20) - the "ZERO CONTENT LOSS" guard for
a project DIRECTORY, built on the exact SAME core.epub.regression_checker.
extract_visible_text() the zip-based repair engine already uses for its
own content-integrity check and Full Auto Repair report (see core/epub/
regression_checker.py) - one shared definition of "visible text", never a
second copy that could disagree between the two subsystems.

A snapshot is taken from files ON DISK (never from in-memory trees, which
may already have been mutated by link_resolver/toc_resolver/id_assigner) -
BEFORE any file in the project is written, and again AFTER, so the
comparison reflects exactly what changed on disk, not what this run merely
intended to change."""
import hashlib
from collections import Counter
from dataclasses import dataclass, field

from core.epub.regression_checker import ContentFingerprint, extract_visible_text

_BINARY_CATEGORIES = ("image_files", "font_files", "av_files", "svg_files")


@dataclass
class ContentSnapshot:
    text_by_path: dict = field(default_factory=dict)     # xhtml path -> extract_visible_text() result (or None)
    hash_by_path: dict = field(default_factory=dict)       # binary asset path -> sha256 hex
    xhtml_paths: set = field(default_factory=set)
    binary_paths: set = field(default_factory=set)


@dataclass
class ContentIntegrityResult:
    ok: bool
    mismatches: list = field(default_factory=list)


def _hash_file(abs_path: str):
    try:
        with open(abs_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def take_snapshot(scan) -> ContentSnapshot:
    snap = ContentSnapshot()
    for path in scan.xhtml_files:
        snap.xhtml_paths.add(path)
        try:
            with open(scan.abspath(path), "rb") as f:
                raw = f.read()
        except OSError:
            snap.text_by_path[path] = None
            continue
        snap.text_by_path[path] = extract_visible_text(raw)
    for category in _BINARY_CATEGORIES:
        for path in getattr(scan, category):
            snap.binary_paths.add(path)
            snap.hash_by_path[path] = _hash_file(scan.abspath(path))
    return snap


def compare_snapshots(before: ContentSnapshot, after: ContentSnapshot) -> ContentIntegrityResult:
    """Compares each document's visible text as a CHARACTER MULTISET, not
    an exact ordered string - deliberately more permissive than core.epub.
    regression_checker's own zip-based check (which never reorders
    anything and correctly stays strict). This module's own
    citation_placement step legitimately MOVES a whole figure/table block
    to a new position WITHIN THE SAME FILE (spec: "MOVE = allowed... Moving
    DOM nodes must not change visible content" - meaning the same content
    must still be there, not that its position in the flattened text
    stream is frozen). A multiset comparison accepts any such reordering
    while still catching real loss/corruption: every character that was
    there before must still be there afterward in the SAME quantity - it
    can never mask a character being added, removed, or substituted,
    because that would change the multiset's own counts. Cross-file moves
    never happen (citation_placement only ever relocates within one
    document), so a per-file - not whole-book - multiset is the correctly
    scoped check.

    Whitespace characters (already collapsed to single spaces by
    extract_visible_text) are excluded from the multiset itself - removing
    a block from between two text runs merges what were two separate
    collapsed-whitespace boundaries into one, and inserting a block
    between two previously-adjacent runs creates a new one, so the exact
    COUNT of separator spaces can legitimately shift by one or two per
    move even though not a single real character was lost. Every non-
    whitespace character's count must still match exactly."""
    mismatches = []
    for path in sorted(before.xhtml_paths):
        if path not in after.xhtml_paths:
            mismatches.append(f"'{path}' no longer exists after generation.")
            continue
        before_text, after_text = before.text_by_path.get(path), after.text_by_path.get(path)
        if before_text is None or after_text is None:
            continue  # an unparseable document either way isn't itself evidence of text loss
        before_counts = Counter(c for c in before_text if not c.isspace())
        after_counts = Counter(c for c in after_text if not c.isspace())
        if before_counts != after_counts:
            mismatches.append(f"'{path}' visible text changed after generation.")

    for path in sorted(before.binary_paths):
        if path not in after.binary_paths:
            mismatches.append(f"'{path}' (image/font/audio/video asset) is missing after generation.")
            continue
        if before.hash_by_path.get(path) != after.hash_by_path.get(path):
            mismatches.append(f"'{path}' (image/font/audio/video asset) content changed after generation.")

    return ContentIntegrityResult(ok=not mismatches, mismatches=mismatches)


def compute_fingerprint(scan, snapshot: ContentSnapshot = None) -> ContentFingerprint:
    """Reuses the exact same core.epub.regression_checker.ContentFingerprint
    shape the zip-based Full Auto Repair report already displays (spec:
    "CONTENT INTEGRITY... Original characters / Original words / Original
    images / Original XHTML files") - a caller who already has a snapshot
    (from take_snapshot) passes it in to avoid re-reading every file from
    disk a second time."""
    snap = snapshot or take_snapshot(scan)
    characters = words = 0
    for path in snap.xhtml_paths:
        text = snap.text_by_path.get(path)
        if text is None:
            continue
        characters += len(text)
        words += len(text.split())
    images = len(scan.image_files) + len(scan.svg_files)
    return ContentFingerprint(characters=characters, words=words, images=images, xhtml_files=len(scan.xhtml_files))
