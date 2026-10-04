# CUPEPUB Auto Zone / Auto Tag / Character-Formatting Engine

Scope: **CUPEPUB profile only.** XML and EPUB profiles behave exactly as before
(verified byte-identical output); the engine's menu refuses to run on them and
character decorations are only extracted while CUPEPUB is active.

Menu: **File › Auto Zone / Auto Tag (CUPEPUB)**.

## Pipeline

```
PDF page
 └ core/text_extractor          cached rawdict, exact characters (unchanged)
 └ core/underline_detector      vector / thin-rect / quad / annotation / raster segments
                                -> exact character ranges (underline, strike)
 └ auto_zoning/layout_engine    glyph lines, running heads, page numbers, figures
                                (images + vector clusters), tables, footnote rule,
                                bands/columns (gutter-crossing lines split bands),
                                lists, paragraphs, verse lines, reading order
 └ auto_zoning/semantic_classifier   multi-signal role candidates + evidence,
                                contextual pass (sources, verse runs, references)
 └ core/tag_knowledge           TAG KNOWLEDGE MODEL (see below)
 └ auto_zoning/auto_tag_engine  role x tag x structure scoring, next-valid fallback,
                                confidence breakdown, NEEDS REVIEW
 └ auto_zoning/smart_auto_zone  page / document runs, caches, resume, cancel,
                                lock + manual-override protection
 └ ZoneManager (existing)       zones carry tag + attributes; text keeps inline markup
 └ EpubXmlGenerator -> core/tag_normalizer -> Mapping.xml -> XHTML  (existing path)
 └ core/auto_validation         9-stage validation report
```

## Tag Knowledge Model (`core/tag_knowledge`)

No tag names are hard-coded in the engine. Semantic **roles** (paragraph, verse
line, footnote, ...) are described as concept words in
`profiles/CUPEPUB/semantic_roles.json` and resolved to the project's own tags by
matching against:

| Source | What it contributes |
|---|---|
| `CUPEPUB_Zoning.xml` + `CUPLookup.xml` (via `cup_config`) | tag inventory, labels, attributes, categories, image tags |
| `Mapping.xml` | families (enclose rules), parent/child XPaths, section openers, output class / epub:type / role tokens, reverse map XHTML → zone tag |
| `profiles/CUPEPUB/*.dtd` or *Load Project DTD* | content models, allowed children, ordering (Glushkov follow sets), cardinality, required / enumerated attributes, full validation |
| `profiles/CUPEPUB/reference_xml/` or *Load Reference XML Corpus* | frequencies, succession probabilities, parent/child, attributes, text patterns, inline formatting usage — across all files |
| Reference projects (existing *Load Reference Project*) | tags used in real zoned projects |
| User corrections | retagging an auto zone records role → tag preferences (`settings["auto_tag_learning"]`) |

*File › Auto Zone / Auto Tag › Tag Knowledge Model…* prints the derived model.

## Priority and protection

`USER MANUAL OVERRIDE > LOCKED > AUTO TAG MODEL > SEMANTIC > LAYOUT > OCR`

* Retagging or resizing an automatic zone sets `manual_override`; Auto Zone /
  Auto Tag never change it again (Re-analyse only replaces the engine's own
  unprotected zones).
* **Lock Zone** (right-click) — locked zones survive Auto Zone, Auto Tag,
  re-analysis and forced retagging.
* Hand-drawn zones are protected; candidates overlapping them are dropped.

## Zone model extensions

`Zone` keeps its format; new state lives in `attributes` and is exposed as
properties: `locked`, `manual_override`, `protected`, `confidence`,
`needs_review`, `auto_role`, `formatting_ranges`. Engine bookkeeping never
reaches generated output.

## Character formatting

* `core/underline_detector.py` maps each candidate segment to the characters
  whose own advance box it covers (≥ 50 %). Partial words stay partial, single
  letters stay single, separate rules stay separate ranges, spaces are
  decorated only when the same rule runs through them. Long rules that are
  mostly not under text (table / heading / footnote rules) are rejected.
* Scanned pages: underline ink runs are detected in the rendered line image
  (shape tests reject glyph bars and descender bowls) and mapped to glyph
  columns recovered from the same image (width-aware alignment, monospace
  aware). Searchable scans use the native glyph boxes with raster segments.
* `core/formatting_ranges.py` — markup ⇄ (plain text, ranges) model;
  `core/tag_normalizer.py` merges identical adjacent ranges, removes empty /
  duplicate formatting, keeps separate ranges separate, guarantees identical
  text.

