# Client rules: CUPEPUB (the client's SPiXVali validation)

The client validates every delivery with its own Windows tool, **SPiXVali** (Client `CUPEPUB`, rules `EPUB-001` to `EPUB-051` in its `Rule_List.xml`). This application runs the same rules natively, as a separate validation next to EPUBCheck. They are in `core/epub/client_rules/cupepub.py`.

The rules were rebuilt from the tool itself, from `Cls_CUP_EPUB.Validate` in `SPiXVali.dll` and its configuration. They use the same regular expressions, the same XPath conditions and the same exemptions, so the counts match the tool's log. The one exception is EPUB-022, the package size: its limit is not in the tool's configuration, so it is not checked.

**Severity** follows `Rule_List.xml`: `{1,1,1}` = Error, `{2,2,2}` = Warning.

## Where

**Validation window (launcher card 02)**
- **Client rules:** choose `CUPEPUB` (the default) or `(none)`.
- **Client Validate:** runs only the client rules. Each finding becomes a row in the table, with the automatic repair or manual action for that rule.
- **Client tool log (optional)…:** a SPiXVali log for this EPUB. Its counts per rule are compared with the native rules, in a message box here and in the dashboard.
- **VALIDATE & AUTO-FIX EPUB:** with a profile chosen, the client rules are validated and repaired as part of the closed loop.

**EPUB Structure (card 04) and PDF ↔ XHTML QC (card 05)**
- The automatic validate & auto-fix after generation also applies the CUPEPUB rules.

## What is repaired automatically

Every repair is a transaction of the closed-loop engine (`core/epub/client_rules/cupepub_fixes.py`). It is committed only when all of these hold:
- the content-integrity check passes against the original;
- EPUBCheck is not worse;
- the edit is minimal and textual (documents are never re-serialized).

| Rule | Repair |
|---|---|
| EPUB-010 junk characters | Every non-ASCII character in XHTML is written as `&#xHHHH;`. The text is identical; the byte-order mark is removed.<br>Reason: the tool reads each file as UTF-8 **and as UTF-7**, so any literal `é`, `©` or non-breaking space is reported.<br>U+FFFD (a character already lost) is listed for review. |
| EPUB-009 link missing | "chapter 3", "Figure 1.1", "equation 3", "Part II", "[12]" and URLs are **linked when exactly one valid target exists**:<br>• the `_ch3` / `_pt2` file;<br>• the figure, table or equation with that number (by caption or id);<br>• the numbered reference;<br>• a complete URL or e-mail address.<br>**Otherwise the text is wrapped in `<span>…</span>`**, which the tool does not report, and the item is listed for review. A reference is never linked to a guess. |
| EPUB-043 index page numbers | Locators become links to their page markers (`#Page_N`). `45n3` is linked as a whole to page 45. A number with no page marker, or a number in the entry text (e.g. "Treaty of 1863"), is wrapped in a span and listed for review. |
| EPUB-044 / 045 ranges | Ranges are linked **separately**, and an abbreviated end goes to its full page: `123–25` becomes `<a …Page_123>123</a>–<a …Page_125>25</a>`.<br>One link over a whole range is split. A wrong range end (`#Page_22` for 122) is repointed. |
| EPUB-046 roman pages | Roman locators (`vii–viii`) are linked when that page marker exists. |
| EPUB-047 see / see also | The text after `<i>see</i>` / `<i>see also</i>` is linked to the index entry with that head word. If the entry has no id, one is added (`idx-church`). An unknown entry is wrapped in a span. |
| EPUB-049 | `<a>45</a>n3` becomes `<a>45n3</a>`. The text is unchanged. |
| EPUB-042 | `Table <a>3</a>` becomes `<a>Table 3</a>`. |
| EPUB-001 / 014 identifiers | The ISBN identifier is written as `<dc:identifier id="isbn-id">urn:isbn:…</dc:identifier>` with identifier-type 15.<br>The **print ISBN from the copyright page** is added as `<dc:source id="src-id">` with identifier-type 15 and `source-of` pagination. |
| EPUB-023 | Cover image item `id="cover-image" properties="cover-image"` and `<meta name="cover" content="cover-image"/>`. |
| EPUB-035 / 037 | **OPF guide:** Cover / Table of Contents / **Begin reading** / Index.<br>**Navigation landmarks:** `<h2 id="landmarks">Book Landmarks</h2>`, `<ol class="none">`, and the entries Cover Page / Contents / Begin Reading / Index.<br>The tool reads Begin Reading as `epub:type="part"`. |
| EPUB-041 | `dc:creator` is set to the author printed on the title page (`<p class="bookauthor">`), and file-as is added. A different name is never overwritten: it goes to review. |
| EPUB-024 | The role that matches the epub:type (`doc-chapter`, `doc-preface`, `doc-pagebreak`…), only on elements where that role is allowed. |
| EPUB-025 | `<title>` set to the chapter number and title of the `<header><h1>`. |
| EPUB-011 | A double-escaped reference shown as text (`&amp;#x2019;`) becomes the character. |
| EPUB-019 DPI | The required DPI is written into the image header: cover 300, inline / icon 135, others 150.<br>**The pixels are proven identical** (decoded pixel hash); an image is never resampled. |
| EPUB-021 cover size | The cover is scaled (Lanczos, aspect ratio kept) to **1200 px wide or 1800 px high, never larger**, and saved at 300 DPI. For example, 700 × 1014 becomes 1200 × 1738. This is the only repair that changes pixels; it is declared to the integrity check with the exact new size. |
| EPUB-038 / 005 / 006 | `linear="no"` is removed. The mimetype line break is removed. Reading-system files are taken out of META-INF; `encryption.xml` and the like are kept and go to review. |
| EPUB-001 / 002 file name | A copy named **`<ISBN>.epub`** is written to `<name>_autofix/client_delivery/` and validated under that name. |

