"""Automatic repairs for the CUPEPUB client rules (core.epub.client_rules.
cupepub). Same contract as core.epub.autofix_strategies: fn(mp, package) ->
RepairActionResult with .changes / .review / .allowed, every edit textual
and minimal, each run by the closed-loop engine as its own transaction
(content integrity + EPUBCheck must not get worse, otherwise rolled back).

LINKS (EPUB-009, EPUB-043 ... 047): a reference / locator is linked ONLY to
a target that exists - the chapter / part file, the figure / table /
equation / section with that number, the page marker (#Page_N) of that
page, the index entry that "see"/"see also" names, a real URL / e-mail.
When no single target exists the text is wrapped in <span>...</span>
instead, so it is marked as checked and never linked to a guess. Ranges
are linked separately ("123-25" -> Page_123 and Page_125).

JUNK (EPUB-010): every non-ASCII character in an XHTML file is written as a
numeric character reference (&#x2019;) - the identical character, so the
text does not change; a byte-order mark is removed. U+FFFD (a character
already lost before this package) is listed for review."""
import io
import posixpath
import re
import struct
import zlib

from core.epub.autofix_strategies import _change, _result
from core.epub.client_rules import cupepub as C
from core.epub.client_rules import front_matter as FM
from core.epub.client_rules import images as IMG
from core.epub.client_rules import rawmarkup as R
from core.epub.integrity_snapshot import Allowed, pixel_hash

SPAN_OPEN, SPAN_CLOSE = "<span>", "</span>"


def _book(mp):
    files = {n: mp.get_bytes(n) for n in mp.order if mp.exists(n) and not n.endswith("/")}
    return C.Book(files, posixpath.basename(getattr(mp, "path", "") or ""))


def _review(file, reference, reason):
    return {"file": file, "reference": reference, "reason": reason}


def _set_text(mp, name, text):
    mp.set_bytes(name, text.encode("utf-8"))


# ================================================================ EPUB-010
_SKIP_RE = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>", re.S)


def to_ascii_refs(text):
    """Every non-ASCII character outside comments / CDATA / processing
    instructions as &#xHHHH;. Returns (text, converted count, skipped count)."""
    out, pos, n, skipped = [], 0, 0, 0

    def conv(s):
        nonlocal n
        res = []
        for ch in s:
            if ord(ch) > 127:
                n += 1
                res.append(f"&#x{ord(ch):04X};")
            else:
                res.append(ch)
        return "".join(res)
    for m in _SKIP_RE.finditer(text):
        out.append(conv(text[pos:m.start()]))
        skipped += sum(1 for ch in m.group(0) if ord(ch) > 127)
        out.append(m.group(0))
        pos = m.end()
    out.append(conv(text[pos:]))
    return "".join(out), n, skipped


