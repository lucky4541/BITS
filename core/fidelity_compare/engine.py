"""Top-level orchestrator - the one entry point the GUI (and the
acceptance test) calls: reads all three inputs, runs every comparator in
this package, and returns a flat list of Difference dicts plus the score
report. Nothing here writes to any input file (spec section 71): every
reader call is read-only, and every comparator only ever returns data.

Comparison stages (spec section 3):
  A = Original PDF <-> EPUB
  B = EPUB <-> Converted PDF
  C = Original PDF <-> Converted PDF (the final fidelity comparison, and
      the ONLY stage where layout/alignment/position/indentation/margin/
      spacing/font/column comparison runs, since only two real PDFs have
      comparable fixed geometry - see layout_analyzer.py's own guards)."""
from core.fidelity_compare import (
    alignment_detector, block_aligner, character_diff, column_analyzer, confidence_engine,
    epub_reader, figure_compare, font_analyzer, footnote_compare, heading_compare, hyphenation_detector,
    indentation_detector, link_compare, list_compare, margin_detector, merged_word_detector, page_aligner,
    paragraph_aligner, pdf_reader, reference_compare, score_calculator, spacing_analyzer, spacing_detector,
    table_compare, token_aligner, unicode_detector, word_aligner,
)
from core.fidelity_compare.difference_model import Difference, bbox_dict, next_id

CONTENT_MATCH_THRESHOLD = 0.35  # below this, treat a paragraph pair as wholesale replace, not a word-level edit


def read_all(original_pdf_path: str, epub_path: str, converted_pdf_path: str, progress_cb=None):
    if progress_cb:
        progress_cb("Reading Original PDF", 0, 3)
    original_doc = pdf_reader.read_pdf(original_pdf_path, "original_pdf", progress_cb=
                                      (lambda stage, current, total: progress_cb(stage + " (Original)", current, total)) if progress_cb else None)
    if progress_cb:
        progress_cb("Reading EPUB", 1, 3)
    epub_doc = epub_reader.read_epub(epub_path)
    if progress_cb:
        progress_cb("Reading Converted PDF", 2, 3)
    converted_doc = pdf_reader.read_pdf(converted_pdf_path, "converted_pdf", progress_cb=
                                       (lambda stage, current, total: progress_cb(stage + " (Converted)", current, total)) if progress_cb else None)
    return original_doc, epub_doc, converted_doc


def _severity_from_confidence(score: float) -> str:
    from core.fidelity_compare.confidence_engine import bucket
    b = bucket(score)
    return {"HIGH": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}.get(b, "MEDIUM")


