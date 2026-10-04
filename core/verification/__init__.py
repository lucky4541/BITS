"""Verification workflow - a NEW layer sitting between OCR/Extraction and
Generate XHTML in the Zoning app's own pipeline (gui/main_window.py).

This package never reimplements the existing extraction/OCR/formatting-
detection/XHTML-generation pipeline - it orchestrates and cross-checks it:

    verification_models.py       - VerificationIssue/VerificationSession,
                                    the user-facing data model. Issues wrap
                                    core.fidelity_compare.difference_model.
                                    Difference (reused, not duplicated).
    content_verifier.py          - word/char-level content checks, reusing
                                    core.fidelity_compare's merged_word_
                                    detector/unicode_detector/homoglyph_
                                    detector/hyphenation_detector (all
                                    pure, stateless functions).
    formatting_verifier.py       - cross-checks core.formatting_detector's
                                    native-PDF formatting decision against
                                    core.ocr.style_detector's independent
                                    OCR/visual decision, when both exist.
    word_join_verifier.py        - thin wrapper around merged_word_detector.
    unicode_verifier.py          - thin wrapper around unicode_detector/
                                    homoglyph_detector, plus surfaces
                                    core.text_extractor.get_unicode_repair_
                                    log()'s own rejected/low-confidence
                                    repairs as REVIEW issues.
    spell_verifier.py            - pyspellchecker-backed, advisory only.
    grammar_verifier.py          - lightweight rule-based, advisory only.
    verification_engine.py       - runs every verifier for one zone.
    verification_orchestrator.py - the per-project driver: iterates zones,
                                    decides when a second (OCR) reading is
                                    worth fetching, applies fixes, persists.
    verification_session.py      - (de)serialization to/from the existing
                                    project JSON + Zone.attributes, exactly
                                    following the "page_rotations" pattern
                                    already used elsewhere in this app.

Nothing in this package is imported by core/text_extractor.py, core/
zone_manager.py, core/epub_xml_generator.py, or core/xml_generator.py -
the existing pipeline has zero dependency on this package and keeps
working identically whether or not verification is ever opened for a
given project (gui/main_window.py is the only integration point, and the
Generate XHTML gate it adds is a soft, opt-in warning - see
verification_orchestrator.py's own docstring)."""
