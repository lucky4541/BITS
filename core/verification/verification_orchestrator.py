"""Per-project verification driver: runs every zone through verification_
engine.verify_zone, assembles a VerificationSession, and applies fixes.

Applying a fix REUSES the existing, established "manual correction"
mechanism this app already has - core.zone_manager.ZoneManager.
set_zone_text(zone_id, text) (used today for CUPEPUB PageNum manual
entry): it marks zone.attributes["manual_text"]=True, which is exactly
what makes core.text_extractor._zone_prefers_stored_text trust the saved
text over a fresh (digital or OCR) re-extraction from then on, and it is
already undo/redo-aware. Nothing new is invented for "how a correction
actually changes what generates" - this module only computes WHAT the
corrected full text should be and hands it to that existing function."""
import re

from core.verification import verification_models as vm
from core.verification import verification_engine, word_join_verifier, inline_style, role_detector
from core.verification.verification_models import VerificationSession

_WORD_SPAN_RE = re.compile(r"\S+")


def _build_page_number_context(zone_manager) -> list:
    """[(page_number, zone_text), ...] for every PageNum-role zone in the
    project - built once per verification pass/recheck (same pattern as
    word_join_verifier.build_vocabulary_from_zones), giving role_
    detector.suggest_page_number_correction real sequential-neighbor
    evidence to check a candidate correction against."""
    return [(z.page, z.text or "") for z in zone_manager.zones.values()
            if role_detector.is_page_number_zone(z)]


def run_full_verification(pdf_document, zone_manager, progress_cb=None,
                           dictionary_additions=None, mode: str = "fast",
                           ocr_cache=None, ocr_settings=None) -> VerificationSession:
    """Iterates every zone in (page, serial) order - an approximate but
    sufficient reading-order sort for verification purposes; this module
    never reorders zones itself, only reads zone_manager.zones.
    `mode`: see verification_engine.verify_zone's own docstring - "fast"
    (default) never runs book-wide Deep analysis (spec: "Never make Deep
    Mode default")."""
    from core.text_extractor import clear_cache

    clear_cache()
    zones = sorted(zone_manager.zones.values(), key=lambda z: (z.page, z.serial or 0))
    vocabulary = word_join_verifier.build_vocabulary_from_zones(zone_manager.zones.values())
    page_number_context = _build_page_number_context(zone_manager)
    pdf_path = getattr(pdf_document, "path", "") or ""

    all_issues = []
    for index, zone in enumerate(zones):
        if progress_cb:
            progress_cb(index, len(zones), zone)
        try:
            page = pdf_document.get_page(zone.page)
            issues = verification_engine.verify_zone(
                page, zone, pdf_path, vocabulary, dictionary_additions=dictionary_additions, mode=mode,
                page_number_context=page_number_context, ocr_cache=ocr_cache,
                ocr_settings=ocr_settings)
            all_issues.extend(issues)
        except Exception:
            # One malformed zone must never abort verification for the
            # rest of the project - skip it, leave it unflagged.
            continue

    import datetime
    session = VerificationSession(issues=all_issues, dictionary_additions=list(dictionary_additions or []))
    session.last_run_at = datetime.datetime.now().isoformat(timespec="seconds")
    return session


def recheck_zone(session: VerificationSession, pdf_document, zone_manager, zone_id: str,
                  force_formatting_check: bool = False, mode: str = "fast",
                  ocr_cache=None, ocr_settings=None) -> list:
    """Re-runs every verifier for exactly one zone and replaces that
    zone's own entries in the session - the spec's own "verification
    loop": fix -> recheck -> the issue either disappears (resolved) or a
    fresh, up-to-date issue takes its place. This is the ONLY function in
    this module that actually re-verifies anything - apply_style/apply_
    fix/apply_manual_text below never call it themselves (spec: "style
    application must never trigger verification"); the caller (the
    Verification window's own explicit Recheck Selection/Zone/Page/Book
    actions) decides when to call this, always off the UI thread."""
    from core.text_extractor import clear_cache

    zone = zone_manager.zones.get(zone_id)
    if zone is None:
        return []
    clear_cache()
    page = pdf_document.get_page(zone.page)
    pdf_path = getattr(pdf_document, "path", "") or ""
    vocabulary = word_join_verifier.build_vocabulary_from_zones(zone_manager.zones.values())
    page_number_context = _build_page_number_context(zone_manager)
    issues = verification_engine.verify_zone(
        page, zone, pdf_path, vocabulary, dictionary_additions=session.dictionary_additions,
        force_formatting_check=force_formatting_check, mode=mode, page_number_context=page_number_context,
        ocr_cache=ocr_cache, ocr_settings=ocr_settings)
    session.replace_zone_issues(zone_id, issues)
    return issues