def _make_diff(type_, category, severity, score, stage, **kw) -> Difference:
    from core.fidelity_compare.confidence_engine import bucket
    d = Difference(id=next_id(), type=type_, category=category, severity=severity,
                    confidence=bucket(score), confidence_score=score, comparison_stage=stage)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def compare_content(original_doc, converted_doc, vocabulary, stage: str, progress_cb=None) -> list:
    """Paragraph + word-level content comparison for one stage pair (spec
    sections 8/9/18). progress_cb(current, total) is called once per
    paragraph pair - the word-level diff below is the single most
    expensive per-item operation in this whole pipeline for a real book
    with thousands of paragraphs, so this is where "is it actually still
    working" feedback matters most (spec section 66)."""
    orig_paras = [(p, b) for p, b in original_doc.all_blocks() if b.kind == "paragraph"]
    conv_paras = [(p, b) for p, b in converted_doc.all_blocks() if b.kind == "paragraph"]
    differences = []

    struct_changes = paragraph_aligner.find_paragraph_structure_changes(
        [b for _, b in orig_paras], [b for _, b in conv_paras])
    handled_orig, handled_conv = set(), set()
    for sc in struct_changes:
        handled_orig.update(sc["original_indices"])
        handled_conv.update(sc["converted_indices"])
        op = orig_paras[sc["original_indices"][0]][0]
        cp = conv_paras[sc["converted_indices"][0]][0]
        differences.append(_make_diff(
            sc["type"], "structure", "LOW", sc["confidence"], stage,
            original_page=op.number, converted_page=cp.number,
            explanation="CONTENT PRESERVED / STRUCTURE CHANGED", result_kind="SEMANTIC"))

    pairs = block_aligner.align_by_key([b.text.semantic for _, b in orig_paras],
                                        [b.text.semantic for _, b in conv_paras])
    total = len(pairs) or 1
    for i, p in enumerate(pairs, start=1):
        if progress_cb and i % 25 == 0:
            progress_cb(i, total)  # every 25th pair - a per-pair call would itself dominate runtime on a huge book
        if p.op == "equal" or (p.original_index in handled_orig) or (p.converted_index in handled_conv):
            continue
        if p.op == "delete":
            op, ob = orig_paras[p.original_index]
            differences.append(_make_diff("MISSING_CONTENT", "content", "HIGH", 0.9, stage,
                                           original_page=op.number, original_bbox=bbox_dict(ob.bbox),
                                           original_text=ob.text.raw, original_paragraph=p.original_index + 1,
                                           result_kind="RAW"))
        elif p.op == "insert":
            cp, cb = conv_paras[p.converted_index]
            differences.append(_make_diff("ADDED_CONTENT", "content", "HIGH", 0.9, stage,
                                           converted_page=cp.number, converted_bbox=bbox_dict(cb.bbox),
                                           converted_text=cb.text.raw, converted_paragraph=p.converted_index + 1,
                                           result_kind="RAW"))
        elif p.op == "replace" and p.original_index is not None and p.converted_index is not None:
            op, ob = orig_paras[p.original_index]
            cp, cb = conv_paras[p.converted_index]
            # Paragraph index (spec: "EXACT CONTENT DIFFERENCE ENGINE"
            # section 20 - "which paragraph") - 1-based position among
            # this page's own paragraph blocks, threaded down into every
            # word/character-level finding this paragraph pair produces.
            orig_para_num, conv_para_num = p.original_index + 1, p.converted_index + 1
            sim = block_aligner.similarity(ob.text.semantic, cb.text.semantic)
            if sim < CONTENT_MATCH_THRESHOLD:
                differences.append(_make_diff("CHANGED_CONTENT", "content", "HIGH", 1 - sim, stage,
                                               original_page=op.number, original_bbox=bbox_dict(ob.bbox),
                                               converted_page=cp.number, converted_bbox=bbox_dict(cb.bbox),
                                               original_text=ob.text.raw, converted_text=cb.text.raw,
                                               original_paragraph=orig_para_num, converted_paragraph=conv_para_num,
                                               result_kind="SEMANTIC"))
                continue
            differences.extend(_word_level_diff(op, ob, cp, cb, vocabulary, stage, orig_para_num, conv_para_num))
            differences.extend(_hyphenation_diff(ob, cb, op, cp, stage))
    return differences


