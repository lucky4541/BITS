"""Runs every verifier for ONE zone and returns a list[VerificationIssue].

The single policy decision this module makes is WHEN to fetch a second,
independent OCR reading of a zone's own crop to use as comparison
evidence for content_verifier/word_join_verifier/unicode_verifier (none
of those can find a content mismatch with only one text source to look
at). Per the spec's own explicit rules:

  - "For digital pages: native PDF extraction remains primary" and
    "Do not OCR every digital page unnecessarily" -> a cleanly DIGITAL
    page never fetches a second OCR reading automatically.
  - "For scanned pages: OCR remains primary" -> the zone's own text
    already IS the OCR reading; there is no second, different source to
    compare it against, so content/word-join/unicode-char checks are
    skipped for such zones (nothing to gain from OCR'ing the same crop
    again with the same engine).
  - "For mixed/searchable pages: use native extraction + OCR as
    evidence" -> exactly the case where a fresh OCR reading is fetched:
    the zone is using native/digital text but the page itself is not
    cleanly digital (MIXED/SCANNED/OCR_REQUIRED/UNKNOWN classification,
    or genuinely image-dominated).

This mirrors formatting_verifier.has_independent_evidence's own reasoning
exactly (same "narrow, uncommon category" cost profile) and reuses the
identical crop+recognize pattern core.ocr.style_detector.
_populate_empty_scanned_zone_text already established - the only
difference is this NEVER writes back to zone.text (evidence-gathering
only, never mutates the zone)."""
from core.fidelity_compare.difference_model import Difference, next_id, bbox_dict
from core.fidelity_compare import confidence_engine

import re

from core.verification import verification_models as vm
from core.verification import content_verifier, word_join_verifier, unicode_verifier
from core.verification import spell_verifier, grammar_verifier, formatting_verifier
from core.verification import alignment_verifier, role_detector

_MISSING_CALLOUT_RE = re.compile(r"([.!?])(\d{1,3})(?!\d)")

OCR_EVIDENCE_DPI = 300


def _fetch_ocr_evidence(page, zone, language: str = "en", cache=None, ocr_settings=None) -> str:
    """Read OCR evidence from the project's prepared page cache only.

    Verification never starts a fresh zone-crop OCR call. If the separate OCR
    preparation step has not produced a cached result for this page/settings,
    no OCR evidence is used and verification continues with the native data.
    """
    try:
        from core.ocr import ocr_service
        settings = dict(ocr_settings or {})
        result = ocr_service.get_cached_ocr_for_page(
            page.parent, int(page.number) + 1,
            engine_name=settings.get("engine", "PaddleOCR"),
            language=settings.get("language", language or "en"),
            dpi=int(settings.get("dpi", OCR_EVIDENCE_DPI)),
            preprocessing_settings=settings.get("preprocessing", {}),
            cache=cache)
        return ocr_service.cached_ocr_text_for_bbox(result, zone.bbox)
    except Exception:
        return ""


def should_fetch_ocr_evidence(page, zone) -> bool:
    from core.text_extractor import _zone_wants_image_formatting_detection
    from core.ocr.style_detector import page_is_image_dominated
    from core.ocr.text_quality import analyze_page_object, classify_page

    if zone.attributes.get("manual_text"):
        return False
    if _zone_wants_image_formatting_detection(page, zone):
        return False  # already OCR/visual-sourced - re-OCR of the same crop tells us nothing new
    metrics = analyze_page_object(page, zone.page)
    if classify_page(metrics) in ("SCANNED", "MIXED", "OCR_REQUIRED", "UNKNOWN"):
        return True
    return page_is_image_dominated(page, zone.bbox)


def _make_difference(*, issue_type: str, category: str, original_text: str, converted_text: str,
                      page: int, bbox, confidence_score: float, explanation: str,
                      original_word_index: int = None) -> Difference:
    diff = Difference(
        id=next_id("verif"), type=issue_type, category=category,
        severity="HIGH" if confidence_score >= 0.75 else ("MEDIUM" if confidence_score >= 0.4 else "LOW"),
        confidence=confidence_engine.bucket(confidence_score), confidence_score=confidence_score,
        original_text=original_text, converted_text=converted_text,
        original_page=page, original_word_index=original_word_index,
        explanation=explanation,
    )
    if bbox is not None:
        diff.add_original_highlight(page, bbox_dict(bbox), text=converted_text)
    return diff