## Confidence and review

Each decision stores `confidence` (0–100) and `confidence_breakdown`
(layout, formatting, semantic, tag, structure), evidence and alternatives.
Low confidence, ambiguity, or a structural fallback sets **NEEDS REVIEW**
(magenta zone, `F7` jumps to the next one, right-click › *Show Auto Tag
Decision…* / *Mark as Reviewed*).

## Debug overlays

*Debug Overlays* submenu: glyph boxes, spans, layout blocks with role scores,
reading-order path, underline/strike vectors with the exact characters
assigned, table/figure boundaries, decision + confidence labels, parent/child
links.

## Performance

Page analyses (layout + roles, never tag decisions) are cached in memory and
in `layout_cache/<pdf fingerprint>/`. Changing a tag or a formatting decision
never re-runs layout analysis or OCR; Auto Zone never starts OCR (use the
existing *Prepare OCR Cache* for scans). *Auto Zone Document* runs on a worker
thread with its own PDF handle, shows progress, can be stopped and resumes
from `settings["auto_zone_document_state"]`.

## Tests

`python -m pytest tests -q` — synthetic regression book
(`tests/fixtures/regression_doc.py`) covering full / partial / single-character
/ multiple / phrase / punctuation / accented / combined underlines, strike,
verse, paragraph boundaries, two-column reading order, figures, captions,
footnotes, lists, locking, overrides, DTD constraints, corpus statistics and
normalization. Real PDFs go in `tests/regression/pdfs/` with an
`.expected.json` (see the README there).

## References, endnotes and cross-page continuations

**Sections** (`auto_zoning/section_context.py`): the whole PDF text layer is scanned once (cached as
`sections.json`) for section headings - *References, Bibliography, Works Cited, Sources, Further Reading…*
and *Notes, Endnotes, Notes to Chapter N…* - recognised by their own typography (bigger or bold, short,
on their own line). A section runs until the next heading of the same or higher rank, across pages; a
smaller sub-heading inside it (e.g. "Chapter 1" inside the Notes) does not end it. Inside a section:

| Block | CUPEPUB tag |
|---|---|
| the section heading | `RefHead` / `EnHead` |
| reference entry starting with a number (`1.`, `[1]`, `(1)`) | `Ref_N` |
| author-date reference entry | `Ref_D` |
| note | `Endnotes` |

Both note layouts are handled:

* **notes at the end of each chapter** - each chapter's own "Notes" heading opens a section that the next
  chapter heading closes;
* **all notes at the end of the book, grouped by chapter** - one "Notes" title, then a bold group heading
  per chapter ("Introduction", "1 'Jewels of Women'") with numbering restarting at 1. Group headings
  inside a notes section become `EnHead` (references: `RefHead`); the XHTML generator turns each
  `EnHead` + following `Endnotes` run into its own numbered endnote group.

Reference lists and notes are usually set with a hanging indent - number / first line out, turn-over
lines in - the opposite of body paragraphs, so the paragraph grouper both merges entries and splits at
every turn-over line. Inside a section each run of text blocks is therefore re-segmented from its PDF
lines into exactly one zone per entry. The text column is found from the line positions; only a line
that sticks out to the left of it opens a new entry (right-aligned numbers "4" / "10" may sit at
different positions), so a turn-over line that happens to begin with a number ("1898 in a letter…")
stays part of its note. Running heads that change on every page ("128 Notes to page 3") and "This page
intentionally left blank" are never zoned.

**Continuations** (`auto_zoning/continuity.py`): after a page is zoned, the first content zone of each
flow on the page (main text, notes) is compared with the last one before the page break. Signals are
weighted and summed into a confidence (previous text stops mid-sentence or ends with a broken word, next
starts lowercase, next first line not indented / indented, previous last line full / short, and for
notes and references: next starts with an entry number or a new "Surname, X" entry, next first line at
the hanging-indent position). At 80% or more the pair is linked with an ordinary **Merge Previous**
(yellow; the reasons are stored in `auto_continuation_reason`). A heading at the top of a page never
continues anything. **Unmerge** undoes a link, and that pair is never linked automatically again.

*Auto Zone → Link Page Continuations (whole document)* runs the same detection over an already zoned
project (manual zones included) without re-zoning. Settings: `auto_zone_sections`,
`auto_zone_continuations` (both on by default).