def _word_level_diff(op, ob, cp, cb, vocabulary, stage, orig_para_num=None, conv_para_num=None) -> list:
    original_tokens, converted_tokens, pairs = token_aligner.align_tokens(ob.text.raw, cb.text.raw)
    findings = word_aligner.find_word_differences(original_tokens, converted_tokens, pairs, vocabulary)
    differences = []
    for f in findings:
        ftype = f["type"]
        # Word index (spec section 20 - "which word changed") - 1-based,
        # from the SAME token positions word_aligner already computed;
        # None when a finding type genuinely has no single corresponding
        # word on one side (e.g. ADDED_WORD has no original_index).
        orig_word_idx = f["original_index"] + 1 if f.get("original_index") is not None else None
        conv_word_idx = f["converted_index"] + 1 if f.get("converted_index") is not None else None
        if ftype == "CHANGED_WORD":
            sub = f.get("sub_classification")
            category = "unicode" if sub in ("HOMOGLYPH_SUBSTITUTION", "UNICODE_CODEPOINT_CHANGED",
                                             "SPECIAL_CHARACTER_CHANGED", "LIGATURE_CHANGED") else "content"
            result_kind = "RAW" if category == "unicode" else "SEMANTIC"
            d = _make_diff(sub or ftype, category, "HIGH" if category == "unicode" else "MEDIUM",
                            f["confidence"], stage, original_page=op.number, converted_page=cp.number,
                            original_bbox=bbox_dict(ob.bbox), converted_bbox=bbox_dict(cb.bbox),
                            original_text=f["original_text"], converted_text=f["converted_text"],
                            original_paragraph=orig_para_num, converted_paragraph=conv_para_num,
                            original_word_index=orig_word_idx, converted_word_index=conv_word_idx,
                            result_kind=result_kind)
            char_diff = f.get("char_diff") or {}
            char_findings = char_diff.get("char_findings", [])
            if char_findings:
                # Character index (spec section 20 - "which character
                # changed") - the position WITHIN the word of the first
                # actual differing character, from character_diff.py's own
                # already-computed difflib opcode positions.
                d.original_character_index = char_findings[0].get("position")
            for cf in char_findings:
                if cf.get("original"):
                    d.original_unicode = cf["original"]
                    d.converted_unicode = cf.get("converted")
                    break
            differences.append(d)
        elif ftype in ("MERGED_WORD", "SPLIT_WORD"):
            severity = "LOW" if f.get("uncertain") else "MEDIUM"
            confidence_type = "UNCERTAIN" if f.get("uncertain") else None
            d = _make_diff(ftype, "content", severity, f["confidence"], stage,
                            original_page=op.number, converted_page=cp.number,
                            original_bbox=bbox_dict(ob.bbox), converted_bbox=bbox_dict(cb.bbox),
                            original_text=f["original_text"], converted_text=f["converted_text"],
                            original_paragraph=orig_para_num, converted_paragraph=conv_para_num,
                            original_word_index=orig_word_idx, converted_word_index=conv_word_idx,
                            result_kind="SEMANTIC")
            if confidence_type:
                d.confidence = "LOW"
            differences.append(d)
            companion = f.get("companion")
            if companion:
                differences.append(_make_diff(companion, "content", "LOW", f["confidence"], stage,
                                               original_page=op.number, converted_page=cp.number,
                                               original_text=f["original_text"],
                                               converted_text=f["converted_text"],
                                               original_paragraph=orig_para_num, converted_paragraph=conv_para_num,
                                               original_word_index=orig_word_idx, converted_word_index=conv_word_idx,
                                               result_kind="RAW"))
        elif ftype in ("MISSING_WORD", "ADDED_WORD"):
            differences.append(_make_diff(ftype, "content", "MEDIUM", f["confidence"], stage,
                                           original_page=op.number, converted_page=cp.number,
                                           original_text=f.get("original_text", ""),
                                           converted_text=f.get("converted_text", ""),
                                           original_paragraph=orig_para_num, converted_paragraph=conv_para_num,
                                           original_word_index=orig_word_idx, converted_word_index=conv_word_idx,
                                           result_kind="RAW"))
    return differences


def _hyphenation_diff(ob, cb, op, cp, stage) -> list:
    """Scans the ORIGINAL block's own line boundaries for line-break
    hyphens (only meaningful when ob came from a real PDF - EPUB-sourced
    blocks have no Line objects) and classifies against the converted
    text (spec section 17)."""
    differences = []
    lines = ob.lines
    for i in range(len(lines) - 1):
        line, next_line = lines[i], lines[i + 1]
        if not hyphenation_detector.is_line_break_hyphen(line.text.raw, next_line.text.raw):
            continue
        prefix = line.text.raw.rstrip().split()[-1] if line.text.raw.split() else ""
        suffix = next_line.text.raw.lstrip().split()[0] if next_line.text.raw.split() else ""
        if not prefix or not suffix:
            continue
        joined_solid = (prefix.rstrip("-­") + suffix).casefold()
        if joined_solid not in cb.text.semantic.replace(" ", ""):
            continue
        result = hyphenation_detector.classify(prefix, suffix, joined_solid, True)
        if result["type"]:
            differences.append(_make_diff(result["type"], "hyphenation", "MEDIUM", 0.85, stage,
                                           original_page=op.number, converted_page=cp.number,
                                           original_bbox=bbox_dict(line.bbox),
                                           original_text=f"{prefix}\n{suffix}",
                                           converted_text=joined_solid, explanation=result["status"],
                                           result_kind="RAW"))
    return differences