def find_missing_footnote_callouts(tagged_text: str) -> list:
    """A bare digit (1-3 chars) immediately after sentence-ending
    punctuation, NOT already wrapped in <sup> - role_detector.
    classify_inline_candidate's own exact evidence pattern, checked
    directly against the zone's CURRENT extraction with no OCR/visual
    evidence needed at all (a self-contained typographic signal: real
    prose essentially never has a bare digit glued straight onto
    sentence-ending punctuation for any other reason). Returns raw
    finding dicts: {"number": str, "preceding_char": str, "tagged_match_start": int}."""
    findings = []
    for m in _MISSING_CALLOUT_RE.finditer(tagged_text):
        prefix = tagged_text[:m.start()]
        if prefix.count("<sup>") > prefix.count("</sup>"):
            continue  # already inside a superscript run - not missing at all
        if role_detector.classify_inline_candidate(m.group(2), m.group(1)) != role_detector.BODY_FOOTNOTE_CALLOUT:
            continue
        findings.append({"number": m.group(2), "preceding_char": m.group(1), "tagged_match_start": m.start(2)})
    return findings


def verify_zone(page, zone, pdf_path: str, vocabulary: set, dictionary_additions=None,
                 force_formatting_check: bool = False, mode: str = "fast",
                 page_number_context: list = None, ocr_cache=None, ocr_settings=None) -> list:
    """Runs every verifier applicable to this one zone. `page` is the
    live fitz.Page (from PDFDocument.get_page(zone.page)); `vocabulary`
    is word_join_verifier.build_vocabulary_from_zones's own output for
    the whole project (built once by the caller, not per zone, since
    it's the same set for every zone).

    `mode`: "fast" (default) is today's existing, already-bounded
    behavior - OCR evidence is only fetched when should_fetch_ocr_
    evidence's own gate already says so (never blanket for a cleanly
    digital page), and the formatting visual cross-check only runs
    automatically in the same narrow case (see formatting_verifier.
    has_independent_evidence). "deep" forces BOTH of those for this one
    explicit scope (never automatic/book-wide - the caller decides when
    to pass it, e.g. the operator's own "Deep" toolbar action) -
    equivalent to force_formatting_check=True plus always fetching OCR
    evidence regardless of page classification.

    `page_number_context`: [(page_number, zone_text), ...] for every
    OTHER PageNum-role zone already in the project (built once by the
    caller, same pattern as `vocabulary`) - used purely as sequential
    evidence for role_detector.suggest_page_number_correction; never
    required (defaults to none, meaning no sequential evidence, so any
    proposed correction is LOW confidence only)."""
    from core.text_extractor import extract_zone_formatted_text, strip_tags_to_plain

    deep = mode == "deep"
    force_formatting_check = force_formatting_check or deep

    issues = []
    tagged_text = extract_zone_formatted_text(page, zone)
    plain_text = strip_tags_to_plain(tagged_text)

    ocr_text = _fetch_ocr_evidence(
        page, zone, cache=ocr_cache, ocr_settings=ocr_settings
    ) if (deep or should_fetch_ocr_evidence(page, zone)) else ""

    # --- content (missing/extra/wrong word, punctuation) ---
    if ocr_text:
        for finding in content_verifier.find_content_issues(plain_text, ocr_text):
            if finding["kind"] == "missing":
                original_text = " ".join(finding["words"])
                diff = _make_difference(
                    issue_type="MISSING", category="content", original_text=original_text,
                    converted_text="", page=zone.page, bbox=zone.bbox, confidence_score=0.7,
                    explanation=f'OCR evidence shows "{original_text}" which is missing from the extracted text',
                    original_word_index=finding["word_index"])
                issues.append(vm.VerificationIssue.from_difference(
                    diff, vm.CONTENT, zone_id=zone.zone_id, ocr_text=ocr_text,
                    expected_text=original_text, source_evidence="OCR evidence"))
            elif finding["kind"] == "extra":
                extra_text = " ".join(finding["words"])
                diff = _make_difference(
                    issue_type="EXTRA", category="content", original_text="", converted_text=extra_text,
                    page=zone.page, bbox=zone.bbox, confidence_score=0.5,
                    explanation=f'"{extra_text}" appears in the extracted text but not in OCR evidence',
                    original_word_index=finding["word_index"])
                issues.append(vm.VerificationIssue.from_difference(
                    diff, vm.CONTENT, zone_id=zone.zone_id, ocr_text=ocr_text, source_evidence="OCR evidence"))
            else:  # changed
                score = 0.85
                diff = _make_difference(
                    issue_type="CHANGED", category="content",
                    original_text=finding["original_word"], converted_text=finding["converted_word"],
                    page=zone.page, bbox=zone.bbox, confidence_score=score,
                    explanation=f'OCR evidence reads "{finding["original_word"]}", extracted text has '
                                f'"{finding["converted_word"]}"',
                    original_word_index=finding["word_index"])
                issue_type = finding["issue_type"]
                issues.append(vm.VerificationIssue.from_difference(
                    diff, issue_type, zone_id=zone.zone_id, ocr_text=ocr_text,
                    expected_text=finding["original_word"], suggested_text=finding["original_word"],
                    word_span_count=1, source_evidence="OCR evidence",
                    auto_fixable=(issue_type == vm.PUNCTUATION)))

        # --- word join / split ---
        for finding in word_join_verifier.find_word_join_issues(plain_text, ocr_text, vocabulary):
            if finding["kind"] == "merge":
                original_text = " ".join(finding["original_words"])
                score = finding["confidence"]
                diff = _make_difference(
                    issue_type="MERGED_WORD", category="content", original_text=original_text,
                    converted_text=finding["converted_word"], page=zone.page, bbox=zone.bbox,
                    confidence_score=score,
                    explanation=f'"{finding["converted_word"]}" looks like a merge of "{original_text}"',
                    original_word_index=finding["word_index"])
                issues.append(vm.VerificationIssue.from_difference(
                    diff, vm.WORD_JOIN, zone_id=zone.zone_id, ocr_text=ocr_text,
                    expected_text=original_text, suggested_text=original_text, word_span_count=1,
                    source_evidence=f'OCR + dictionary ({finding["dictionary_support"]:.0%} support)',
                    auto_fixable=not finding.get("uncertain")))
            else:
                converted_text = " ".join(finding["converted_words"])
                score = finding["confidence"]
                diff = _make_difference(
                    issue_type="SPLIT_WORD", category="content", original_text=finding["original_word"],
                    converted_text=converted_text, page=zone.page, bbox=zone.bbox, confidence_score=score,
                    explanation=f'"{converted_text}" looks like a split of "{finding["original_word"]}"',
                    original_word_index=finding["word_index"])
                issues.append(vm.VerificationIssue.from_difference(
                    diff, vm.WORD_SPLIT, zone_id=zone.zone_id, ocr_text=ocr_text,
                    expected_text=finding["original_word"], suggested_text=finding["original_word"],
                    word_span_count=len(finding["converted_words"]),
                    source_evidence=f'OCR + dictionary ({finding["dictionary_support"]:.0%} support)',
                    auto_fixable=not finding.get("uncertain")))

        # --- unicode / homoglyph (character-aligned) ---
        for finding in unicode_verifier.find_unicode_char_issues(plain_text, ocr_text):
            reason = "homoglyph" if finding.get("is_homoglyph") else (
                "accent difference" if finding.get("accent_stripped") else "codepoint mismatch")
            score = 0.8 if finding.get("is_homoglyph") or finding.get("script_changed") else 0.6
            diff = _make_difference(
                issue_type="UNICODE_MISMATCH", category="unicode",
                original_text=finding["original_char"], converted_text=finding["converted_char"],
                page=zone.page, bbox=zone.bbox, confidence_score=score,
                explanation=f'OCR evidence reads {finding["original_char"]!r}, extracted text has '
                            f'{finding["converted_char"]!r} ({reason})',
                original_word_index=None)
            issues.append(vm.VerificationIssue.from_difference(
                diff, vm.UNICODE, zone_id=zone.zone_id, char_range=(finding["index"], finding["index"] + 1),
                ocr_text=ocr_text, expected_text=finding["original_char"],
                suggested_text=finding["original_char"], source_evidence=f"OCR evidence ({reason})",
                auto_fixable=finding.get("is_homoglyph", False)))

    # --- structural Unicode repair log (from extraction time) ---
    for record in unicode_verifier.repair_log_issues_for_pdf(pdf_path):
        if record.get("page") != zone.page:
            continue
        diff = _make_difference(
            issue_type="UNICODE_REPAIR_REVIEW", category="unicode",
            original_text=record.get("original_char", ""),
            converted_text=record.get("replacement_char") or "", page=zone.page, bbox=zone.bbox,
            confidence_score=(record.get("confidence") or 0.0) if record.get("accepted") else 0.2,
            explanation=record.get("reason", ""))
        issues.append(vm.VerificationIssue.from_difference(
            diff, vm.UNICODE, zone_id=zone.zone_id,
            source_evidence="extraction-time structural repair log",
            suggested_text=record.get("replacement_char"),
            auto_fixable=False))

    # --- spelling (advisory, plain text) ---
    for finding in spell_verifier.find_spelling_issues(plain_text, dictionary_additions):
        corroborated = bool(ocr_text) and finding["suggestion"] and finding["suggestion"] in ocr_text.casefold()
        score = 0.8 if corroborated else 0.3
        # An unknown-dictionary word is a SUGGESTION, never an asserted
        # fact (spec: "must not automatically be treated as spelling
        # errors" - names, places, foreign words, citations, technical
        # terms all legitimately fail a dictionary lookup). Only the
        # OCR-corroborated case (independently confirmed by a second
        # reading, and already the only case this function marks
        # auto_fixable below) gets more assertive wording; the plain
        # dictionary-only case always reads as "possible", never
        # "incorrect", regardless of how confident-sounding the rest of
        # the UI might otherwise imply.
        label = "Spelling issue (OCR-confirmed)" if corroborated else "Possible spelling issue"
        explanation = f'{label}: "{finding["word"]}" not found in dictionary'
        if finding["suggestion"]:
            explanation += f'; suggested correction: "{finding["suggestion"]}"'
        if not corroborated:
            explanation += "; may be a proper noun, foreign word, citation, or other valid term"
        diff = _make_difference(
            issue_type="SPELLING", category="content", original_text=finding["suggestion"] or "",
            converted_text=finding["word"], page=zone.page, bbox=zone.bbox, confidence_score=score,
            explanation=explanation, original_word_index=None)
        issues.append(vm.VerificationIssue.from_difference(
            diff, vm.SPELLING, zone_id=zone.zone_id,
            char_range=(finding["char_start"], finding["char_end"]),
            suggested_text=finding["suggestion"],
            source_evidence="OCR-corroborated" if corroborated else "dictionary only",
            auto_fixable=corroborated))

    # --- grammar (always advisory; the rules are English rules) ---
    from core.lang import detect_language
    _english = detect_language(plain_text, default="en") == "en"
    for finding in (grammar_verifier.find_grammar_issues(plain_text) if _english else []):
        diff = _make_difference(
            issue_type="GRAMMAR", category="content", original_text=finding.get("suggestion") or "",
            converted_text=finding["match_text"], page=zone.page, bbox=zone.bbox, confidence_score=0.5,
            explanation=finding["explanation"])
        issues.append(vm.VerificationIssue.from_difference(
            diff, vm.GRAMMAR, zone_id=zone.zone_id,
            char_range=(finding["char_start"], finding["char_end"]),
            suggested_text=finding.get("suggestion"), source_evidence="rule-based grammar check",
            auto_fixable=False))

    # --- formatting cross-check (role-aware for superscript/subscript) ---
    zone_role = role_detector.classify_zone_role(zone)
    fmt_result = formatting_verifier.check_formatting(page, zone, force=force_formatting_check)
    if fmt_result and not fmt_result["agrees"]:
        for tag in fmt_result["mismatches"]:
            if tag in ("superscript", "subscript") and not role_detector.eligible_for_supsub_issue(zone_role):
                # Spec's own hard rule: a PAGE_NUMBER zone is never
                # eligible for a superscript/subscript issue AT ALL,
                # regardless of what the PDF's own font flags/geometry
                # say - role is checked BEFORE formatting here, never
                # inferred from size/position (role_detector.py's own
                # docstring explains why this zone is already known to
                # be a page number: its own tag/cup_name, set correctly
                # at zoning time, not guessed here).
                continue
            issue_type = formatting_verifier.issue_type_for_tag(tag)
            role_note = ""
            if tag in ("superscript", "subscript") and zone_role != role_detector.NORMAL_TEXT:
                role_note = f" (zone role: {zone_role})"
            diff = _make_difference(
                issue_type="FORMATTING_MISMATCH", category="layout",
                original_text=f"{tag}={fmt_result['visual'][tag]}", converted_text=f"{tag}={fmt_result['native'][tag]}",
                page=zone.page, bbox=zone.bbox, confidence_score=0.6,
                explanation=f'native extraction says {tag}={fmt_result["native"][tag]}, '
                            f'independent visual check says {tag}={fmt_result["visual"][tag]}{role_note}')
            # style_field only set when the fix would be to ADD this
            # style (visual says True) - inline_style.add_override only
            # ever adds an override, so a "should be REMOVED" mismatch
            # (visual False, native True) has no safe one-click action
            # yet and is left manual-only (style_field=None).
            fixable_field = tag if fmt_result["visual"].get(tag) else None
            issues.append(vm.VerificationIssue.from_difference(
                diff, issue_type, zone_id=zone.zone_id,
                formatting_expected=fmt_result["visual"], formatting_found=fmt_result["native"],
                source_evidence="native vs. independent visual check", auto_fixable=False,
                style_field=fixable_field))

    # --- alignment ---
    from core.text_extractor import extract_lines
    align_result = alignment_verifier.check_alignment(zone, extract_lines(page, zone.bbox))
    if align_result:
        diff = _make_difference(
            issue_type="ALIGNMENT_MISMATCH", category="layout",
            original_text=alignment_verifier.display_name(align_result["detected"]),
            converted_text=alignment_verifier.display_name(align_result["current"]),
            page=zone.page, bbox=zone.bbox, confidence_score=0.7,
            explanation=f'PDF geometry indicates {align_result["detected"]} alignment, '
                        f'zone is currently set to {align_result["current"]}')
        issues.append(vm.VerificationIssue.from_difference(
            diff, vm.ALIGNMENT, zone_id=zone.zone_id,
            expected_text=align_result["detected"], suggested_text=align_result["detected"],
            source_evidence="PDF line geometry", auto_fixable=False))

    # --- structure (page-number-merge) ---
    for finding in content_verifier.find_structure_issues(plain_text, ocr_text or None):
        diff = _make_difference(
            issue_type="PAGE_NUMBER_MERGED", category="structure",
            original_text=finding["number"], converted_text=finding["content"],
            page=zone.page, bbox=zone.bbox, confidence_score=0.75,
            explanation=f'page number "{finding["number"]}" appears merged with '
                        f'{finding["position"]} content in this zone\'s text')
        issues.append(vm.VerificationIssue.from_difference(
            diff, vm.STRUCTURE, zone_id=zone.zone_id,
            source_evidence="zone text position pattern", auto_fixable=False))

    # --- missing body-footnote-callout superscript (role-aware, no OCR needed) ---
    if role_detector.eligible_for_supsub_issue(zone_role):
        for finding in find_missing_footnote_callouts(tagged_text):
            plain_offset = len(strip_tags_to_plain(tagged_text[:finding["tagged_match_start"]]))
            diff = _make_difference(
                issue_type="SUPERSCRIPT_MISSING", category="layout",
                original_text=f'{finding["preceding_char"]}<sup>{finding["number"]}</sup>',
                converted_text=f'{finding["preceding_char"]}{finding["number"]}',
                page=zone.page, bbox=zone.bbox, confidence_score=0.8,
                explanation=f'"{finding["number"]}" immediately after "{finding["preceding_char"]}" looks like a '
                            f'body footnote callout that should be superscript but is not')
            issues.append(vm.VerificationIssue.from_difference(
                diff, vm.SUPERSCRIPT, zone_id=zone.zone_id,
                char_range=(plain_offset, plain_offset + len(finding["number"])),
                expected_text=finding["number"], suggested_text=finding["number"],
                source_evidence="typographic pattern (digit glued to sentence-ending punctuation)",
                auto_fixable=True, style_field="superscript"))

    # --- page-number OCR-confusion correction (role-gated, evidence-based) ---
    if zone_role == role_detector.PAGE_NUMBER:
        correction = role_detector.suggest_page_number_correction(
            zone.page, zone.text or plain_text, page_number_context or [])
        if correction:
            diff = _make_difference(
                issue_type="PAGE_NUMBER_OCR_ERROR", category="content",
                original_text=correction["suggested"], converted_text=(zone.text or plain_text).strip(),
                page=zone.page, bbox=zone.bbox, confidence_score=correction["confidence"],
                explanation=correction["reason"])
            page_number_text = (zone.text or plain_text)
            issues.append(vm.VerificationIssue.from_difference(
                diff, vm.STRUCTURE, zone_id=zone.zone_id,
                char_range=(0, len(page_number_text)),
                expected_text=correction["suggested"], suggested_text=correction["suggested"],
                source_evidence=correction["reason"], auto_fixable=correction["confidence"] >= 0.75))

    return issues
