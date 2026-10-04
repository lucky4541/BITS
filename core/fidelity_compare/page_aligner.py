"""Content-based page alignment (spec section 52: "Do NOT assume Original
page 1 = Converted page 1... use content alignment... store the mapping")
- reflow can freely change page count, so pages are aligned by comparing
each page's own semantic text via the same LCS technique used everywhere
else in this subsystem, never by matching physical index. Works
identically whether the second Document is a real paged PDF or an
EPUB (whose "pages" are spine items - see epub_reader.py); the alignment
itself doesn't care what a "page" means, only that each one has text."""
from core.fidelity_compare import block_aligner


def _page_key(page) -> str:
    return " ".join(block.text.semantic for block in page.blocks)


def align_pages(original_doc, converted_doc) -> list:
    """Returns a list of {"original_pages": [..page numbers..],
    "converted_pages": [..page numbers..]} groups, in document order,
    covering every page of both documents at least once."""
    original_keys = [_page_key(p) for p in original_doc.pages]
    converted_keys = [_page_key(p) for p in converted_doc.pages]
    pairs = block_aligner.align_by_key(original_keys, converted_keys)

    groups = []
    i = 0
    while i < len(pairs):
        p = pairs[i]
        if p.op == "equal":
            groups.append({"original_pages": [original_doc.pages[p.original_index].number],
                            "converted_pages": [converted_doc.pages[p.converted_index].number],
                            "op": "equal"})
            i += 1
            continue
        group = [p]
        j = i + 1
        while j < len(pairs) and pairs[j].op == p.op:
            group.append(pairs[j])
            j += 1
        if p.op == "replace":
            oidx = sorted({g.original_index for g in group if g.original_index is not None})
            cidx = sorted({g.converted_index for g in group if g.converted_index is not None})
            groups.append({"original_pages": [original_doc.pages[k].number for k in oidx],
                            "converted_pages": [converted_doc.pages[k].number for k in cidx], "op": "replace"})
        elif p.op == "delete":
            oidx = [g.original_index for g in group]
            groups.append({"original_pages": [original_doc.pages[k].number for k in oidx],
                            "converted_pages": [], "op": "delete"})
        elif p.op == "insert":
            cidx = [g.converted_index for g in group]
            groups.append({"original_pages": [], "converted_pages": [converted_doc.pages[k].number for k in cidx],
                            "op": "insert"})
        i = j
    return groups


def page_number_for(groups: list, original_page: int = None, converted_page: int = None):
    """Looks up the corresponding page(s) on the other side for a single
    page number, used by the GUI's synchronized viewer (spec section 47)
    to navigate both sides when a difference is selected."""
    for g in groups:
        if original_page is not None and original_page in g["original_pages"]:
            return g["converted_pages"]
        if converted_page is not None and converted_page in g["converted_pages"]:
            return g["original_pages"]
    return []