def compare_structure(original_doc, converted_doc, stage: str, progress_cb=None, page_groups=None) -> list:
    """progress_cb(stage_label, current, total), forwarded to figure_compare/
    table_compare's own per-item progress (spec section 66: "Comparing 145
    / 292 pages" - a real count, not a static label that can look
    indistinguishable from a hang on a book with hundreds of figures/
    tables) - see also figure_compare.py's own docstring on the real
    O(N*M) image-hashing stall this was found to expose."""
    differences = []
    for f in heading_compare.compare(original_doc, converted_doc):
        differences.append(_from_generic(f, "structure", stage))
    for f in list_compare.compare(original_doc, converted_doc):
        differences.append(_from_generic(f, "structure", stage))

    def _fig_progress(current, total):
        if progress_cb:
            progress_cb(f"Comparing Figures ({stage})", current, total)

    for f in figure_compare.compare(original_doc, converted_doc, progress_cb=_fig_progress, page_groups=page_groups):
        differences.append(_from_generic(f, "figure", stage))

    def _tbl_progress(current, total):
        if progress_cb:
            progress_cb(f"Comparing Tables ({stage})", current, total)

    for f in table_compare.compare(original_doc, converted_doc, progress_cb=_tbl_progress):
        differences.append(_from_generic(f, "table", stage))
    for f in footnote_compare.compare(original_doc, converted_doc):
        differences.append(_from_generic(f, "footnote", stage))
    for f in reference_compare.compare(original_doc, converted_doc):
        differences.append(_from_generic(f, "reference", stage))
    for f in index_compare_findings(original_doc, converted_doc):
        differences.append(_from_generic(f, "index", stage))
    return differences


def index_compare_findings(original_doc, converted_doc):
    from core.fidelity_compare import index_compare
    return index_compare.compare(original_doc, converted_doc)


def _from_generic(f: dict, category: str, stage: str) -> Difference:
    score = f.get("confidence", 0.7)
    return _make_diff(f["type"], category, f.get("severity", "MEDIUM"), score, stage,
                       original_page=f.get("original_page"), converted_page=f.get("converted_page"),
                       original_text=f.get("original_text", f.get("text", "")),
                       converted_text=f.get("converted_text", ""), explanation=str(f), result_kind="SEMANTIC")


