# VALIDATE & AUTO-FIX EPUB — closed-loop, data-safe repair

This is an upgrade of the existing *Full Auto Repair* (`core/epub/repair_engine.py`), not a second system. It reuses the existing parts:

- EPUBCheck runner and parser;
- Quick Validator;
- error analyzer;
- package reader and builder;
- the existing repair strategies;
- the XHTML tag-repair strategy;
- the QC engine, when a source PDF is given.

## Where to use it

- **Validation (launcher card 02):** open the EPUB, optionally choose the **Source PDF**, then click **VALIDATE & AUTO-FIX EPUB**. It runs in the background with live progress, then the dashboard opens.
- **EPUB Structure (card 04):** the **Validate & Auto-Fix output** option is on by default. The packaged EPUB is validated and repaired right after Generate & Validate.
- **PDF ↔ XHTML QC (card 05):** **Build EPUB…** runs the same loop on the built file, PDF-aware.

The original EPUB is never modified. Next to it you get:

```
book.epub                      original - only read (sha256 verified again at the end)
book.repaired.epub             the final, validated package
book_autofix/
  repair_backup/original.epub  immutable backup (+ original.sha256, inventory_before.json)
  working/step_NNN_*.epub      every intermediate state, one file per transaction
  repair_history/              repair_plan.txt, report.html, report.json, repair_history.json,
                               epubcheck_before.json, epubcheck_after.json, integrity.json,
                               pdf_comparison.html (when a PDF is given)
```

## The loop

```
BACKUP -> INVENTORY -> EPUBCHECK -> ROOT-CAUSE REPAIR PLAN
  -> package hygiene (temp / OS files are never packaged)
  -> PASS n: LEVEL 1 repairs, each its own transaction -> EPUBCheck
             (if the batch made EPUBCheck worse: replay one at a time, reject the culprit)
           LEVEL 2 repairs, each its own transaction + EPUBCheck immediately
  -> repeat until EPUBCheck is clean or only manual-review issues remain
  -> PDF stage (page markers / image mapping verified against the PDF)
  -> FINAL PACKAGE -> FINAL EPUBCHECK ON THE PACKAGED FILE -> FINAL INTEGRITY CHECK
  -> DASHBOARD + REPORTS
```

### What a transaction checks

Every repair is applied to a copy of the current state. It is **committed** only when all of these hold:

1. **Content integrity, compared against the original.** Nothing may have disappeared: text words, paragraphs, headings, lists, tables, figures, notes, images, image references that resolved, links, links that resolved, IDs, page markers, stylesheet links, metadata entries, manifest declarations, spine documents, navigation entries, NCX navPoints, CSS rules, and the bytes of every image, font and media file. A file's hash changing is not a loss in itself, because valid repairs change files. Content is compared semantically.
2. **The Quick Validator is not worse.**
3. **EPUBCheck is not worse** (LEVEL 2 repairs, and LEVEL 1 batches). Errors are counted per location, because EPUBCheck's `nError` counts a message once even when it lists many places.

Otherwise the repair is **rolled back**, and the reason is recorded in the history.

## Levels

| Level | Applied | Repairs |
|---|---|---|
| 1 SAFE | automatically, batched | mimetype; undeclared resource; manifest href; duplicate spine entry; named entities → numeric references; unclosed / mis-nested tags (minimal); URI syntax (backslash, spaces, absolute paths); missing `dcterms:modified`; `unique-identifier`; NCX playOrder; CSS missing `}` or `*/`; junk files |
| 2 HIGH CONFIDENCE | automatically, one transaction each, EPUBCheck immediately | duplicate IDs (with reference analysis); broken #fragments; missing resources (case, wrong folder, stale split file); media type; a `role` not allowed on a link; PDF-verified page markers and images |
| 3 MEDIUM | never: suggestion + recommended action | OPF / navigation / markup problems without a single certain fix |
| 4 LOW | never: manual review | unknown codes, accessibility content, a missing ISBN |

Every repair is a **minimal textual edit**: the exact attribute, entity, element, brace or spine entry. Documents are never re-serialized or pretty-printed, so whitespace, attribute order, comments and the DOCTYPE stay byte-identical.

Nothing is invented and nothing is deleted to silence an error:
- A missing image file, an id that exists nowhere and a missing ISBN stay as **NEEDS REVIEW**, with the reason.
- The reference is never removed.