## What stays for manual review

Each item is listed in the dashboard's **Client rules** tab and in the report, with the reason and the recommended action:
- **EPUB-024:** `role="doc-cover"` is not allowed on `<section>` (EPUBCheck RSC-005).
- **EPUB-026 / 032:** a repeated page marker is never deleted.
- **EPUB-013:** alt text is never invented.
- Renames: **EPUB-004, 016, 017, 018**.
- Index locators with no page are wrapped in spans, but the tool's index rules still report them. They are listed for a check against the print index.

## Images when the EPUB is generated (EPUB Structure)

With **Images: 150 dpi, cover 300 dpi + 1200x1800** (on by default), every PNG / JPEG is written into the packaged EPUB as the client requires (`core/epub/client_rules/images.py`):
- **Ordinary images:** 150 DPI. Inline / icon images get 135 DPI, as the client's tool expects. The DPI goes into the image header; the pixels are unchanged.
- **The cover (`cover.jpg`):** 300 DPI, scaled to 1200 px wide or 1800 px high, never larger.

Only the packaged copy changes; the project's own image files are never modified. The report lists every image in the section **IMAGES (CLIENT: 150 DPI, COVER 300 DPI + 1200 x 1800)**.

## OPF in the client's format (EPUB Structure)

The generated OPF follows the client's sample. Element order:
1. title (display-seq);
2. creator(s) (role aut, file-as "Wimsatt, James I.", display-seq);
3. accessibility metadata;
4. language;
5. publisher;
6. `dc:identifier id="isbn-id"` (identifier-type 15);
7. `dc:source id="src-id"` (identifier-type 15, source-of pagination);
8. `dc:date` (`2006-01-01T00:00:00Z`);
9. `dcterms:modified`;
10. `dc:rights`;
11. `<meta name="cover" content="cover-image"/>`.

After the metadata:
- **Manifest:** the cover image item has `id="cover-image"` and `properties="cover-image"`.
- **Guide:** Cover / Table of Contents / Begin reading / Index.

The values come from the book's own pages (`core/epub/client_rules/front_matter.py`):
- **Title page:** `booktitle`, `booksubtitle`, `bookauthor` (or the first heading / a "by …" line).
- **Copyright page:**
  - `© University of Toronto Press Incorporated 2006` gives the rights, the year, and the publisher (without "Incorporated" / "Inc." / "Ltd.").
  - Every `ISBN … (cloth)` / `ISBN … (EPUB)` line is read: the e-book ISBN becomes the identifier, the print ISBN the source.

ISBNs typed into the EPUB Structure window always win; an empty field is filled from the copyright page.

The identifier id is `isbn-id`, not the sample's `pub-id`. The client's tool reads the ISBN only from `<dc:identifier id="isbn-id">` (EPUB-001 / 014).

## Tests

`tests/test_client_rules.py` uses `tests/fixtures/cup_book.py`, a CUP-style book with the problems from a real SPiXVali log. It covers:
- the log parser;
- the native rules;
- each repair (with the integrity check);
- the client-format OPF generated from the title and copyright pages (EPUBCheck valid);
- the full closed loop with the client rules: 53 client findings go to 6 manual-review items, and EPUBCheck goes from 1 error to 0.