def compare_layout(original_doc, converted_doc, page_groups=None) -> list:
    """Layout comparison - ONLY ever run for Original PDF <-> Converted
    PDF (stage C), never against the EPUB (spec section 28, and see
    epub_reader.py's own docstring on why)."""
    orig_blocks = [(p, b) for p, b in original_doc.all_blocks() if b.layout and b.layout.page_width > 0]
    conv_blocks = [(p, b) for p, b in converted_doc.all_blocks() if b.layout and b.layout.page_width > 0]
    pairs = block_aligner.align_by_key([b.text.semantic for _, b in orig_blocks],
                                        [b.text.semantic for _, b in conv_blocks])
    differences = []
    for p in pairs:
        matched_o = p.original_index
        matched_c = p.converted_index
        if matched_o is None or matched_c is None:
            if p.op != "replace":
                continue
        if matched_o is None or matched_c is None:
            continue
        op, ob = orig_blocks[matched_o]
        cp, cb = conv_blocks[matched_c]
        if p.op == "replace":
            sim = block_aligner.similarity(ob.text.semantic, cb.text.semantic)
            if sim < 0.5:
                continue  # not really the same block - content comparison already reports this
        ol, cl = ob.layout, cb.layout

        align_result = alignment_detector.compare(ol, cl)
        if align_result.get("changed"):
            differences.append(_make_diff(
                "ALIGNMENT_CHANGED", "layout", align_result["severity"], align_result["confidence"], "C",
                original_page=op.number, converted_page=cp.number, original_bbox=bbox_dict(ob.bbox),
                converted_bbox=bbox_dict(cb.bbox), original_text=ob.text.raw,
                original_layout={"alignment": ol.alignment, **ob.bbox.to_dict()},
                converted_layout={"alignment": cl.alignment, **cb.bbox.to_dict()},
                explanation=align_result.get("transition") or "ALIGNMENT_CHANGED", result_kind="LAYOUT"))

        for f in font_analyzer.compare(ol.font, cl.font):
            differences.append(_make_diff(f["type"], "layout", f["severity"], f["confidence"], "C",
                                           original_page=op.number, converted_page=cp.number,
                                           original_text=str(f["original"]), converted_text=str(f["converted"]),
                                           result_kind="LAYOUT"))

        # Inline typography is compared independently of the block's dominant
        # font. This is essential for mixed normal/italic/bold paragraphs.
        for wf in font_analyzer.compare_word_runs(
                [w for ln in ob.lines for w in getattr(ln, "words", [])],
                [w for ln in cb.lines for w in getattr(ln, "words", [])]):
            differences.append(_make_diff(
                wf["type"], "layout", wf["severity"], wf["confidence"], "C",
                original_page=op.number, converted_page=cp.number,
                original_bbox=bbox_dict(wf.get("original_bbox")),
                converted_bbox=bbox_dict(wf.get("converted_bbox")),
                original_text=wf.get("original_text", ""),
                converted_text=wf.get("converted_text", ""),
                explanation=wf.get("evidence", "inline typography"),
                result_kind="LAYOUT"))

        for rf in font_analyzer.compare_run_formatting(ob.lines, cb.lines):
            differences.append(_make_diff(
                rf["type"], "layout", rf["severity"], rf["confidence"], "C",
                original_page=op.number, converted_page=cp.number,
                original_bbox=bbox_dict(rf.get("original_bbox")),
                converted_bbox=bbox_dict(rf.get("converted_bbox")),
                original_text=rf.get("original_text", ""),
                converted_text=rf.get("converted_text", ""),
                explanation=rf.get("evidence", "inline run typography"),
                result_kind="LAYOUT"))

        ind = indentation_detector.compare(ol, cl)
        if ind.get("changed"):
            differences.append(_make_diff(ind["type"], "layout", ind["severity"], ind["confidence"], "C",
                                           original_page=op.number, converted_page=cp.number,
                                           original_text=str(round(ind["original"], 1)),
                                           converted_text=str(round(ind["converted"], 1)), result_kind="LAYOUT"))

        ls = spacing_analyzer.compare_line_spacing(ol, cl)
        if ls.get("changed"):
            differences.append(_make_diff(ls["type"], "layout", ls["severity"], ls["confidence"], "C",
                                           original_page=op.number, converted_page=cp.number,
                                           original_text=str(ls["original"]), converted_text=str(ls["converted"]),
                                           result_kind="LAYOUT"))
        for psf in spacing_analyzer.compare_paragraph_spacing(ol, cl):
            differences.append(_make_diff(psf["type"], "layout", psf["severity"], psf["confidence"], "C",
                                           original_page=op.number, converted_page=cp.number, result_kind="LAYOUT"))

        col = column_analyzer.compare_block_column_assignment(ol, cl, op, cp)
        if col.get("changed"):
            differences.append(_make_diff(col["type"], "layout", col["severity"], col["confidence"], "C",
                                           original_page=op.number, converted_page=cp.number, result_kind="LAYOUT"))

    groups = page_groups if page_groups is not None else page_aligner.align_pages(original_doc, converted_doc)
    orig_pages_by_num = {p.number: p for p in original_doc.pages}
    conv_pages_by_num = {p.number: p for p in converted_doc.pages}
    for g in groups:
        if len(g["original_pages"]) == 1 and len(g["converted_pages"]) == 1:
            op_ = orig_pages_by_num[g["original_pages"][0]]
            cp_ = conv_pages_by_num[g["converted_pages"][0]]
            for m in margin_detector.compare(op_, cp_):
                differences.append(_make_diff(m["type"], "layout", m["severity"], m["confidence"], "C",
                                               original_page=op_.number, converted_page=cp_.number,
                                               explanation=f"{m['side']} margin", result_kind="LAYOUT"))
            for cc in column_analyzer.compare_page_columns(op_, cp_):
                differences.append(_make_diff(cc["type"], "layout", cc["severity"], cc["confidence"], "C",
                                               original_page=op_.number, converted_page=cp_.number,
                                               result_kind="LAYOUT"))
    return differences


