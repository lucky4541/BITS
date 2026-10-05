# BITS Tool

Desktop tool for converting book and article PDFs to **BITS 2.2** (book) and
**JATS 1.4** (journal article) XML:

```
PDF ─► zoning & tagging (manual / Auto Zone / Auto Tag, OCR for scans)
    ─► XML generator ─► BITS book / JATS article structure
    ─► DTD validation + safe auto-fix ─► <prefix>.xml + images/ + <prefix>_validation.txt
    ─► PDF ↔ XML compare (proof)
```

Tag library: <https://jats.nlm.nih.gov/extensions/bits/tag-library/2.2/index.html>

## Start

```
pip install -r requirements.txt
python main.py
```

The launcher has three cards:

| Card | What it does |
|---|---|
| **01 Zoning & Tagging** | Open a PDF, draw/auto-detect zones, tag them, *Generate XML* |
| **02 XML Validation** | Validate an existing BITS / JATS file against its DTD; *Validate & Auto-Fix* writes `<name>.fixed.xml` (the input is never changed) |
| **03 PDF ↔ XML Compare** | Proof a generated BITS / JATS file against the source PDF (missing / extra / changed text, formatting) |

## Profiles

Choose the profile in the zoning window: **BITS** (book) or **JATS** (article).
The tag buttons are defined in `core/bits/vocabulary.py`; the profile files
`profiles/bits_profile.json` and `profiles/jats_profile.json` are generated
from it:

```
python -m core.bits.vocabulary profiles
```

### BITS output

| Zone tags | Output |
|---|---|
| Book Title, Book Subtitle, Author, Editor, Affiliation, Series Title, Publisher, Publisher Place, ISBN, Copyright, Edition | `book-meta` |
| Dedication / Foreword / Preface / Acknowledgments / Contents / Front Matter Title | `front-matter` (`dedication`, `foreword`, `preface`, `ack`, `toc`, `front-matter-part`) |
| Part Title, Chapter Title, Introduction Title | `book-body/book-part` (`book-part-type="part" / "chapter"`), with `book-part-meta/title-group` (label + title), chapter authors, abstract, keywords |
| Heading 2 – 6 | nested `sec` inside the chapter `body` |
| Footnote / Endnote | `fn-group` in the chapter's `back` |
| Appendix / Notes / Glossary / Bibliography / Index Title | `book-back` (`book-app-group`, `notes`, `glossary`, `ref-list`, `index`) |
| Reference | `ref/mixed-citation` |
| Index Entry / Subentry / Sub-subentry | `index-entry` (nested by level) |
| Page Number | `<target target-type="pagenum" id="pageN"/>` inside the nearest paragraph |

### JATS output

`article/front/article-meta` (Article Title, Subtitle, Author, Affiliation,
Corresponding Author, Author Notes, Abstract, Keywords, dates, Copyright, DOI),
`body` with nested `sec` from Heading 1 – 6, `back` (`ack`, `app-group`,
`fn-group`, `ref-list`, `glossary`). `journal-meta` is filled from the project
setting `bits_meta` (journal id / title / ISSN / publisher) or placeholders.

### Tag-button attribute convention

| Attribute on a tag button | Effect |
|---|---|
| `part_type` (on an `h1`) | the kind of book part / section it opens (`chapter`, `part`, `preface`, `foreword`, `introduction`, `dedication`, `ack`, `toc`, `appendix`, `notes`, `glossary`, `bibliography`, `index`) |
| `@name` | copied to the XML element as attribute `name` (e.g. `@contrib-type`, `@index-level`) |
| `list_type` | list numbering (`order`, `alpha-upper`, `roman-lower`, `simple`, ...) |
| `asset_kind` | marks image zones (figure / graphic / equation) |
| `tag_label` | internal: which button was used (Auto Tag learning) – never written to the output |

## DTDs

* **JATS 1.4** (Journal Publishing, MathML 3) is bundled in `profiles/JATS/dtd/`.
* **BITS 2.2** must be installed once: download the *BITS 2.2 DTD* package from
  <https://jats.nlm.nih.gov/extensions/bits/2.2/>, unzip it into
  `profiles/BITS/dtd/` (any `BITS-book*.dtd` in that folder or a sub-folder is
  found; the MathML 3 variant is preferred) – or choose the DTD file in
  *Settings › BITS 2.2 DTD file*.
  Until then BITS output is still generated and checked for lost text, and the
  status says `NOT VALIDATED - DTD not installed`.

The DOCTYPE written to the output uses the DTD's own public identifier.

## Validation and auto-fix

Every *Generate XML* run validates the result against the DTD and applies
only **data-safe** fixes (`core/bits/autofix.py`):

* unknown elements → `named-content` / `p content-type="…"` / `boxed-text`
  (the text is kept);
* undeclared attributes removed, enumerated values mapped
  (`list-type="number"` → `order`), required attributes and ids added;
* loose text and inline elements wrapped in `p`, paragraphs split around
  blocks, page markers moved into the nearest paragraph;
* blocks after a `sec` moved into it; metadata containers reordered to the
  DTD order – reading order of the text is **never** changed;
* required empty children added.

After each pass the words of the document are compared with the input; a pass
that would change any text is reverted. The status is one of `VALID`,
`NEEDS REVIEW` (remaining DTD errors are listed), `NOT VALIDATED - DTD not
installed` or `FAIL - text changed`; the details go to
`<prefix>_validation.txt` next to the XML.

## Auto Zone / Auto Tag

*File › Auto Zone / Auto Tag (BITS / JATS)* – layout analysis, semantic roles
(`profiles/BITS/semantic_roles.json`, `profiles/JATS/semantic_roles.json`),
tag decisions with confidence and NEEDS REVIEW, character formatting
(bold / italic / underline / strike), reference and note sections and page
continuations. See [docs/AUTO_ZONE_ENGINE.md](docs/AUTO_ZONE_ENGINE.md).

## Code

| Path | |
|---|---|
| `core/bits/vocabulary.py` | tag buttons of the BITS / JATS profiles |
| `core/bits/structure.py` | zone XML → BITS book / JATS article |
| `core/bits/autofix.py` | DTD-driven safe auto-fix |
| `core/bits/dtd.py` | DTD lookup, loading, DOCTYPE |
| `core/bits/pipeline.py` | generate / validate / fix / preview |
| `core/xml_generator.py` | zones → intermediate XML |
| `app/xml_validation/` | launcher card 02 |
| `core/fidelity_compare/` | PDF ↔ XML compare |

## Tests

```
python -m pytest tests -q
```

`tests/test_bits_output.py` builds a synthetic 5-page book, zones it with the
BITS and JATS tags and checks structure, DTD validity (JATS always; BITS once
its DTD is installed), auto-fix rules and that no text is ever lost.

## Build

`python build_exe.py` (PyInstaller, `BITSTool.spec`) – the packaged profiles
and DTDs are verified after the build.