def fix_junk_characters(mp, package):
    book = _book(mp)
    changes, review = [], []
    for name in book.xhtml:
        data = mp.get_bytes(name)
        if not C.non_ascii(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            review.append(_review(name, "encoding", "the file is not valid UTF-8 - convert it before the client "
                                                    "rules can be applied"))
            continue
        bom = text.startswith("﻿")
        text = text.lstrip("﻿")
        if "�" in text:
            for m in re.finditer("�", text):
                line, col = R.line_col(text, m.start())
                review.append(_review(name, f"line {line}:{col}", "U+FFFD replacement character - the original "
                                      "character was lost before this package; check it against the PDF"))
        new, n, skipped = to_ascii_refs(text)
        if skipped:
            review.append(_review(name, "comment / CDATA", f"{skipped} non-ASCII character(s) inside comments or "
                                                           "CDATA left as they are"))
        if new != data.decode("utf-8"):
            _set_text(mp, name, new)
            changes.append(_change(name, "document", f"{n} non-ASCII character(s)" + (" + BOM" if bom else ""),
                                   "&#xHHHH; numeric references",
                                   "EPUB-010: characters written as numeric references (identical text)", 0.99))
    return _result(changes, review)


# ================================================================ EPUB-011
_DOUBLE_RE = re.compile(r"&(?:amp|#x0?0026|#0?38);(#x[0-9A-Fa-f]{2,6}|#\d{2,7}|[A-Za-z][A-Za-z0-9]{1,31});")


def fix_double_escaped_references(mp, package):
    import html
    book = _book(mp)
    changes, allowed = [], Allowed()
    for name in book.names:
        if not name.lower().endswith((".xhtml", ".ncx")):
            continue
        raw = book.raw(name)
        edits = []

        def repl(m):
            ref = m.group(1)
            ch = html.unescape(f"&{ref};")
            if ch == f"&{ref};":
                return m.group(0)
            edits.append((f"&{ref};", ch))
            return f"&#x{ord(ch[0]):04X};" if len(ch) == 1 and not ref.startswith("#") else f"&{ref};"
        new = _DOUBLE_RE.sub(repl, raw)
        if new != raw:
            _set_text(mp, name, new)
            allowed.text_edits.setdefault(name, []).extend(sorted(set(edits)))
            changes.append(_change(name, "document", ", ".join(sorted({e[0] for e in edits}))[:120],
                                   "the characters they stand for",
                                   "EPUB-011: double-escaped character reference shown as text", 0.95))
    return _result(changes, allowed=allowed)


# ================================================================ OPF
def _opf(mp, book):
    return book.opf, book.opf_text()


def _front(book):
    docs = [(n, book.root(n)) for n in book.xhtml]
    return FM.extract(docs)


def _insert_in_metadata(opf, block):
    m = re.search(r"\n?([ \t]*)</metadata>", opf)
    indent = "    "
    if m is None:
        return opf, False
    prev = re.findall(r"\n([ \t]+)<", opf[:m.start()])
    if prev:
        indent = prev[-1]
    ins = "".join(f"\n{indent}{line}" for line in block)
    return opf[:m.start()] + ins + opf[m.start():], True


def _rename_id_refs(opf, old, new):
    opf = re.sub(r'(\sid=")%s(")' % re.escape(old), r"\g<1>%s\2" % new, opf)
    opf = re.sub(r'(\srefines="#)%s(")' % re.escape(old), r"\g<1>%s\2" % new, opf)
    opf = re.sub(r'(\sunique-identifier=")%s(")' % re.escape(old), r"\g<1>%s\2" % new, opf)
    return opf


def fix_opf_identifiers(mp, package):
    """EPUB-014 / 001: <dc:identifier id="isbn-id">urn:isbn:EBOOK</dc:identifier>
    (+ identifier-type 15) and the print ISBN as <dc:source id="src-id"> with
    identifier-type 15 and source-of pagination - taken from the copyright
    page; never invented."""
    book = _book(mp)
    name, opf = _opf(mp, book)
    if not name:
        return _result([])
    changes, review, allowed = [], [], Allowed()
    before_entries = _metadata_entries(opf)
    new = opf
    ident = re.search(r"<dc:identifier\b([^>]*)>\s*((?:urn:isbn:)?\s*[0-9Xx][0-9Xx\- ]{8,20})\s*</dc:identifier>", new)
    fm = _front(book)
    if ident is None:
        ebook = fm.ebook_isbn()
        if not ebook:
            review.append(_review(name, "dc:identifier", "no ISBN identifier in the OPF and no e-book ISBN on the "
                                                         "copyright page - add the e-book ISBN"))
            return _result([], review)
        uid = re.search(r'unique-identifier="([^"]+)"', new)
        block = [f'<dc:identifier id="isbn-id">urn:isbn:{ebook}</dc:identifier>',
                 '<meta refines="#isbn-id" property="identifier-type" scheme="onix:codelist5">15</meta>']
        new, ok = _insert_in_metadata(new, block)
        if uid and uid.group(1) != "isbn-id" and not re.search(r'\sid="%s"' % re.escape(uid.group(1)), new):
            new = new.replace(uid.group(0), 'unique-identifier="isbn-id"')
        changes.append(_change(name, "metadata", "", block[0], "EPUB-014: e-book ISBN from the copyright page", 0.9))
    else:
        attrs = R.attrs_of(ident.group(1))
        old_id = attrs.get("id", "")
        value = ident.group(2).strip()
        digits = re.sub(r"[^0-9Xx]", "", value)
        if old_id != "isbn-id" and not re.search(r'\sid="isbn-id"', new):
            if old_id:
                new = _rename_id_refs(new, old_id, "isbn-id")
            else:
                new = new.replace(ident.group(0), ident.group(0).replace("<dc:identifier", '<dc:identifier id="isbn-id"', 1))
                new = re.sub(r'unique-identifier="[^"]*"', 'unique-identifier="isbn-id"', new, count=1)
            changes.append(_change(name, "dc:identifier", f'id="{old_id}"', 'id="isbn-id"',
                                   "EPUB-001/014: the client tool reads the ISBN from id=\"isbn-id\"", 0.97))
        ident = re.search(r"<dc:identifier\b([^>]*)>\s*((?:urn:isbn:)?\s*[0-9Xx][0-9Xx\- ]{8,20})\s*</dc:identifier>",
                          new)
        tag_ok = re.match(r'<dc:identifier id="isbn-id">', ident.group(0))
        if not value.startswith("urn:isbn:") or not tag_ok or digits != value.replace("urn:isbn:", ""):
            fixed = f'<dc:identifier id="isbn-id">urn:isbn:{digits}</dc:identifier>'
            extra = {k: v for k, v in R.attrs_of(ident.group(1)).items() if k != "id"}
            if extra:
                review.append(_review(name, "dc:identifier", f"identifier has extra attributes {sorted(extra)}"))
            else:
                new = new.replace(ident.group(0), fixed, 1)
                changes.append(_change(name, "dc:identifier", ident.group(0), fixed,
                                       "EPUB-014: identifier written as urn:isbn:", 0.95))
        if not re.search(r'<meta[^>]+refines="#isbn-id"[^>]*property="identifier-type"', new):
            new, _ok = _insert_in_metadata(new, ['<meta refines="#isbn-id" property="identifier-type" '
                                                 'scheme="onix:codelist5">15</meta>'])
            changes.append(_change(name, "metadata", "", "identifier-type 15", "ISBN identifier type (ONIX list 5)",
                                   0.97))
    # print ISBN as dc:source
    ebook_digits = re.sub(r"[^0-9Xx]", "", (re.search(r'<dc:identifier id="isbn-id">([^<]+)<', new) or
                                            [None, ""])[1])
    src = re.search(r"<dc:source\b([^>]*)>([^<]*)</dc:source>", new)
    if src is None:
        pr = fm.print_isbn(exclude=(ebook_digits,))
        if pr:
            block = [f'<dc:source id="src-id">urn:isbn:{pr}</dc:source>',
                     '<meta refines="#src-id" property="identifier-type" scheme="onix:codelist5">15</meta>',
                     '<meta refines="#src-id" property="source-of">pagination</meta>']
            new, _ok = _insert_in_metadata(new, block)
            changes.append(_change(name, "metadata", "", block[0],
                                   f"EPUB-014: print ISBN from the copyright page ({fm.copyright_page})", 0.9))
        else:
            review.append(_review(name, "dc:source", "no print ISBN found on the copyright page - add "
                                                     "<dc:source id=\"src-id\">urn:isbn:PRINT</dc:source>"))
    else:
        sid = R.attrs_of(src.group(1)).get("id", "")
        if sid != "src-id" and not re.search(r'\sid="src-id"', new):
            if sid:
                new = _rename_id_refs(new, sid, "src-id")
            else:
                new = new.replace(src.group(0), src.group(0).replace("<dc:source", '<dc:source id="src-id"', 1))
            changes.append(_change(name, "dc:source", f'id="{sid}"', 'id="src-id"', "EPUB-014: print ISBN source id",
                                   0.95))
        block = []
        if not re.search(r'<meta[^>]+refines="#src-id"[^>]+>15</', new):
            block.append('<meta refines="#src-id" property="identifier-type" scheme="onix:codelist5">15</meta>')
        if not re.search(r'<meta[^>]+refines="#src-id"[^>]+>pagination</', new):
            block.append('<meta refines="#src-id" property="source-of">pagination</meta>')
        if block:
            new, _ok = _insert_in_metadata(new, block)
            changes.append(_change(name, "metadata", "", "; ".join(block), "EPUB-014: print ISBN properties", 0.95))
    if new != opf:
        _set_text(mp, name, new)
        for e in before_entries:
            allowed.metadata_changes[e] = "identifier id / form corrected (EPUB-014)"
    return _result(changes, review, allowed)


def _metadata_entries(opf):
    """The inventory's metadata entry strings that a rewrite of identifier /
    source / creator elements may legitimately change."""
    from core.epub.integrity_snapshot import parse_lenient
    root = parse_lenient(opf.encode("utf-8"))[0]
    out = []
    if root is None:
        return out
    for el in root.iter():
        p = el.getparent()
        if p is not None and isinstance(el.tag, str) and C._local(p.tag) == "metadata":
            ln = C._local(el.tag).lower()
            if ln in ("identifier", "source", "creator", "meta"):
                attrs = ",".join(f"{C._local(k)}={v}" for k, v in sorted(el.attrib.items()) if C._local(k) != "id")
                out.append(f"{ln}={' '.join((el.text or '').split())}|{attrs}")
    return out


def fix_cover_properties(mp, package):
    """EPUB-023: the cover image item id="cover-image" properties="cover-image"
    and <meta name="cover" content="cover-image"/>."""
    book = _book(mp)
    name, opf = _opf(mp, book)
    if not name:
        return _result([])
    changes, review = [], []
    items = [(m, R.attrs_of(m.group(1))) for m in re.finditer(r"<item\b([^>]*?)/?>", opf)]
    cover = next(((m, a) for m, a in items if "cover-image" in a.get("properties", "").split()), None)
    meta = re.search(r"<meta\b(?=[^>]*\bname=\"cover\")[^>]*>", opf)
    if cover is None and meta:
        cid = R.attrs_of(meta.group(0)).get("content")
        cover = next(((m, a) for m, a in items if a.get("id") == cid and a.get("media-type", "").startswith("image")),
                     None)
    if cover is None:
        cands = [(m, a) for m, a in items if a.get("media-type", "").startswith("image") and
                 re.match(r"^cover\.", posixpath.basename(a.get("href", "")).lower())]
        cover = cands[0] if len(cands) == 1 else None
    if cover is None:
        review.append(_review(name, "cover image", "no cover image found (cover.jpg / properties=\"cover-image\")"))
        return _result([], review)
    m, a = cover
    new = opf
    old_id = a.get("id", "")
    if old_id != "cover-image":
        if re.search(r'\sid="cover-image"', new):
            review.append(_review(name, "cover image", "id \"cover-image\" is already used by another element"))
            return _result([], review)
        new = re.sub(r'(\sid=")%s(")' % re.escape(old_id), r"\g<1>cover-image\2", new)
        new = re.sub(r'(\sidref=")%s(")' % re.escape(old_id), r"\g<1>cover-image\2", new)
        new = re.sub(r'(<meta\b[^>]*\bcontent=")%s(")' % re.escape(old_id), r"\g<1>cover-image\2", new)
        changes.append(_change(name, "manifest", f'id="{old_id}"', 'id="cover-image"', "EPUB-023: cover image id",
                               0.97))
    item = re.search(r"<item\b(?=[^>]*\bid=\"cover-image\")[^>]*?/?>", new)
    props = R.attrs_of(item.group(0)).get("properties")
    if props != "cover-image":
        if props is None:
            fixed = re.sub(r"\s*/?>$", lambda t: ' properties="cover-image"' + t.group(0), item.group(0), count=1)
        else:
            fixed = item.group(0).replace(f'properties="{props}"', 'properties="cover-image"')
            if "cover-image" not in props.split():
                pass
            else:
                review.append(_review(name, "cover image", f'properties="{props}" also kept other values'))
                fixed = item.group(0)
        if fixed != item.group(0):
            new = new.replace(item.group(0), fixed, 1)
            changes.append(_change(name, "manifest", item.group(0), fixed, "EPUB-023: cover-image property", 0.97))
    meta = re.search(r"<meta\b(?=[^>]*\bname=\"cover\")[^>]*>", new)
    if meta is None:
        new, _ok = _insert_in_metadata(new, ['<meta name="cover" content="cover-image"/>'])
        changes.append(_change(name, "metadata", "", '<meta name="cover" content="cover-image"/>',
                               "EPUB-023: cover meta", 0.97))
    elif R.attrs_of(meta.group(0)).get("content") != "cover-image":
        fixed = re.sub(r'content="[^"]*"', 'content="cover-image"', meta.group(0))
        new = new.replace(meta.group(0), fixed, 1)
        changes.append(_change(name, "metadata", meta.group(0), fixed, "EPUB-023: cover meta", 0.97))
    allowed = Allowed()
    if new != opf:
        _set_text(mp, name, new)
        for e in _metadata_entries(opf):
            if e.startswith("meta=") and "name=cover" in e:
                allowed.metadata_changes[e] = "cover meta points to cover-image (EPUB-023)"
    return _result(changes, review, allowed)


def _docs_by_role(book):
    """cover / toc / begin / index documents (paths)."""
    spine = _spine_docs(book)
    out = {}
    for n in spine:
        base = posixpath.basename(n).lower()
        types = {t for e in book.elements(n) for t in (C.attr(e, "epub_type") or "").split()}
        if "cover" not in out and (base.endswith("_cv.xhtml") or "cover" in types):
            out["cover"] = n
        if "toc" not in out and "toc" in types and not book.is_nav(n):
            out["toc"] = n
        if "index" not in out and "index" in types:
            out["index"] = n
        if "begin" not in out and ({"bodymatter", "chapter", "part"} & types or
                                   re.search(r"_(ch|pt)\d+[a-z]?\.xhtml$", base)):
            out["begin"] = n
    if "toc" not in out:
        nav = next((n for n in book.xhtml if book.is_nav(n)), None)
        if nav:
            out["toc"] = nav
    return out


def _spine_docs(book):
    opf = book.opf_text()
    ids = {a.get("id"): a.get("href") for a in (R.attrs_of(m.group(1)) for m in re.finditer(r"<item\b([^>]*)>", opf))}
    out = []
    for m in re.finditer(r"<itemref\b([^>]*)>", opf):
        href = ids.get(R.attrs_of(m.group(1)).get("idref"))
        if href:
            p = posixpath.normpath(posixpath.join(book.opf_dir, href)) if book.opf_dir else href
            if p in book.files:
                out.append(p)
    return out or list(book.xhtml)


def fix_opf_guide(mp, package):
    """EPUB-035 (OPF): guide references cover "Cover", toc "Table of
    Contents", text "Begin reading", index "Index"."""
    book = _book(mp)
    name, opf = _opf(mp, book)
    if not name:
        return _result([])
    docs = _docs_by_role(book)
    want = [("cover", "Cover", docs.get("cover")), ("toc", "Table of Contents", docs.get("toc")),
            ("text", "Begin reading", docs.get("begin")), ("index", "Index", docs.get("index"))]
    changes, review = [], []
    new = opf
    guide = re.search(r"<guide\b[^>]*>(.*?)</guide>", new, re.S)
    if guide is None and "<guide" not in new:
        indent = (re.search(r"\n([ \t]*)<spine", new) or [None, "  "])[1]
        lines = [f"{indent}<guide>"]
        for typ, title, doc in want:
            if doc:
                lines.append(f'{indent}  <reference type="{typ}" title="{title}" href="{_href(book, doc)}"/>')
        lines.append(f"{indent}</guide>")
        new = new.replace("</package>", "\n".join(lines) + "\n</package>", 1)
        changes.append(_change(name, "guide", "", "guide with " + ", ".join(t for t, _x, d in want if d),
                               "EPUB-035: OPF guide", 0.95))
    elif guide is not None:
        body = guide.group(1)
        new_body = body
        for typ, title, doc in want:
            refs = [m for m in re.finditer(r"<reference\b[^>]*?/?>", new_body)
                    if R.attrs_of(m.group(0)).get("type") == typ]
            if refs:
                r0 = refs[0].group(0)
                if R.attrs_of(r0).get("title") != title:
                    fixed = re.sub(r'title="[^"]*"', f'title="{title}"', r0) if "title=" in r0 else \
                        r0.replace("<reference", f'<reference title="{title}"', 1)
                    new_body = new_body.replace(r0, fixed, 1)
                    changes.append(_change(name, "guide", r0, fixed, "EPUB-035: guide title", 0.95))
            elif doc:
                indent = (re.search(r"\n([ \t]*)<reference", new_body) or [None, "    "])[1]
                ref = f'<reference type="{typ}" title="{title}" href="{_href(book, doc)}"/>'
                new_body = new_body.rstrip() + f"\n{indent}{ref}\n" + (re.search(r"\n([ \t]*)$", body) or
                                                                       [None, "  "])[1]
                changes.append(_change(name, "guide", "", ref, "EPUB-035: guide reference", 0.93))
            else:
                review.append(_review(name, f"guide {typ}", f"no document found for the '{title}' guide entry"))
        new = new.replace(guide.group(0), guide.group(0).replace(body, new_body, 1), 1)
    if new != opf:
        _set_text(mp, name, new)
    return _result(changes, review)


def _href(book, doc):
    return posixpath.relpath(doc, book.opf_dir) if book.opf_dir else doc


def fix_spine_linear(mp, package):
    """EPUB-038: no linear="no" in the spine."""
    book = _book(mp)
    name, opf = _opf(mp, book)
    new, n = re.subn(r'(<itemref\b[^>]*?)\s+linear="no"', r"\1", opf)
    if not n:
        return _result([])
    _set_text(mp, name, new)
    return _result([_change(name, "spine", 'linear="no"', "(removed)", "EPUB-038: every spine item linear", 0.95)])


def fix_author_metadata(mp, package):
    """EPUB-041: dc:creator equals the title page's <p class="bookauthor">
    (the title page is the authority); file-as "Last, First" is added
    when missing."""
    book = _book(mp)
    name, opf = _opf(mp, book)
    if not name:
        return _result([])
    authors = []
    for n in book.xhtml:
        if any(C.attr(e, "epub_type") == "titlepage" for e in book.elements(n)):
            authors += [" ".join("".join(e.itertext()).split()) for e in book.elements(n, "p")
                        if e.get("class") == "bookauthor"]
    authors = [a for a in authors if a]
    changes, review, allowed = [], [], Allowed()
    creators = list(re.finditer(r"<dc:creator\b([^>]*)>([^<]*)</dc:creator>", opf))
    new = opf
    if not authors:
        return _result([])
    if not creators:
        lines = []
        for k, a in enumerate(authors, 1):
            cid = f"creator{k}"
            lines += [f'<dc:creator id="{cid}">{R.escape_text(a)}</dc:creator>',
                      f'<meta refines="#{cid}" property="role" scheme="marc:relators" id="role{k}">aut</meta>',
                      f'<meta refines="#{cid}" property="file-as">{R.escape_text(FM.file_as(a))}</meta>',
                      f'<meta refines="#{cid}" property="display-seq">{k}</meta>']
        new, _ok = _insert_in_metadata(new, lines)
        changes.append(_change(name, "dc:creator", "", "; ".join(authors), "EPUB-041: author from the title page",
                               0.9))
    elif len(creators) == 1 and len(authors) == 1:
        m = creators[0]
        cur = " ".join(R.decode(m.group(2))[0].split())
        if cur != authors[0]:
            if FM.file_as(authors[0]) == cur or _same_name(cur, authors[0]):
                fixed = f"<dc:creator{m.group(1)}>{R.escape_text(authors[0])}</dc:creator>"
                new = new.replace(m.group(0), fixed, 1)
                changes.append(_change(name, "dc:creator", cur, authors[0],
                                       "EPUB-041: author as printed on the title page", 0.9))
                for e in _metadata_entries(opf):
                    if e.startswith("creator="):
                        allowed.metadata_changes[e] = "author as printed on the title page (EPUB-041)"
            else:
                review.append(_review(name, "dc:creator", f"OPF author '{cur}' differs from the title page "
                                                          f"'{authors[0]}' - check which is right"))
        cid = R.attrs_of(m.group(1)).get("id")
        if cid and not re.search(r'refines="#%s"[^>]*property="file-as"' % re.escape(cid), new):
            line = f'<meta refines="#{cid}" property="file-as">{R.escape_text(FM.file_as(authors[0]))}</meta>'
            new, _ok = _insert_in_metadata(new, [line])
            changes.append(_change(name, "metadata", "", line, "author file-as", 0.9))
    else:
        missing = [a for a in authors if a not in [" ".join(R.decode(c.group(2))[0].split()) for c in creators]]
        if missing:
            review.append(_review(name, "dc:creator", f"title page authors {authors} - OPF has "
                                                      f"{[c.group(2) for c in creators]}"))
    if new != opf:
        _set_text(mp, name, new)
    return _result(changes, review, allowed)


def _same_name(a, b):
    na = sorted(C.norm_entry(a).split())
    nb = sorted(C.norm_entry(b).split())
    return na == nb


def fix_document_titles(mp, package):
    """EPUB-025: <title> = the chapter number + title of the <header><h1>."""
    book = _book(mp)
    changes = []
    for n in book.xhtml:
        want = C.expected_title(book, n)
        raw = book.raw(n)
        m = re.search(r"<title>(.*?)</title>", raw, re.S)
        if not want or m is None:
            continue
        cur = " ".join(R.decode(re.sub(r"<[^>]+>", "", m.group(1)))[0].split())
        if C.FRONT_TITLES.match(cur) and any(C.FRONT_TYPES.match(C.attr(e, "epub_type") or "")
                                             for e in book.elements(n)):
            continue
        if not C.title_matches(cur, want):
            text = " ".join(want.replace("\x00", " ").split())
            new = raw[:m.start(1)] + R.escape_text(text) + raw[m.end(1):]
            _set_text(mp, n, new)
            changes.append(_change(n, "<title>", cur, text, "EPUB-025: document title matches the heading", 0.9))
    return _result(changes)


# ================================================================ package
def fix_mimetype(mp, package):
    if not mp.exists("mimetype"):
        return _result([])
    data = mp.get_bytes("mimetype")
    if data == b"application/epub+zip" or data.strip() != b"application/epub+zip":
        return _result([])
    mp.set_bytes("mimetype", b"application/epub+zip")
    return _result([_change("mimetype", "file", repr(data), "application/epub+zip",
                            "EPUB-005: no line break in the mimetype file", 1.0)])


def fix_meta_inf_extras(mp, package):
    changes, review, allowed = [], [], Allowed()
    for n in list(mp.order):
        if n.startswith("META-INF/") and not n.endswith("/") and not re.search(r"container\.xml$", n) and mp.exists(n):
            if C._removable_meta(n):
                mp.remove_entry(n)
                allowed.removed_files.add(n)
                changes.append(_change(n, "file", n, "(not packaged)", "EPUB-006: reading-system file in META-INF",
                                       0.95))
            else:
                review.append(_review(n, "META-INF", "needed by the package (encryption / signatures / rights) - "
                                                     "check with the client"))
    return _result(changes, review, allowed)


# ================================================================ DPI
set_png_dpi, set_jpeg_dpi = IMG.set_png_dpi, IMG.set_jpeg_dpi


def fix_image_dpi(mp, package):
    """EPUB-019: the DPI the client requires (cover 300, inline / icon 135,
    others 150) written into the image header. The pixels are proven
    identical (decoded pixel hash) - an image is never resampled."""
    book = _book(mp)
    changes, review, allowed = [], [], Allowed()
    for n in book.images():
        base = posixpath.basename(n).lower()
        info = C.image_info(book.files[n])
        if info is None:
            continue
        w, h, dx, dy = info
        want = C.expected_dpi(n)
        cover = re.match(r"^cover\..*g$", base) is not None
        if cover and C.cover_ok_019(w, h, dx, dy) or not cover and (dx, dy) == (want, want):
            continue
        data = book.files[n]
        new = set_png_dpi(data, want) if data[:4] == b"\x89PNG" else set_jpeg_dpi(data, want)
        if new is None or C.image_info(new) is None or C.image_info(new)[2:] != (want, want) or \
                pixel_hash(new) != pixel_hash(data):
            review.append(_review(n, "DPI", f"{dx}x{dy} DPI - could not be set without touching the pixels"))
            continue
        mp.set_bytes(n, new)
        allowed.binary_changes[n] = f"DPI {dx} -> {want} in the header (pixels identical)"
        changes.append(_change(n, "image header", f"{dx}x{dy} DPI", f"{want}x{want} DPI",
                               "EPUB-019: DPI metadata (pixels unchanged)", 0.95))
    return _result(changes, review, allowed)


def fix_cover_size(mp, package):
    """EPUB-021 (and the cover's EPUB-019): the cover scaled into the client's
    box - 1200 px wide or 1800 px high, never larger - with its aspect ratio
    kept, saved at 300 DPI. The only repair that changes pixels; it is
    declared to the integrity check with the exact new size."""
    book = _book(mp)
    changes, review, allowed = [], [], Allowed()
    for n in book.images():
        if not IMG.is_cover(n):
            continue
        meta = IMG.info(book.files[n])
        if meta is None or IMG.cover_size_ok(meta[1], meta[2]):
            continue
        try:
            new, before, after = IMG.resize_cover(book.files[n])
        except Exception as e:  # noqa: BLE001
            review.append(_review(n, "cover", f"could not be resized: {e}"))
            continue
        mp.set_bytes(n, new)
        allowed.resized_images[n] = list(after)
        changes.append(_change(n, "image", f"{before[0]}x{before[1]} px", f"{after[0]}x{after[1]} px, 300 DPI",
                               "EPUB-021: cover scaled into 1200 (W) x 1800 (H), aspect ratio kept", 0.9))
    return _result(changes, review, allowed)


# ================================================================ nav
def fix_nav_landmarks(mp, package):
    """EPUB-035 / 037 (navigation file): landmarks nav with
    <h2 id="landmarks">Book Landmarks</h2>, <ol class="none"> and the entries
    Cover Page (cover), Contents (toc), Begin Reading (part), Index (index)."""
    book = _book(mp)
    nav = next((n for n in book.xhtml if book.is_nav(n)), None)
    if nav is None:
        return _result([])
    raw = book.raw(nav)
    changes, review, allowed = [], [], Allowed()
    docs = _docs_by_role(book)
    targets = {"cover": docs.get("cover"), "toc": docs.get("toc"), "part": docs.get("begin"),
               "index": docs.get("index")}
    m = re.search(r"<nav\b(?=[^>]*\bepub:type=\"landmarks\")[^>]*>(.*?)</nav>", raw, re.S)
    new = raw
    if m is None:
        entries = "".join(f'\n      <li><a epub:type="{t}" href="{book.rel(nav, targets[t])}">{label}</a></li>'
                          for t, label in C.NAV_LANDMARKS if targets.get(t))
        block = (f'\n    <nav epub:type="landmarks" id="guide" hidden="hidden">\n      <h2 id="landmarks">Book '
                 f'Landmarks</h2>\n      <ol class="none">{entries}\n      </ol>\n    </nav>')
        toc_end = re.search(r"<nav\b(?=[^>]*\bepub:type=\"toc\")[^>]*>.*?</nav>", raw, re.S)
        at = toc_end.end() if toc_end else raw.rfind("</body>")
        new = raw[:at] + block + raw[at:]
        changes.append(_change(nav, "landmarks", "", "landmarks nav", "EPUB-037: landmarks navigation", 0.93))
        allowed.added_text.setdefault(nav, []).extend(
            ["Book Landmarks"] + [label for t, label in C.NAV_LANDMARKS if targets.get(t)])
    else:
        body = m.group(1)
        nb = body
        h = re.search(r"<(h[1-6])\b([^>]*)>(.*?)</\1>", nb, re.S)
        if h is None:
            nb = '\n      <h2 id="landmarks">Book Landmarks</h2>' + nb
            allowed.added_text.setdefault(nav, []).append("Book Landmarks")
            changes.append(_change(nav, "landmarks", "", "h2 Book Landmarks", "EPUB-037: landmarks heading", 0.95))
        else:
            old_text = " ".join(R.decode(re.sub(r"<[^>]+>", "", h.group(3)))[0].split())
            a = R.attrs_of(h.group(2))
            if h.group(1) != "h2" or a.get("id") != "landmarks" or old_text != "Book Landmarks":
                keep = " ".join(f'{k}="{R.escape_attr(v)}"' for k, v in a.items() if k != "id")
                fixed = f'<h2 id="landmarks"{" " + keep if keep else ""}>Book Landmarks</h2>'
                if a.get("id") and a.get("id") != "landmarks" and re.search(
                        r"#%s\"" % re.escape(a["id"]), raw):
                    review.append(_review(nav, "landmarks heading", f"id '{a['id']}' is referenced - not renamed"))
                else:
                    nb = nb.replace(h.group(0), fixed, 1)
                    if old_text and old_text != "Book Landmarks":
                        allowed.text_edits.setdefault(nav, []).append((old_text, "Book Landmarks"))
                    changes.append(_change(nav, "landmarks", h.group(0)[:80], fixed, "EPUB-037: landmarks heading",
                                           0.95))
        ol = re.search(r"<ol\b([^>]*)>", nb)
        if ol and R.attrs_of(ol.group(1)).get("class") != "none":
            a = R.attrs_of(ol.group(1))
            fixed = re.sub(r'class="[^"]*"', 'class="none"', ol.group(0)) if "class" in a else \
                ol.group(0).replace("<ol", '<ol class="none"', 1)
            nb = nb.replace(ol.group(0), fixed, 1)
            changes.append(_change(nav, "landmarks", ol.group(0), fixed, "EPUB-037: landmarks list class", 0.97))
        for typ, label in C.NAV_LANDMARKS:
            links = [x for x in re.finditer(r"<a\b([^>]*)>(.*?)</a>", nb, re.S)]
            mine = [x for x in links if R.attrs_of(x.group(1)).get("epub:type") == typ]
            if any(" ".join(R.decode(re.sub(r"<[^>]+>", "", x.group(2)))[0].split()) == label for x in mine):
                continue
            if mine:
                x = mine[0]
                old = " ".join(R.decode(re.sub(r"<[^>]+>", "", x.group(2)))[0].split())
                fixed = f"<a{x.group(1)}>{label}</a>"
                nb = nb.replace(x.group(0), fixed, 1)
                allowed.nav_relabels[old] = label
                allowed.text_edits.setdefault(nav, []).append((old, label))
                changes.append(_change(nav, "landmarks", x.group(0), fixed, f"EPUB-035: '{label}' landmark", 0.93))
                continue
            if typ == "part":
                # an existing Begin Reading / bodymatter entry takes the type the client tool reads
                bm = [x for x in links if R.attrs_of(x.group(1)).get("epub:type") == "bodymatter"]
                if bm:
                    x = bm[0]
                    old = " ".join(R.decode(re.sub(r"<[^>]+>", "", x.group(2)))[0].split())
                    attrs = x.group(1).replace('epub:type="bodymatter"', 'epub:type="part"')
                    fixed = f"<a{attrs}>{label}</a>"
                    nb = nb.replace(x.group(0), fixed, 1)
                    if old != label:
                        allowed.nav_relabels[old] = label
                        allowed.text_edits.setdefault(nav, []).append((old, label))
                    changes.append(_change(nav, "landmarks", x.group(0), fixed,
                                           "EPUB-035: Begin Reading as epub:type=\"part\" (client tool)", 0.9))
                    continue
            doc = targets.get(typ)
            if not doc:
                review.append(_review(nav, f"landmark {label}", "no document found for this landmark"))
                continue
            li_ind = (re.search(r"\n([ \t]*)<li", nb) or [None, "        "])[1]
            entry = f'<li><a epub:type="{typ}" href="{book.rel(nav, doc)}">{label}</a></li>'
            close = nb.rfind("</ol>")
            if close < 0:
                review.append(_review(nav, "landmarks", "no <ol> list in the landmarks nav"))
                continue
            close_ind = (re.search(r"\n([ \t]*)$", nb[:close]) or [None, "      "])[1]
            nb = nb[:close].rstrip() + f"\n{li_ind}{entry}\n{close_ind}" + nb[close:]
            allowed.added_text.setdefault(nav, []).append(label)
            changes.append(_change(nav, "landmarks", "", entry, f"EPUB-035: '{label}' landmark", 0.93))
        new = raw[:m.start(1)] + nb + raw[m.end(1):]
    if new != raw:
        _set_text(mp, nav, new)
    return _result(changes, review, allowed)


_ROLE_ELEMENTS = {"page-list": ("nav", "section", "div"), "cover": ("img",), "appendix": ("section", "article", "div"),
                  "bibliography": ("section", "div"), "dedication": ("section", "div"),
                  "epigraph": ("section", "div", "blockquote", "p"), "foreword": ("section", "div"),
                  "glossary": ("section", "div", "dl"), "introduction": ("section", "div"),
                  "pagebreak": ("span", "hr", "div"), "preface": ("section", "div"), "toc": ("nav", "section", "div"),
                  "chapter": ("section", "article", "div")}


def fix_epub_type_roles(mp, package):
    """EPUB-024: the role the client maps to each epub:type (chapter ->
    doc-chapter, pagebreak -> doc-pagebreak, ...) added where it is missing,
    only on elements that role is allowed on."""
    book = _book(mp)
    changes = []
    for n in book.xhtml:
        raw = book.raw(n)
        edits = []
        nav_lm = [(m.start(), m.end()) for m in re.finditer(
            r"<nav\b(?=[^>]*\bepub:type=\"landmarks\")[^>]*>.*?</nav>", raw, re.S)]
        for m in re.finditer(r"<([a-zA-Z][\w]*)\b([^>]*?)(/?)>", raw):
            if any(a <= m.start() < b for a, b in nav_lm):
                continue
            tag, attrs = m.group(1).lower(), R.attrs_of(m.group(2))
            et, role = attrs.get("epub:type"), attrs.get("role")
            if et in D_ROLES and et not in ("endnote", "biblioentry") and role is None and \
                    tag in _ROLE_ELEMENTS.get(et, ()):
                at = m.end(2)
                edits.append((at, at, f' role="{D_ROLES[et]}"'))
        if edits:
            new = R.apply_edits(raw, edits)
            _set_text(mp, n, new)
            changes.append(_change(n, "elements", f"{len(edits)} element(s) without role", "role added",
                                   "EPUB-024: role matching epub:type", 0.93))
    return _result(changes)


from core.epub.client_rules.cupepub_data import EPUB_TYPE_ROLES as D_ROLES  # noqa: E402


# ================================================================ links
def _wrap_link(raw_slice, href):
    return f'<a href="{R.escape_attr(href)}">{raw_slice}</a>'


def _wrap_span(raw_slice):
    return f"{SPAN_OPEN}{raw_slice}{SPAN_CLOSE}"


_URL_TOKEN_RE = re.compile(r"[^\s<>\"]+")


def _url_target(token):
    t = token.strip("([{\"'").rstrip(".,;:)]}\"'")
    if re.match(r"^https?://[\w.-]+\.[a-z]{2,}\S*$", t, re.I):
        return t, t
    if re.match(r"^www\.[\w-]+(\.[\w-]+)+(/\S*)?$", t, re.I):
        return t, "http://" + t
    if re.match(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$", t):
        return t, "mailto:" + t
    if re.match(r"^[\w-]+(\.[\w-]+)*\.(com|org|net|edu|gov|ca|co\.uk|ac\.uk|uk|info)(/\S*)?$", t, re.I):
        return t, "http://" + t
    return None, None


def resolve_reference(book, name, ref):
    """(href, reason) for an EPUB-009 match, href None when no single target."""
    t = book.targets
    if ref.kind == "chapter":
        f = t.chapters.get(int(ref.key))
        if f == name:
            return None, "the chapter refers to itself"
        return (book.rel(name, f), "chapter file") if f else (None, "no such chapter")
    if ref.kind == "part":
        f = t.parts.get(int(ref.key or 0))
        if f == name:
            return None, "the part refers to itself"
        return (book.rel(name, f), "part file") if f else (None, "no such part")
    if ref.kind in ("figure", "table", "section", "equation", "box", "appendix"):
        tgt = t.unique(ref.kind, ref.key)
        if tgt is None and ref.kind == "appendix" and len(ref.key) == 1 and ref.key.isalpha():
            f = t.appendices.get(ord(ref.key) - 64)
            if f:
                return book.rel(name, f), "appendix file"
        if tgt is None:
            many = len(set(t.labels.get((ref.kind, ref.key), [])))
            return None, (f"{many} {ref.kind}s numbered {ref.key}" if many else f"no {ref.kind} {ref.key}")
        return book.rel(name, tgt[0], tgt[1]), f"{ref.kind} {ref.key}"
    if ref.kind == "citation":
        if not re.fullmatch(r"\d+", ref.key):
            return None, "a list of citations - link each manually"
        v = list(dict.fromkeys(t.refs.get(ref.key.lstrip("0") or "0", []) + t.refs.get(ref.key, [])))
        if len(v) == 1:
            return book.rel(name, v[0][0], v[0][1]), f"reference {ref.key}"
        return None, ("no reference numbered " + ref.key) if not v else f"{len(v)} references numbered {ref.key}"
    return None, "not a resolvable reference"


def fix_cross_reference_links(mp, package):
    """EPUB-009: link each reference to its target, or wrap it in a span."""
    book = _book(mp)
    changes, review = [], []
    for n in book.xhtml:
        refs = C.link_matches(book, n)
        if not refs:
            continue
        raw = book.raw(n)
        edits = R.EditList()
        linked = spanned = 0
        done_urls = set()
        for ref in refs:
            seg = ref.seg
            i, j = ref.i, ref.j
            if ref.kind == "url":
                tok = next((m for m in _URL_TOKEN_RE.finditer(seg.text) if m.start() <= i < m.end()), None)
                if tok is None or (seg.start, tok.start()) in done_urls:
                    continue
                done_urls.add((seg.start, tok.start()))
                shown, href = _url_target(tok.group(0))
                if shown:
                    i = tok.start() + tok.group(0).find(shown)
                    j = i + len(shown)
                    reason = "URL / e-mail"
                else:
                    i, j = tok.start(), tok.end()
                    reason = "not a complete URL / e-mail"
            else:
                if ref.kind in ("figure", "table", "section", "equation", "box"):
                    ext = re.match(r"(?:[.\-]\d+)+[a-z]?\b", seg.text[j:])
                    if ext:
                        j += len(ext.group(0))
                        ref.key = (ref.key + ext.group(0)).replace("-", ".")
                href, reason = resolve_reference(book, n, ref)
            a, b = seg.raw_span(i, j)
            piece = raw[a:b]
            if href:
                if edits.add(a, b, _wrap_link(piece, href)):
                    linked += 1
                    changes.append(_change(n, f"line {R.line_col(raw, a)[0]}", seg.text[i:j],
                                           f'<a href="{href}">', f"EPUB-009: linked ({reason})", 0.9))
            else:
                if edits.add(a, b, _wrap_span(piece)):
                    spanned += 1
                    changes.append(_change(n, f"line {R.line_col(raw, a)[0]}", seg.text[i:j], "<span>",
                                           f"EPUB-009: no valid target ({reason}) - marked with a span", 0.9))
                    review.append(_review(n, seg.text[i:j], f"no link target: {reason} (wrapped in <span>)"))
        if edits.items:
            _set_text(mp, n, R.apply_edits(raw, edits.items))
    return _result(changes, review)


# ---------------------------------------------------------------- index
_LOC_RE = re.compile(
    r"(?<![\w&#;])(?:"
    r"(?P<nb>\d+)(?P<ns>nn?\.?\s?\d+(?:[–\-]\d+)?)"                 # 45n3
    r"|(?P<r1>\d+)(?P<rd>\s?(?:–|-|&#x2013;|&#8211;)\s?)(?P<r2>\d+)"  # 12-15
    r"|(?P<n>\d+)"                                                    # 12
    r"|(?P<x1>[ivxlc]+)(?P<xd>\s?[–\-]\s?)(?P<x2>[ivxlc]+)"           # xi-xii
    r"|(?P<x>[ivxlc]+)"                                               # xii
    r"|(?P<X>[IVXL]+)"                                                # flagged upper-case roman
    r")(?![\w])")


def _locator_position(seg, i):
    before = seg.text[:i].rstrip()
    if not before:
        prev = seg.prev
        return isinstance(prev, R.Node) and prev.name in ("a", "span")
    return before[-1] in ",;:" or before.endswith((" and", " &"))


def fix_index_links(mp, package):
    """EPUB-043 ... 047 / 049 in index documents: page locators linked to
    their page markers (ranges as two links, abbreviated ends expanded),
    roman pages linked, "see / see also" linked to the named entry; any
    locator without a page marker / entry wrapped in a span."""
    book = _book(mp)
    t = book.targets
    changes, review = [], []
    for n in book.xhtml:
        if not book.is_index_doc(n):
            continue
        raw = book.raw(n)
        edits = R.EditList()
        stats = {"linked": 0, "spanned": 0, "ranges": 0, "see": 0, "anchors": 0}

        def page_href(label):
            p = t.page(label)
            return book.rel(n, p[0], p[1]) if p else None

        # --- existing anchors: 045 (one link over a range), 044 (wrong end), 049 (merged text)
        for m in re.finditer(r"<a\b([^>]*)>([^<]*)</a>", raw):
            a_attrs = R.attrs_of(m.group(1))
            text, offs = R.decode(m.group(2), m.start(2))
            rm = re.fullmatch(r"\s*(\d+)(\s?(?:–|-)\s?)(\d+)\s*", text)
            if rm and C.PAGE_HREF_RE.search(a_attrs.get("href", "")):
                end_full = C.expand_range_end(rm.group(1), rm.group(3))
                h1, h2 = page_href(rm.group(1)) or a_attrs["href"], page_href(end_full)
                s1, e1 = offs[rm.start(1)], offs[rm.end(1)]
                s3, e3 = offs[rm.start(3)], offs[rm.end(3)]
                first = _wrap_link(raw[s1:e1], h1)
                second = _wrap_link(raw[s3:e3], h2) if h2 else _wrap_span(raw[s3:e3])
                rep = first + raw[e1:s3] + second
                lead, trail = raw[m.start(2):s1], raw[e3:m.end(2)]
                if edits.add(m.start(), m.end(), lead + rep + trail):
                    stats["ranges"] += 1
                    changes.append(_change(n, f"line {R.line_col(raw, m.start())[0]}", text, "two links",
                                           "EPUB-045: page range linked separately", 0.93))
        for seg in book.segs(n):
            if not (isinstance(seg.prev, R.Node) and isinstance(seg.next, R.Node) and seg.prev.name == "a" and
                    seg.next.name == "a" and C.INDEX_DASH_ONLY_RE.match(seg.text.strip())):
                continue
            t1 = seg.prev.text.strip()
            am = re.compile(r"<a\b([^>]*)>([^<]*)</a>").match(raw, seg.next.start)
            if not am or not re.fullmatch(r"\d+", t1):
                continue
            t2 = R.decode(am.group(2))[0].strip()
            if not re.fullmatch(r"\d+", t2):
                continue
            full = C.expand_range_end(t1, t2)
            cur = C.PAGE_HREF_RE.search(R.attrs_of(am.group(1)).get("href", ""))
            want = page_href(full)
            if want and (cur is None or cur.group(1) != full):
                href_m = re.search(r'href="[^"]*"', am.group(0))
                if href_m:
                    a0 = am.start() + href_m.start()
                    if edits.add(a0, am.start() + href_m.end(), f'href="{R.escape_attr(want)}"'):
                        changes.append(_change(n, f"line {R.line_col(raw, a0)[0]}", href_m.group(0),
                                               f'href="{want}"', "EPUB-044: range end points to its full page", 0.93))
        for m in re.finditer(r"(<a\b[^>]*>[^<]*)(</a>)([A-Za-z0-9]{1,6})(?![A-Za-z0-9])", raw):
            if C.PAGE_HREF_RE.search(m.group(1)):
                if edits.add(m.start(2), m.end(3), m.group(3) + "</a>"):
                    changes.append(_change(n, f"line {R.line_col(raw, m.start())[0]}", "</a>" + m.group(3),
                                           m.group(3) + "</a>", "EPUB-049: locator text inside its link", 0.93))
        # --- text: 043 / 046 / 047
        for seg in book.segs(n):
            if seg.inside("a", "span", "title", "h1", "h2", "h3", "h4", "h5", "h6"):
                continue
            prev = seg.prev
            if seg.text.strip() and isinstance(prev, R.Node) and prev.name == "i" and \
                    C.SEE_RE.match(prev.text.strip().lower()):
                _see_links(book, n, raw, seg, edits, changes, review, stats)
                continue
            for m in _LOC_RE.finditer(seg.text):
                i = m.start()
                if m.group("X"):
                    if C.ROMAN_RE.fullmatch(m.group("X")) and not re.search(r"[A-Z][a-z]+ $", seg.text[:i]) and \
                            any(nd.name == "li" and nd.get("class").lower() == "index" for nd in seg.stack):
                        a0, b0 = seg.raw_span(m.start(), m.end())
                        if edits.add(a0, b0, _wrap_span(raw[a0:b0])):
                            stats["spanned"] += 1
                            review.append(_review(n, m.group(0), "upper-case roman numeral (EPUB-046) - no page; "
                                                                 "wrapped in <span>"))
                    continue
                if m.group("x") or m.group("x1"):
                    labels = [m.group("x")] if m.group("x") else [m.group("x1"), m.group("x2")]
                    if not all(t.page(lb) for lb in labels) or not _locator_position(seg, i):
                        continue                                    # a word, not a page
                if not _locator_position(seg, i) and not m.group("x1"):
                    if m.group("n") or m.group("nb") or m.group("r1"):
                        if not re.match(r" |\d|;", seg.text[i - 1:i] if i else "") and not m.group("r1"):
                            continue                                # the client tool does not report it
                        a0, b0 = seg.raw_span(m.start(), m.end())
                        if edits.add(a0, b0, _wrap_span(raw[a0:b0])):
                            stats["spanned"] += 1
                            review.append(_review(n, m.group(0), "number in the entry text, not a page locator - "
                                                                 "wrapped in <span>"))
                    continue
                _link_locator(book, n, raw, seg, m, edits, changes, review, stats, page_href)
        if edits.items:
            _set_text(mp, n, R.apply_edits(raw, edits.items))
            changes.append(_change(n, "index", "", f"{stats['linked']} locator link(s), {stats['ranges']} range(s) "
                                                   f"split, {stats['see']} see-reference(s), {stats['spanned']} "
                                                   "span(s)", "EPUB-043...047: index links", 0.9))
    return _result(changes, review)


def _link_locator(book, n, raw, seg, m, edits, changes, review, stats, page_href):
    def one(i, j, label):
        a0, b0 = seg.raw_span(i, j)
        href = page_href(label)
        if href:
            return _wrap_link(raw[a0:b0], href), True
        return _wrap_span(raw[a0:b0]), False
    if m.group("nb"):                                   # 45n3 -> whole token to page 45
        rep, ok = one(m.start(), m.end(), m.group("nb"))
        a0, b0 = seg.raw_span(m.start(), m.end())
        if edits.add(a0, b0, rep):
            stats["linked" if ok else "spanned"] += 1
            if not ok:
                review.append(_review(n, m.group(0), f"no page marker for page {m.group('nb')} - wrapped in <span>"))
        return
    if m.group("r1") or m.group("x1"):
        g1, g2 = ("r1", "r2") if m.group("r1") else ("x1", "x2")
        start, end = m.group(g1), m.group(g2)
        full = C.expand_range_end(start, end) if m.group("r1") else end
        rep1, ok1 = one(m.start(g1), m.end(g1), start)
        rep2, ok2 = one(m.start(g2), m.end(g2), full)
        a0, _x = seg.raw_span(m.start(g1), m.end(g1))
        _y, b0 = seg.raw_span(m.start(g2), m.end(g2))
        mid = raw[seg.offsets[m.end(g1)]:seg.offsets[m.start(g2)]]
        if edits.add(a0, b0, rep1 + mid + rep2):
            stats["linked"] += ok1 + ok2
            stats["spanned"] += (not ok1) + (not ok2)
            if not (ok1 and ok2):
                review.append(_review(n, m.group(0), "range end without a page marker - wrapped in <span>"))
        return
    label = m.group("n") or m.group("x")
    rep, ok = one(m.start(), m.end(), label)
    a0, b0 = seg.raw_span(m.start(), m.end())
    if edits.add(a0, b0, rep):
        stats["linked" if ok else "spanned"] += 1
        if not ok:
            review.append(_review(n, label, f"no page marker for page {label} - wrapped in <span>"))


def _see_links(book, n, raw, seg, edits, changes, review, stats):
    t = book.targets
    text = seg.text
    pos = 0
    for part in re.split(r"(;)", text):
        if part == ";" or not part.strip():
            pos += len(part)
            continue
        lead = len(part) - len(part.lstrip())
        name_txt = part.strip()
        core = re.sub(r"[.)\]]+$", "", name_txt).strip()
        if not core:
            pos += len(part)
            continue
        i = pos + lead
        j = i + len(core)
        hits = t.index_entries.get(C.norm_entry(core)) or t.index_entries.get(
            C.norm_entry(re.split(r"[:,]", core)[0])) or []
        hits = [h for h in hits if h[0] == n] or hits
        a0, b0 = seg.raw_span(i, j)
        if len(hits) == 1:
            f, eid, tag_start, tag_end = hits[0]
            if not eid:
                eid = _new_id(book, core)
                if f == n:
                    if not edits.add(tag_end - 1, tag_end - 1, f' id="{eid}"'):
                        eid = None
                else:
                    eid = None
            if eid:
                href = book.rel(n, f, eid)
                if edits.add(a0, b0, _wrap_link(raw[a0:b0], href)):
                    stats["see"] += 1
                    book.targets.index_entries[C.norm_entry(core)] = [(f, eid, tag_start, tag_end)]
                pos += len(part)
                continue
        if edits.add(a0, b0, _wrap_span(raw[a0:b0])):
            stats["spanned"] += 1
            review.append(_review(n, core, ("no index entry named '%s'" % core) if not hits else
                                  f"{len(hits)} index entries named '{core}'") )
        pos += len(part)


def _new_id(book, text):
    base = "idx-" + (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "entry")
    used = {e.get("id") for nm in book.xhtml for e in book.elements(nm) if e.get("id")}
    used |= getattr(book, "_new_ids", set())
    cand, k = base, 2
    while cand in used:
        cand = f"{base}-{k}"
        k += 1
    book._new_ids = used | {cand}
    return cand


def fix_link_labels(mp, package):
    """EPUB-042: "Table <a>3</a>" -> "<a>Table 3</a>" (the label word moves
    inside the link; the text is unchanged)."""
    book = _book(mp)
    changes = []
    for n in book.xhtml:
        raw = book.raw(n)
        edits = []
        for m in re.finditer(r"\b(Tab(?:les?|\.|s\.)|Fig(?:ures?|\.|s\.)|Sec(?:tions?|\.|s\.)|Equations?|Eqns?\.|"
                             r"Eqs?\.)((?:\s|&#x0*A0;|&#160;|&nbsp;)*)(<a\b[^>]*>)", raw, re.I):
            edits.append((m.start(), m.end(), m.group(3) + m.group(1) + m.group(2)))
        if edits:
            _set_text(mp, n, R.apply_edits(raw, edits))
            changes.append(_change(n, "links", f"{len(edits)} label(s) before a link", "label inside the link",
                                   "EPUB-042: link includes its label", 0.9))
    return _result(changes)


# ================================================================ registry
# (label, rule codes, function) in the order they are applied
FIXES = [
    ("CUPEPUB mimetype", ("EPUB-005",), fix_mimetype),
    ("CUPEPUB META-INF files", ("EPUB-006",), fix_meta_inf_extras),
    ("CUPEPUB OPF identifiers / print ISBN", ("EPUB-001", "EPUB-014"), fix_opf_identifiers),
    ("CUPEPUB cover image property", ("EPUB-023",), fix_cover_properties),
    ("CUPEPUB author metadata", ("EPUB-041",), fix_author_metadata),
    ("CUPEPUB OPF guide", ("EPUB-035",), fix_opf_guide),
    ("CUPEPUB spine linear", ("EPUB-038",), fix_spine_linear),
    ("CUPEPUB landmarks", ("EPUB-035", "EPUB-037"), fix_nav_landmarks),
    ("CUPEPUB epub:type / role", ("EPUB-024",), fix_epub_type_roles),
    ("CUPEPUB document titles", ("EPUB-025",), fix_document_titles),
    ("CUPEPUB double-escaped references", ("EPUB-011",), fix_double_escaped_references),
    ("CUPEPUB index links", ("EPUB-043", "EPUB-044", "EPUB-045", "EPUB-046", "EPUB-047", "EPUB-049"),
     fix_index_links),
    ("CUPEPUB cross-reference links", ("EPUB-009",), fix_cross_reference_links),
    ("CUPEPUB link labels", ("EPUB-042",), fix_link_labels),
    ("CUPEPUB cover size", ("EPUB-021", "EPUB-019"), fix_cover_size),
    ("CUPEPUB image DPI", ("EPUB-019",), fix_image_dpi),
    ("CUPEPUB junk characters", ("EPUB-010",), fix_junk_characters),
]