def run_full_comparison(original_pdf_path: str, epub_path: str, converted_pdf_path: str,
                         progress_cb=None, should_cancel=None) -> dict:
    should_cancel = should_cancel or (lambda: False)
    original_doc, epub_doc, converted_doc = read_all(original_pdf_path, epub_path, converted_pdf_path, progress_cb)
    if should_cancel():
        return {"cancelled": True}

    if progress_cb:
        progress_cb("Building Semantic Model", 0, 1)
    vocabulary = merged_word_detector.build_vocabulary(original_doc, converted_doc)

    if progress_cb:
        progress_cb("Aligning Pages", 0, 1)
    page_groups = page_aligner.align_pages(original_doc, converted_doc)
    # Reuse the already-computed page correspondence for all three structural
    # comparisons. This is especially valuable for figure matching, whose
    # candidate search is now restricted to aligned page groups.
    page_groups_a = page_aligner.align_pages(original_doc, epub_doc)
    page_groups_b = page_aligner.align_pages(epub_doc, converted_doc)

    all_differences = []

    def _content_progress(stage_letter):
        def _cb(current, total):
            if progress_cb:
                progress_cb(f"Comparing Text ({stage_letter}) - Unicode/Hyphenation inline", current, total)
        return _cb

    all_differences.extend(compare_content(original_doc, epub_doc, vocabulary, "A", _content_progress("A")))
    if should_cancel():
        return {"cancelled": True}
    all_differences.extend(compare_content(epub_doc, converted_doc, vocabulary, "B", _content_progress("B")))
    if should_cancel():
        return {"cancelled": True}
    all_differences.extend(compare_content(original_doc, converted_doc, vocabulary, "C", _content_progress("C")))

    all_differences.extend(compare_structure(original_doc, epub_doc, "A", progress_cb, page_groups_a))
    all_differences.extend(compare_structure(epub_doc, converted_doc, "B", progress_cb, page_groups_b))
    all_differences.extend(compare_structure(original_doc, converted_doc, "C", progress_cb, page_groups))

    if progress_cb:
        progress_cb("Comparing Layout", 0, 1)
    all_differences.extend(compare_layout(original_doc, converted_doc, page_groups))

    if progress_cb:
        progress_cb("Comparing Links", 0, 1)
    for f in link_compare.check_link_integrity(epub_doc):
        all_differences.append(_from_generic(f, "link", "A"))

    if progress_cb:
        progress_cb("Generating Report", 0, 1)

    totals = {
        "content": sum(len(b.text.raw.split()) for _p, b in original_doc.all_blocks() if b.kind == "paragraph"),
        "unicode": sum(len(b.text.raw) for _p, b in original_doc.all_blocks()),
        "hyphenation": max(1, sum(1 for pg in original_doc.pages for blk in pg.blocks
                                   for i in range(len(blk.lines) - 1)
                                   if hyphenation_detector.is_line_break_hyphen(
                                       blk.lines[i].text.raw, blk.lines[i + 1].text.raw))),
        "structure": len(heading_compare.collect_headings(original_doc)) + len(list_compare.collect_list_items(
            original_doc)) + len(original_doc.footnotes) + len(original_doc.references)
            + len(original_doc.index_entries) or 1,
        "layout": max(1, sum(1 for _p, b in original_doc.all_blocks() if b.layout and b.layout.page_width > 0)),
        "figure": max(1, sum(len(pg.figures) for pg in original_doc.pages)),
        "table": max(1, sum(len(pg.tables) for pg in original_doc.pages)),
        "link": max(1, len(epub_doc.links)),
        "ocr": 1,
    }
    scores = score_calculator.calculate_scores([d.to_dict() for d in all_differences], totals)

    return {
        "cancelled": False,
        "differences": all_differences,
        "scores": scores,
        "production_status": score_calculator.production_status(all_differences),
        "summary": score_calculator.summarize_differences([d.to_dict() for d in all_differences]),
        "page_groups": page_groups,
        "original_doc": original_doc,
        "epub_doc": epub_doc,
        "converted_doc": converted_doc,
    }