def recheck_page(session: VerificationSession, pdf_document, zone_manager, page_number: int,
                  mode: str = "fast", ocr_cache=None, ocr_settings=None) -> list:
    """Recheck Page (spec section 27) - every zone on exactly one page,
    never the whole book. Still O(zones on this page), not O(all zones)."""
    zone_ids = [z.zone_id for z in zone_manager.zones.values() if z.page == page_number]
    all_issues = []
    for zone_id in zone_ids:
        all_issues.extend(recheck_zone(session, pdf_document, zone_manager, zone_id, mode=mode,
                      ocr_cache=ocr_cache, ocr_settings=ocr_settings))
    return all_issues


def _word_spans(text: str) -> list:
    return [m.span() for m in _WORD_SPAN_RE.finditer(text)]


def _current_plain_text(pdf_document, zone_manager, zone_id: str) -> str:
    from core.text_extractor import extract_zone_formatted_text, strip_tags_to_plain

    zone = zone_manager.zones[zone_id]
    page = pdf_document.get_page(zone.page)
    return strip_tags_to_plain(extract_zone_formatted_text(page, zone))


def _mark_zone_issues_recheck_required(session: VerificationSession, zone_id: str):
    """Spec's own status lifecycle: OPEN -> Apply Fix -> RECHECK_REQUIRED
    -> Recheck Zone -> VERIFIED - applying a fix/edit/style NEVER jumps
    straight to VERIFIED/FIXED on its own; it marks the zone's open
    issues as awaiting an explicit recheck, which is a separate action
    (see recheck_zone/recheck_page above) the caller decides when to run,
    always off the UI thread."""
    for issue in session.issues_for_zone(zone_id):
        if issue.status in (vm.FIXED, vm.AUTO_FIXED, vm.VERIFIED, vm.IGNORED, vm.REJECTED):
            continue
        issue.status = vm.RECHECK_REQUIRED


def is_issue_stale(pdf_document, zone_manager, issue) -> bool:
    """True only when applying this issue's stored offsets to the zone's
    CURRENT text would be unsafe - i.e. the offsets no longer fall within
    the zone's current plain text at all, meaning something (a manual
    edit, an earlier fix) changed the zone's text length underneath this
    issue since it was detected (spec: "Apply Fix must be safe... if the
    text changed since detection, show Issue changed since verification;
    do not apply stale offsets to the wrong text").

    Deliberately NOT a fuzzy content-equality check against extracted_
    text/source_text - those fields record different things per issue
    type (e.g. a missing-footnote-callout SUPERSCRIPT issue's own source_
    text includes the preceding character, not just the char_range
    substring), so an exact-substring comparison would false-positive on
    issues that are actually still perfectly valid and refuse a
    legitimate fix. A bounds check is the safe, universally-correct
    signal instead: it can never wrongly refuse a valid fix, and it
    always refuses a fix whose stored offsets would otherwise silently
    slice the wrong text."""
    if not issue.zone_id:
        return False
    zone = zone_manager.zones.get(issue.zone_id)
    if zone is None:
        return True
    plain_text = _current_plain_text(pdf_document, zone_manager, issue.zone_id)
    if issue.char_range is not None:
        start, end = issue.char_range
        return not (0 <= start <= end <= len(plain_text))
    if issue.word_index is not None:
        return issue.word_index >= len(_word_spans(plain_text))
    return False