### Duplicate IDs

1. Every reference to the duplicated id (content links, navigation, NCX) is collected.
2. The occurrence the references identify keeps the id. A reference identifies an occurrence when its link text matches that element's text; element kind is the tie-breaker (a heading over a paragraph).
3. Every other occurrence is renamed deterministically (`id-2`, `id-3`, …).
4. A reference whose text identifies a renamed occurrence is redirected to it.

### Fragments

A fragment is repaired only when exactly one target is certain:
- URL-encoding;
- letter case;
- the id moved to another document by a split;
- the id renamed (separators such as `fig-1` → `fig1`).

### Missing resources

A missing file is resolved only when exactly one existing file is meant:
- letter case of the path;
- a backslash or absolute filesystem path;
- a stale split file name, resolved through the unique document that holds the `#fragment`;
- a wrong folder, resolved through the unique file with that name.

## Client rules (CUPEPUB)

With a client profile, the publisher's own validation rules run as well. This is the default in the Validation window, EPUB Structure and QC; the profile is `CUPEPUB`, i.e. the client's SPiXVali tool. The rules run after the EPUBCheck loop:
- every client-rule repair is a LEVEL 2 transaction, with the integrity check and EPUBCheck;
- the client rules are validated again after each committed repair;
- the delivered package is validated at the end, as a copy named `<ISBN>.epub` in `client_delivery/`.

The dashboard gains a **Client rules** tile and tab, and the history folder gets `client_validation_before.log` / `client_validation_after.log` in the client tool's own log layout.

See [CLIENT_RULES_CUPEPUB.md](CLIENT_RULES_CUPEPUB.md).

The integrity guard accepts the client repairs only as narrowly declared changes:
- a visible text edit (the landmarks heading);
- an added landmark label;
- a corrected metadata value;
- an image header whose decoded pixels are identical.

Wrapping text in a link or a span never changes the compared words: words are separated only at block boundaries.

## Root causes

Several EPUBCheck messages with one cause become one group. For example, every "fragment not defined" pointing *into* a document that is not well-formed is a symptom of that document's XML error. It is shown as a symptom, and re-checked after the XML is repaired.

Groups are processed in dependency order:

package / container → OPF → missing resources → URIs → XML → duplicate IDs → fragments → links → navigation → CSS

**Loop prevention:** a strategy that changes files twice without reducing its own errors is stopped. Its issues are marked as a REPAIR LOOP and left for manual review.

## Success

**PASS – VALIDATED** is shown only when both of these hold for the final packaged `.epub`:
- EPUBCheck reports no fatal errors or errors;
- the content-integrity check passes.

Otherwise the result is one of:
- **NEEDS REVIEW**: each remaining issue is listed with its EPUBCheck code, file, line, message, root cause, the attempted repair, why it was not fixed, and the recommended manual action.
- **FAIL**: the content-integrity check failed. The original is then kept as the output.

If EPUBCheck is not installed, the result is always NEEDS REVIEW. EPUBCheck needs Java and `tools/epubcheck/epubcheck.jar`.

## Generator fixes found by the loop

Running the loop on EPUB Structure output found three generator bugs, now fixed at the source:

1. **Navigation links:** a new `nav.xhtml` was written next to the OPF, while its links assumed it sat next to the content documents, so every navigation link broke when the content lived in `xhtml/`.
2. **Identifier:** `unique-identifier="pub-id"` while the identifier element had `id="isbn-id"` (OPF-030).
3. **Landmarks:** `role="doc-chapter"` on landmark links, which is not allowed on `<a>`.

With an ISBN, EPUB Structure output now passes EPUBCheck as generated. Older packages with these bugs are repaired by the loop.

## Tests

`tests/test_epub_autofix.py` uses `tests/fixtures/broken_epub.py`, which has 17 defects that can each be switched on by name. It covers:

- the integrity guard detecting every kind of loss;
- each strategy being minimal and lossless;
- unresolvable references never being invented;
- each safe defect being cleared by real EPUBCheck;
- the full closed loop;
- a destructive repair being rolled back;
- a repair that makes EPUBCheck worse being rolled back;
- the EPUBCheck-unavailable path;
- EPUB Structure output passing EPUBCheck as generated.

The EPUBCheck tests skip when EPUBCheck is not installed.