def run_pdf_to_pdf_comparison(original_pdf_path: str, converted_pdf_path: str,
                               progress_cb=None, should_cancel=None) -> dict:
    """Direct PDF <-> PDF comparison (spec: "ZONETOOL - ADVANCED PDF
    COMPARISON & PRODUCTION QA ENGINE" - "[OPEN ORIGINAL PDF] [OPEN
    GENERATED PDF]", no EPUB involved at all) - a SEPARATE, additive entry
    point, never a duplicate engine: reuses the exact same reader
    (pdf_reader.read_pdf), the exact same content/structure/layout
    comparators run_full_comparison's own "stage C" (Original PDF <->
    Converted PDF - the only stage that already runs PDF<->PDF, real-
    geometry-only layout comparison) already calls, and the same
    score_calculator/page_aligner. Only link_compare is skipped (it checks
    EPUB-internal href integrity - meaningless with no EPUB in the loop)."""
    should_cancel = should_cancel or (lambda: False)
    if progress_cb:
        progress_cb("Reading Original PDF", 0, 2)
    original_doc = pdf_reader.read_pdf(original_pdf_path, "original_pdf", progress_cb=
                                      (lambda stage, current, total: progress_cb(stage + " (Original)", current, total)) if progress_cb else None)
    if progress_cb:
        progress_cb("Reading Generated PDF", 1, 2)
    converted_doc = pdf_reader.read_pdf(converted_pdf_path, "converted_pdf", progress_cb=
                                       (lambda stage, current, total: progress_cb(stage + " (Converted)", current, total)) if progress_cb else None)
    if should_cancel():
        return {"cancelled": True}

    if progress_cb:
        progress_cb("Building Semantic Model", 0, 1)
    vocabulary = merged_word_detector.build_vocabulary(original_doc, converted_doc)

    if progress_cb:
        progress_cb("Aligning Pages", 0, 1)
    page_groups = page_aligner.align_pages(original_doc, converted_doc)

    all_differences = []

    def _content_progress(current, total):
        if progress_cb:
            progress_cb("Comparing Text - Unicode/Hyphenation inline", current, total)

    all_differences.extend(compare_content(original_doc, converted_doc, vocabulary, "C", _content_progress))
    if should_cancel():
        return {"cancelled": True}

    all_differences.extend(compare_structure(original_doc, converted_doc, "C", progress_cb, page_groups))

    if progress_cb:
        progress_cb("Comparing Layout", 0, 1)
    all_differences.extend(compare_layout(original_doc, converted_doc, page_groups))

    if progress_cb:
        progress_cb("Generating Report", 0, 1)

    totals = {
        "content": sum(len(b.text.raw.split()) for _p, b in original_doc.all_blocks() if b.kind == "paragraph"),
        "unicode": sum(len(b.text.raw) for _p, b in original_doc.all_blocks()),
        "hyphenation": max(1, sum(1 for pg in original_doc.pages for blk in pg.blocks
                                   for i in range(len(blk.lines) - 1)
                                   if hyphenation_detector.is_line_break_hyphen(
                                       blk.lines[i].text.raw, blk.lines[i + 1].text.raw))),
        "structure": len(heading_compare.collect_headings(original_doc)) + len(list_compare.collect_list_items(
            original_doc)) + len(original_doc.footnotes) + len(original_doc.references)
            + len(original_doc.index_entries) or 1,
        "layout": max(1, sum(1 for _p, b in original_doc.all_blocks() if b.layout and b.layout.page_width > 0)),
        "figure": max(1, sum(len(pg.figures) for pg in original_doc.pages)),
        "table": max(1, sum(len(pg.tables) for pg in original_doc.pages)),
    }
    scores = score_calculator.calculate_scores([d.to_dict() for d in all_differences], totals)

    return {
        "cancelled": False,
        "differences": all_differences,
        "scores": scores,
        "production_status": score_calculator.production_status(all_differences),
        "summary": score_calculator.summarize_differences([d.to_dict() for d in all_differences]),
        "page_groups": page_groups,
        "original_doc": original_doc,
        "epub_doc": None,
        "converted_doc": converted_doc,
    }