def apply_fix(session: VerificationSession, pdf_document, zone_manager, issue,
               mark_dirty=None) -> bool:
    """Applies issue.suggested_text (must already be set - a caller
    should never call this for an issue whose status isn't
    AUTO_FIX_AVAILABLE, or for a deliberate manual edit the operator
    typed themselves via apply_manual_text below). Marks the fixed issue
    FIXED and every OTHER open issue in the same zone RECHECK_REQUIRED -
    does NOT itself recheck (spec: recheck is always a separate, explicit
    step - see recheck_zone). Returns True on success. Returns False (and
    applies nothing) when is_issue_stale is true - callers wanting the
    specific "Issue changed since verification" message should check
    is_issue_stale themselves first (see gui/verification_window.py)."""
    if not issue.zone_id or issue.suggested_text is None:
        return False
    if is_issue_stale(pdf_document, zone_manager, issue):
        return False
    zone = zone_manager.zones.get(issue.zone_id)
    if zone is None:
        return False

    if issue.style_field:
        # A formatting fix (e.g. "Apply Superscript" for a missing body
        # footnote callout) is a STYLE change, not a text replacement -
        # dispatch to the exact same instant, local apply_style path a
        # manual toolbar click uses. Requires char_range (which zone/
        # word-index-only style issues would eventually be if generalized).
        if issue.char_range is None:
            return False
        start, end = issue.char_range
        apply_style(session, pdf_document, zone_manager, issue.zone_id, start, end,
                     [issue.style_field], mark_dirty=mark_dirty)
        issue.status = vm.FIXED
        return True

    plain_text = _current_plain_text(pdf_document, zone_manager, issue.zone_id)
    spans = _word_spans(plain_text)

    if issue.char_range is not None:
        start, end = issue.char_range
        new_text = plain_text[:start] + issue.suggested_text + plain_text[end:]
    elif issue.word_index is not None and issue.word_index < len(spans):
        # word-index-based fixes (content/word-join/word-split) may span
        # more than one CURRENTLY-EXTRACTED word (a split correction
        # collapses several words into one) - word_span_count (set by
        # verification_engine.py from the same finding that produced
        # this issue) says exactly how many.
        word_count = max(1, issue.word_span_count)
        last_index = min(issue.word_index + word_count, len(spans)) - 1
        start = spans[issue.word_index][0]
        end = spans[last_index][1]
        new_text = plain_text[:start] + issue.suggested_text + plain_text[end:]
    else:
        return False

    zone_manager.set_zone_text(issue.zone_id, new_text)
    issue.apply(new_text=issue.suggested_text)
    _mark_zone_issues_recheck_required(session, issue.zone_id)
    if mark_dirty:
        mark_dirty("verification fix applied")
    return True


def apply_manual_text(session: VerificationSession, pdf_document, zone_manager, zone_id: str,
                       new_text: str, mark_dirty=None):
    """The Verification window's own editable center pane calls this on
    a direct operator edit (not tied to any specific issue) - same
    underlying mechanism (set_zone_text). Marks this zone's issues
    RECHECK_REQUIRED; does not itself recheck (see apply_fix's own
    docstring for why)."""
    zone_manager.set_zone_text(zone_id, new_text)
    _mark_zone_issues_recheck_required(session, zone_id)
    if mark_dirty:
        mark_dirty("verification manual edit")


def apply_style(session: VerificationSession, pdf_document, zone_manager, zone_id: str,
                 start: int, end: int, styles, mark_dirty=None):
    """The Inline Style Editor's own Apply action - INSTANT and purely
    local (spec's own hard rule: "style application must NEVER trigger
    OCR or verification"). Only touches zone.attributes (core.
    verification.inline_style.add_override - see that module for how
    this actually reaches the generated XHTML) and this session's own
    in-memory issue statuses; never re-extracts the PDF, never re-runs
    any verifier, never touches pdf_document. `pdf_document`/`zone_
    manager` are accepted for call-signature symmetry with the other
    apply_* functions but pdf_document is intentionally unused here.

    `styles`: a dict {field_name: bool} to explicitly set OR unset a
    style (e.g. {"italic": False} to remove italic from already-italic
    text - see inline_style.py's own docstring for the real bug this
    fixes), or a bare list/tuple of field names (back-compat shorthand
    for "set these True", matching how this was called before that fix)."""
    zone = zone_manager.zones.get(zone_id)
    if zone is None:
        return
    inline_style.add_override(zone, start, end, styles)
    _mark_zone_issues_recheck_required(session, zone_id)
    if mark_dirty:
        mark_dirty("verification style override")


def ignore_issue(session: VerificationSession, issue, mark_dirty=None):
    issue.ignore()
    if mark_dirty:
        mark_dirty("verification issue ignored")


def add_to_dictionary(session: VerificationSession, word: str, mark_dirty=None):
    cleaned = (word or "").strip()
    if cleaned and cleaned not in session.dictionary_additions:
        session.dictionary_additions.append(cleaned)
    if mark_dirty:
        mark_dirty("verification dictionary updated")
