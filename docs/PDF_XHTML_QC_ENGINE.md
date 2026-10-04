# PDF ↔ XHTML QC Engine

This engine compares an EPUB against its source PDF. On top of the comparison it adds auto-mapping, visual comparison, validation, auto-correction and split management.

It is part of the existing application, not a separate program. You can open it in two ways:

- **Launcher**: card **05 · PDF ↔ XHTML QC**.
- **Fidelity Compare**: the **Open QC Workspace** button. It opens the workspace with the Original PDF and Generated EPUB that are already filled in there.

The inputs are a PDF and either an `.epub` file or an unpacked package folder.

- **`.epub` file**: the tool extracts it into a working copy. The original file is never modified. **Build EPUB…** writes a new file.
- **Package folder**: the tool edits the folder in place. Every change can be undone.

## Architecture

All of the engine code is in `core/qc/`. The GUI is in `app/qc/`.

| Module | Role |
|---|---|
| `package_manager.py` | `EpubPackageManager`. Every structural operation goes through it: the OPF manifest and spine, NAV/TOC, NCX, links and IDs. Each operation runs as a transaction with undo/redo. It also handles locks and remembers decisions in the sidecar file `.epubforge_qc.json`. |
| `pdf_model.py` | Reads the PDF. **Text**: words, lines, paragraphs and columns, using the existing `layout_engine`, `column_detector` and `paragraph_auto_zone`. **Running heads**: detected and excluded. **Page numbers**: Arabic and Roman, front matter, unnumbered pages and inferred numbers, each with a confidence. **Scanned pages**: read with the existing OCR service. **Images**: SHA-1 and perceptual hashes, captions and reading position. Results are cached per page. |
| `xhtml_model.py` | Reads each split: blocks, words, page markers (`epub:type="pagebreak"`, as recognised by `pagebreak_analyzer`), images and captions. Results are cached per split by content hash, so only changed splits are parsed again. |
| `mapping.py` | **The one unified mapping** (details below). Every viewer, panel, report and correction reads from it. |
| `corrections.py` | Applies corrections: page markers (insert, relabel, move, remove), images (remap, move, insert), links, and text (replace, remove, insert). |
| `engine.py` | `QCSession`. Covers analysis and incremental re-analysis, the decisions (accept, reject, edit, ignore, reopen, apply to all similar), auto-correct, image REMAP/CHANGE, the split operations, undo/redo, reports and the EPUB build. |
| `final_validation.py` | Runs checks before export. It reports each section as PASS, WARN, FAIL or SKIP. It also runs the existing Quick Validator and, when it is available, EPUBCheck. |
| `reports.py` | Writes the report as HTML, JSON, CSV or PDF. |

### How the mapping works

`mapping.py` aligns the two texts with anchors:

1. Words whose 5-gram appears exactly once in each document become anchors.
2. The longest increasing subsequence of those anchors keeps them in order.
3. `difflib` aligns only the text between anchors.

As a result, alignment time grows roughly linearly with book size, and one local defect never shifts the alignment of the rest of the book.

### Difference states

| State | Colour | Difference types |
|---|---|---|
| Matched | green | — |
| Missing / Extra | red | missing text, extra text |
| Modified | yellow | modified text (shown down to individual characters) |
| Moved | blue | reordered text |
| Duplicate | orange | duplicate text |
| Uncertain | purple | low-confidence mapping |

The engine also reports these problems:

- paragraphs merged together, or a paragraph split where the PDF has none;
- page markers that are missing, wrong, misplaced, duplicated or extra;
- images that are missing, wrong, modified, moved, extra, or have a broken reference;
- captions that are missing, wrong, on the wrong image, or duplicated;
- broken links;
- package problems;
- splits in the wrong order, or splits that don't map to any PDF page.

Colours can be changed under **Colours…**; they are saved in the UI preferences under `qc_colors`. Every colour is always shown together with its text label.

### Scores

| Level | Scores |
|---|---|
| Document | Overall, Text, Structure, Images, Page Markers, Reading Order, Links, Package |
| Page | Match, Text, Images, Page Marker, Structure, number of differences |
| Split | Match, Text, Missing, Extra, Images, Page Markers, Links, Warnings |

## Decision priority

From highest to lowest:

1. **User decision**: accept, reject, edit or ignore. Decisions are remembered per difference and survive re-analysis.
2. **Locked split**: no operation or correction can change a locked split.
3. **Auto-correction**: applied only when the fix is marked auto-safe and its confidence is at least 0.90. It covers page markers, image remapping, links and package entries. **Text is never changed automatically.**
4. **Comparison results**: semantic, then layout, then OCR evidence.

## Workspace

- **Viewers**: the PDF on the left; on the right, the rendered XHTML and the XHTML source. Scrolling and navigation stay in sync.
- **Click-to-map**: works in both directions. Clicking a PDF word, an XHTML word, a source line, a page-marker badge or an image jumps to the matching place on the other side.
- **Viewing tools**: zoom, Fit width, Fit page and search. Search looks in both documents and also finds text that exists only in the PDF.
- **Overlay mode**: the PDF page and the XHTML rendered by MuPDF's `fitz.Story` are shown on top of each other. There are four options: opacity, blink, pixel difference and edges.
- **Difference navigation**: **◀ Prev diff** and **Next diff ▶**, or F3 and Shift+F3, show a "Difference N/M" counter.
- **Difference panel**:
  - Filters by type and status.
  - Details for each difference, including a character-level diff.
  - Buttons: **ACCEPT**, **REJECT**, **EDIT**, **IGNORE**, **REOPEN**, **APPLY TO ALL SIMILAR**.
  - For images: the PDF and EPUB images side by side, plus **REMAP** (choose another image already in the package) and **CHANGE** (add a new file to the package and point the image at it).
- **Split tree**: shows each split's PDF page range, status, warnings, images, page markers and match score. Drag and drop to reorder. Available actions:

  | Action | Effect |
  |---|---|
  | Add | Adds a new split |
  | Delete | Deletes exactly one split, after showing an impact preview |
  | Merge Prev / Merge Next | Merges the split with its neighbour |
  | Split Here | Splits at the selected XHTML word |
  | Split at PDF Page | Splits where that PDF page begins, located through the mapping |
  | Move ↑ / Move ↓ | Moves the split one place in the spine |
  | Rename | Renames the split file |
  | Lock / Unlock | Protects the split from changes |
  | Preview | Shows the split's details without changing anything |

  Every split operation repairs the OPF manifest and spine, NAV/TOC, NCX, links and IDs. Colliding IDs are renamed deterministically. Anything the tool cannot repair safely is listed under "Needs review".
- **Undo / Redo** (Ctrl+Z / Ctrl+Y): covers every change, including a whole batch of corrections, which is undone in a single step.
- **Final validation** and **Build EPUB…**: the EPUB is written with `mimetype` as the first entry, stored uncompressed.

## Tests

```
python -m pytest tests/test_qc_engine.py -q
```

The fixture `tests/fixtures/qc_book.py` builds a 6-page PDF and a faithful EPUB from the same content spec. Each kind of defect is seeded into the EPUB on purpose, so every expected result comes from the spec, not from the engine.

The tests check:

- the clean book scores 100%;
- each seeded defect is detected as the right type and colour state;
- character-level differences;
- auto-correct changes structure only, and one undo reverts it;
- every kind of decision, including that it survives re-analysis;
- image move and remap;
- split at page / merge back, delete with undo, reorder detection and fix, rename, add and lock;
- reports, final validation and the EPUB build.
