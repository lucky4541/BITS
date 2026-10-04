"""Additional deterministic repair strategies for the closed-loop auto-fix
engine (core.epub.repair_engine). Same contract as core.epub.repair_strategies:
each function is a SWEEP over the current package that fixes every instance
of its own condition it can resolve with high confidence, and leaves every
other instance untouched - listed in `review` with the reason.

MINIMAL CHANGES: every edit here is TEXTUAL - the exact attribute value,
entity, element or brace is replaced in the original source; documents are
never re-serialized, so whitespace, attribute order, entities elsewhere,
comments and the DOCTYPE stay byte-identical.

NEVER INVENTS: a reference is only repointed at a file / id that already
exists, a media type only corrected when the file's own bytes AND its
extension agree, a duplicate id only renamed (never removed) with every
reference re-checked, junk only dropped when nothing declares or references
it. Nothing here deletes text, images, links, IDs or navigation entries.

Every result carries `changes` (file, location, before, after, reason,
confidence) for the repair history and `allowed` (renamed ids, removed junk)
for the integrity check.
"""
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from core.epub.integrity_snapshot import Allowed, named_entities_to_numeric, parse_lenient
from core.epub.repair_strategies import RepairActionResult, _MEDIA_TYPE_BY_EXT

XHTML_NS = "http://www.w3.org/1999/xhtml"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_EXTERNAL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_ABS_FS_RE = re.compile(r"^(file:/+|[A-Za-z]:[\\/]|\\\\)")
JUNK_RE = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini|\.epubforge_qc\.json|__MACOSX/.*|\._[^/]*|"
                     r"[^/]*\.(bak|tmp|orig|swp|log)|[^/]*~|\.git/.*|\.svn/.*)$", re.IGNORECASE)
_REF_ATTRS = {"img": ("src",), "image": (XLINK_HREF, "href"), "source": ("src",), "object": ("data",),
              "audio": ("src",), "video": ("src", "poster"), "link": ("href",), "a": ("href",),
              "content": ("src",), "script": ("src",), "embed": ("src",), "track": ("src",)}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr_name(attr):
    return "xlink:href" if attr == XLINK_HREF else attr


def _xml_escape_attr(v):
    return v.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _result(changes, review=None, allowed=None):
    files = sorted({c["file"] for c in changes})
    desc = "; ".join(f"{c['file']}: {c['reason']} ('{c['before']}' -> '{c['after']}')" for c in changes)
    r = RepairActionResult(applied=bool(changes), description=desc, files=files)
    r.changes = changes
    r.review = review or []
    r.allowed = allowed or Allowed()
    return r


def _change(file, location, before, after, reason, confidence):
    return {"file": file, "location": location, "before": before, "after": after, "reason": reason,
            "confidence": confidence}


def replace_attr_value(text, attr, old, new, nth=None):
    """Replaces attr="old" by attr="new" in raw markup. nth (0-based) limits
    the edit to that occurrence. Returns (text, count)."""
    names = [_attr_name(attr)]
    alts = "|".join(re.escape(n) for n in names)
    variants = {old, _xml_escape_attr(old), old.replace("&", "&amp;")}
    count = [0]
    seen = [0]
    out = text
    for v in sorted(variants, key=len, reverse=True):
        pat = re.compile(r"(?<![\w:.-])((?:%s)\s*=\s*)([\"'])%s\2" % (alts, re.escape(v)))

        def repl(m):
            k = seen[0]
            seen[0] += 1
            if nth is not None and k != nth:
                return m.group(0)
            count[0] += 1
            return f"{m.group(1)}{m.group(2)}{_xml_escape_attr(new)}{m.group(2)}"
        out = pat.sub(repl, out)
        if count[0]:
            break
    return out, count[0]


# ================================================================= index
@dataclass
class PackageIndex:
    names: set = field(default_factory=set)
    lower: dict = field(default_factory=dict)            # lowercased path -> [paths]
    docs: dict = field(default_factory=dict)             # xhtml path -> (root, well_formed)
    ids: dict = field(default_factory=dict)              # xhtml path -> set(ids)
    id_docs: dict = field(default_factory=dict)          # id -> [docs]
    opf: str = ""
    manifest: dict = field(default_factory=dict)         # resolved path -> (id, media type)

    @classmethod
    def build(cls, mp, package):
        idx = cls()
        idx.names = {n for n in mp.order if mp.exists(n) and not n.endswith("/")}
        for n in idx.names:
            idx.lower.setdefault(n.lower(), []).append(n)
        idx.opf = package.opf_path
        for item in package.manifest:
            if item.href:
                idx.manifest[package.resolve_href(item.href)] = (item.id, item.media_type)
        xhtml = {p for p, (_i, mt) in idx.manifest.items() if mt == "application/xhtml+xml"}
        xhtml |= {n for n in idx.names if n.lower().endswith((".xhtml", ".html", ".htm"))}
        for n in sorted(xhtml):
            if n not in idx.names:
                continue
            root, ok = parse_lenient(mp.get_bytes(n))
            idx.docs[n] = (root, ok)
            ids = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")} \
                if root is not None else set()
            idx.ids[n] = ids
            for i in ids:
                idx.id_docs.setdefault(i, []).append(n)
        return idx

    def text_files(self):
        """Every markup file whose references we may repair."""
        out = list(self.docs)
        out += [n for n in self.names if n.lower().endswith(".ncx")]
        return out


def _rel(from_doc, target, frag=""):
    rel = posixpath.relpath(target, posixpath.dirname(from_doc) or ".")
    if target == from_doc and frag:
        rel = ""
    rel = quote(rel, safe="/-._~!$&'()*+,;=:@")
    return rel + (f"#{quote(frag, safe='-._~!$&()*+,;=:@')}" if frag else "")


def resolve_resource(idx, from_doc, value, is_link=False):
    """(new_value, confidence, reason) for a reference whose file part does
    not resolve - or (None, 0, why) when no single, certain target exists."""
    if value is None or _EXTERNAL_RE.match(value) and not _ABS_FS_RE.match(value):
        return None, 0, "external"
    path, sep, frag = value.partition("#")
    if not path:
        return None, 0, "same-document"
    base = posixpath.dirname(from_doc)
    norm = path.replace("\\", "/")
    abs_fs = bool(_ABS_FS_RE.match(path))
    if not abs_fs:
        cand = posixpath.normpath(posixpath.join(base, unquote(norm)))
        if cand in idx.names:
            new = _rel(from_doc, cand, unquote(frag) if sep else "")
            if new != value:
                return new, 0.99, "invalid URI syntax (backslash / unescaped characters) normalised"
            return None, 0, "resolves"
        ci = idx.lower.get(cand.lower(), [])
        if len(ci) == 1:
            return _rel(from_doc, ci[0], unquote(frag) if sep else ""), 0.98, "letter case of the path corrected"
    if is_link and sep and frag:
        docs = idx.id_docs.get(unquote(frag), [])
        if len(docs) == 1:
            return _rel(from_doc, docs[0], unquote(frag)), 0.97, \
                f"file '{path}' does not exist; '#{frag}' lives only in {docs[0]} (stale split / moved file)"
    bn = posixpath.basename(norm).lower()
    matches = [n for n in idx.names if posixpath.basename(n).lower() == bn]
    if len(matches) == 1:
        reason = "absolute filesystem path made package-relative" if abs_fs else \
            "path repointed to the only file with that name"
        return _rel(from_doc, matches[0], unquote(frag) if sep else ""), 0.95, reason
    if len(matches) > 1:
        return None, 0, f"{len(matches)} files named '{posixpath.basename(norm)}' - ambiguous"
    return None, 0, "no file with that name exists in the package (never invented)"


def resolve_fragment(idx, from_doc, value):
    """For a link whose FILE exists but whose #fragment is not an id there."""
    path, _sep, frag = value.partition("#")
    target = from_doc if not path else posixpath.normpath(
        posixpath.join(posixpath.dirname(from_doc), unquote(path.replace("\\", "/"))))
    if target not in idx.docs:
        return None, 0, "target is not a content document"
    root, ok = idx.docs[target]
    if not ok:
        return None, 0, f"{target} is not well-formed yet (root cause: XML error - repaired first)"
    ids = idx.ids.get(target, set())
    f = unquote(frag)
    if f in ids:
        new = (path + "#" if path else "#") + quote(f, safe="-._~!$&()*+,;=:@")
        return (new, 0.99, "fragment URL-encoding corrected") if new != value else (None, 0, "resolves")
    ci = [i for i in ids if i.lower() == f.lower()]
    if len(ci) == 1:
        return (path + "#" if path else "#") + ci[0], 0.98, f"id letter case corrected ('{f}' -> '{ci[0]}')"
    elsewhere = [d for d in idx.id_docs.get(f, []) if d != target]
    if len(elsewhere) == 1:
        return _rel(from_doc, elsewhere[0], f), 0.97, f"'#{f}' moved to {elsewhere[0]} (split / merge)"
    key = re.sub(r"[-_.:\s]", "", f.lower())
    near = [i for i in ids if re.sub(r"[-_.:\s]", "", i.lower()) == key]
    if len(near) == 1:
        return (path + "#" if path else "#") + near[0], 0.95, f"id renamed ('{f}' -> '{near[0]}')"
    return None, 0, "no single matching id exists (never invented)"


def _iter_refs(root):
    for el in root.iter():
        ln = _local(el.tag)
        for attr in _REF_ATTRS.get(ln, ()):
            v = el.get(attr)
            if v is not None:
                yield el, ln, attr, v


# ============================================================ strategies
def fix_named_entities(mp, package):
    """HTML named entities (&nbsp; &mdash; ...) are undefined in XHTML and
    make the whole document unreadable (fatal). Each is replaced by its
    numeric character reference - the identical character."""
    idx = PackageIndex.build(mp, package)
    changes = []
    for name in idx.text_files():
        raw = mp.get_bytes(name).decode("utf-8", errors="strict") if _is_utf8(mp.get_bytes(name)) else None
        if raw is None or "<!ENTITY" in raw:
            continue
        new = named_entities_to_numeric(raw)
        if new != raw:
            found = sorted(set(re.findall(r"&([A-Za-z][A-Za-z0-9]*);", raw)) - {"amp", "lt", "gt", "quot", "apos"})
            mp.set_bytes(name, new.encode("utf-8"))
            changes.append(_change(name, "document", ", ".join(f"&{e};" for e in found[:8]),
                                   "numeric character references",
                                   "undefined named entities replaced by the same characters", 0.99))
    return _result(changes)


def _is_utf8(b):
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def fix_resource_references(mp, package):
    """Every src / href / xlink:href / data reference (content documents,
    navigation, NCX) whose file does not resolve: backslashes, unescaped
    characters, wrong letter case, absolute filesystem paths, stale split
    file names (via the unique document holding the #fragment) and wrong
    directories (via the unique file with that name)."""
    idx = PackageIndex.build(mp, package)
    changes, review = [], []
    for name in idx.text_files():
        raw_b = mp.get_bytes(name)
        if not _is_utf8(raw_b):
            continue
        root = idx.docs.get(name, (None, False))[0]
        if root is None:
            root, _ok = parse_lenient(raw_b)
        if root is None:
            continue
        raw = raw_b.decode("utf-8")
        new_raw = raw
        done = set()
        for el, ln, attr, value in _iter_refs(root):
            path = value.partition("#")[0]
            if not path or (value, attr) in done:
                continue
            if not _ABS_FS_RE.match(path) and (_EXTERNAL_RE.match(path) or path.startswith("data:")):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(path)))
            if "\\" not in path and " " not in path and target in idx.names:
                continue
            new, conf, why = resolve_resource(idx, name, value, is_link=(ln in ("a", "content")))
            if new is None:
                if why not in ("resolves", "external", "same-document"):
                    review.append({"file": name, "reference": value, "reason": why})
                continue
            new_raw, n = replace_attr_value(new_raw, attr, value, new)
            if n:
                done.add((value, attr))
                changes.append(_change(name, f"<{ln} {_attr_name(attr)}>", value, new, why, conf))
        if new_raw != raw:
            mp.set_bytes(name, new_raw.encode("utf-8"))
    # stylesheet url(...) references
    for name in [n for n in idx.names if n.lower().endswith(".css")]:
        raw_b = mp.get_bytes(name)
        if not _is_utf8(raw_b):
            continue
        raw = raw_b.decode("utf-8")
        out = raw
        for m in set(re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", raw)):
            if _EXTERNAL_RE.match(m) and not _ABS_FS_RE.match(m) or m.startswith("data:"):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(m.replace("\\", "/"))))
            if target in idx.names and "\\" not in m:
                continue
            new, conf, why = resolve_resource(idx, name, m)
            if new is None:
                if why not in ("resolves", "external"):
                    review.append({"file": name, "reference": m, "reason": why})
                continue
            out = out.replace(m, new)
            changes.append(_change(name, "url()", m, new, why, conf))
        if out != raw:
            mp.set_bytes(name, out.encode("utf-8"))
    return _result(changes, review)


def fix_broken_fragments(mp, package):
    """Links (content, navigation, NCX) to an id that does not exist in the
    target document: URL-encoding, letter case, an id moved to another
    document by a split, an id renamed (separators). Only one certain
    target is ever used; anything else is left for review."""
    idx = PackageIndex.build(mp, package)
    changes, review = [], []
    for name in idx.text_files():
        raw_b = mp.get_bytes(name)
        if not _is_utf8(raw_b):
            continue
        root = idx.docs.get(name, (None, False))[0]
        if root is None:
            root = parse_lenient(raw_b)[0]
        if root is None:
            continue
        raw = raw_b.decode("utf-8")
        new_raw = raw
        for el, ln, attr, value in _iter_refs(root):
            if ln not in ("a", "content") or "#" not in value:
                continue
            path, _s, frag = value.partition("#")
            if _EXTERNAL_RE.match(path) or not frag:
                continue
            target = name if not path else posixpath.normpath(
                posixpath.join(posixpath.dirname(name), unquote(path)))
            if target not in idx.docs:
                continue                                  # a missing FILE: fix_resource_references
            if unquote(frag) in idx.ids.get(target, set()) and value == value.replace(" ", "%20"):
                continue
            new, conf, why = resolve_fragment(idx, name, value)
            if new is None:
                if why != "resolves":
                    review.append({"file": name, "reference": value, "reason": why,
                                   "text": " ".join("".join(el.itertext()).split())[:60]})
                continue
            new_raw, n = replace_attr_value(new_raw, attr, value, new)
            if n:
                changes.append(_change(name, f"<{ln} {_attr_name(attr)}>", value, new, why, conf))
        if new_raw != raw:
            mp.set_bytes(name, new_raw.encode("utf-8"))
    return _result(changes, review)


_PRIORITY = {"h1": 6, "h2": 6, "h3": 6, "h4": 6, "h5": 6, "h6": 6, "section": 5, "aside": 5, "figure": 5,
             "table": 5, "li": 4, "nav": 4, "div": 2, "p": 2, "span": 1, "a": 1}


def fix_duplicate_ids_safely(mp, package):
    """Duplicate id inside a document. Decides which occurrence is the
    intended target from the references pointing at it (link / nav text
    matching the element's own text, element kind), keeps the id there,
    renames every other occurrence deterministically (<id>-2, -3 ...) and
    redirects any reference whose context identifies a renamed occurrence.
    Nothing is removed."""
    idx = PackageIndex.build(mp, package)
    changes, review = [], []
    allowed = Allowed()
    # every reference (doc, raw value, link text, attr) to (target doc, id)
    refs = defaultdict(list)
    for name in idx.text_files():
        root = idx.docs.get(name, (None, False))[0]
        if root is None:
            root = parse_lenient(mp.get_bytes(name))[0]
        if root is None:
            continue
        for el, ln, attr, value in _iter_refs(root):
            if ln not in ("a", "content") or "#" not in value:
                continue
            path, _s, frag = value.partition("#")
            if _EXTERNAL_RE.match(path):
                continue
            target = name if not path else posixpath.normpath(
                posixpath.join(posixpath.dirname(name), unquote(path)))
            label = " ".join("".join(el.itertext()).split())
            if ln == "content":                     # NCX: label is the sibling navLabel
                par = el.getparent()
                label = " ".join("".join(par.itertext()).split()) if par is not None else ""
            refs[(target, unquote(frag))].append((name, value, label, attr))
    edits = defaultdict(list)                       # doc -> [(attr_value, new, nth)]
    for doc, (root, ok) in idx.docs.items():
        if root is None or not ok:
            continue
        occ = defaultdict(list)
        for el in root.iter():
            if isinstance(el.tag, str) and el.get("id"):
                occ[el.get("id")].append(el)
        taken = set(occ)
        for id_, els in occ.items():
            if len(els) < 2:
                continue
            texts = [" ".join("".join(e.itertext()).split()).lower() for e in els]
            votes = [0] * len(els)
            pointing = refs.get((doc, id_), [])
            ref_choice = []
            for (_src, _val, label, _attr) in pointing:
                lab = label.lower()
                hit = [k for k, t in enumerate(texts) if lab and (t == lab or (len(lab) > 3 and t.startswith(lab)))]
                choice = hit[0] if len(hit) == 1 else None
                ref_choice.append(choice)
                if choice is not None:
                    votes[choice] += 1
            keeper = max(range(len(els)), key=lambda k: (votes[k], _PRIORITY.get(_local(els[k].tag), 0), -k))
            new_ids = {}
            n = 2
            for k in range(len(els)):
                if k == keeper:
                    continue
                cand = f"{id_}-{n}"
                while cand in taken:
                    n += 1
                    cand = f"{id_}-{n}"
                taken.add(cand)
                new_ids[k] = cand
                n += 1
                edits[doc].append(("id", id_, cand, k))
                allowed.renamed_ids[(doc, id_)] = cand
            conflicted = any(c is not None and c != keeper for c in ref_choice)
            conf = 0.99 if not conflicted else 0.95
            changes.append(_change(doc, f"id='{id_}' x{len(els)}", f"id='{id_}' on {len(els)} elements",
                                   f"kept on <{_local(els[keeper].tag)}> (occurrence {keeper + 1}), "
                                   f"renamed {', '.join(new_ids.values())}",
                                   f"duplicate id; {len(pointing)} reference(s) analysed", conf))
            for (src, val, label, attr), choice in zip(pointing, ref_choice):
                if choice is not None and choice != keeper:
                    path = val.partition("#")[0]
                    new_val = (path + "#" if path else "#") + new_ids[choice]
                    edits[src].append(("ref", attr, val, new_val))
                    changes.append(_change(src, "reference", val, new_val,
                                           f"reference text '{label[:40]}' identifies the renamed occurrence", 0.95))
    for doc, ops in edits.items():
        raw = mp.get_bytes(doc).decode("utf-8")
        # rename later occurrences from the END so earlier match indices stay valid
        id_ops = sorted((op for op in ops if op[0] == "id"), key=lambda o: -o[3])
        for _k, old, new, nth in id_ops:
            raw, _n = replace_attr_value(raw, "id", old, new, nth=nth)
        for _k, attr, val, new_val in (op for op in ops if op[0] == "ref"):
            raw, _n = replace_attr_value(raw, attr, val, new_val)
        mp.set_bytes(doc, raw.encode("utf-8"))
    return _result(changes, review, allowed)


_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"),
          (b"GIF89a", "image/gif"), (b"wOFF", "font/woff"), (b"wOF2", "font/woff2"), (b"OTTO", "font/otf"),
          (b"\x00\x01\x00\x00", "font/ttf"), (b"ID3", "audio/mpeg")]


def sniff_media_type(data: bytes, name: str):
    for sig, mt in _MAGIC:
        if data.startswith(sig):
            return mt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip().lower()
    if b"<svg" in head:
        return "image/svg+xml"
    return None


_EQUIV = {"font/otf": {"application/vnd.ms-opentype", "application/font-sfnt", "font/otf"},
          "font/ttf": {"application/font-sfnt", "application/x-font-ttf", "font/ttf"},
          "font/woff": {"application/font-woff", "font/woff"}}


def fix_media_types(mp, package):
    """A manifest media-type that disagrees with the file. Corrected only
    when the file's own bytes AND its extension name the same type."""
    changes, review = [], []
    opf = package.opf_path
    if not opf or not mp.exists(opf):
        return _result([])
    raw = mp.get_bytes(opf).decode("utf-8")
    out = raw
    for item in package.manifest:
        path = package.resolve_href(item.href) if item.href else ""
        if not path or not mp.exists(path):
            continue
        sniffed = sniff_media_type(mp.get_bytes(path), path)
        if not sniffed or item.media_type == sniffed or item.media_type in _EQUIV.get(sniffed, ()):
            continue
        by_ext = _MEDIA_TYPE_BY_EXT.get(posixpath.splitext(path)[1].lower())
        if by_ext != sniffed:
            review.append({"file": path, "reference": item.media_type,
                           "reason": f"file content is {sniffed} but its extension says {by_ext} - rename needs review"})
            continue
        tag = next((m.group(0) for m in re.finditer(r"<item\b[^>]*>", out)
                    if re.search(r"\sid\s*=\s*[\"']%s[\"']" % re.escape(item.id), m.group(0))), None)
        if not tag:
            continue
        new_tag, n = replace_attr_value(tag, "media-type", item.media_type, sniffed)
        if n:
            out = out.replace(tag, new_tag, 1)
            changes.append(_change(opf, f"manifest item '{item.id}'", item.media_type, sniffed,
                                   "media-type corrected (file bytes and extension agree)", 0.99))
    if out != raw:
        mp.set_bytes(opf, out.encode("utf-8"))
    return _result(changes, review)


def fix_duplicate_spine(mp, package):
    """The same document listed twice in the spine: the later itemref is
    removed (the document stays in the reading order, once)."""
    opf = package.opf_path
    if not opf or not mp.exists(opf):
        return _result([])
    raw = mp.get_bytes(opf).decode("utf-8")
    seen, drop, changes = set(), [], []
    for m in re.finditer(r"<itemref\b[^>]*?/>|<itemref\b[^>]*>\s*</itemref>", raw):
        idref = re.search(r"idref\s*=\s*[\"']([^\"']+)", m.group(0))
        if not idref:
            continue
        if idref.group(1) in seen:
            drop.append(m.span())
            changes.append(_change(opf, "spine", m.group(0), "(removed)",
                                   f"'{idref.group(1)}' was already in the spine - duplicate reading-order entry",
                                   0.99))
        seen.add(idref.group(1))
    out = raw
    for a, b in reversed(drop):                     # positional, from the end
        out = out[:a] + out[b:]
    if out != raw:
        mp.set_bytes(opf, out.encode("utf-8"))
    return _result(changes)


def _strip_css_comments_strings(text):
    return re.sub(r"\"(\\.|[^\"\\])*\"|'(\\.|[^'\\])*'", "\"\"", re.sub(r"/\*.*?\*/", "", text, flags=re.S))


def fix_css_syntax(mp, package):
    """Stylesheets that end prematurely: an unterminated comment is closed,
    missing closing braces are appended at the end. No rule is removed or
    altered."""
    changes, review = [], []
    for name in [n for n in mp.order if n.lower().endswith(".css") and mp.exists(n)]:
        b = mp.get_bytes(name)
        if not _is_utf8(b):
            continue
        text = b.decode("utf-8")
        out = text
        if out.count("/*") > out.count("*/") and out.rfind("/*") > out.rfind("*/"):
            out = out.rstrip("\n") + " */\n"
            changes.append(_change(name, "end of file", "unterminated comment", "*/", "comment closed", 0.98))
        clean = _strip_css_comments_strings(out)
        opens, closes = clean.count("{"), clean.count("}")
        if opens > closes:
            missing = opens - closes
            out = out.rstrip("\n") + (" " + "}" * missing) + "\n"
            changes.append(_change(name, "end of file", f"{missing} unclosed block(s)", "}" * missing,
                                   "missing closing brace(s) appended", 0.97))
        elif closes > opens:
            review.append({"file": name, "reference": f"{closes - opens} extra '}}'",
                           "reason": "extra closing brace - position cannot be determined safely"})
        if out != text:
            mp.set_bytes(name, out.encode("utf-8"))
    return _result(changes, review)


def fix_package_hygiene(mp, package):
    """Temporary / backup / OS files (.DS_Store, Thumbs.db, *.bak, *.tmp,
    __MACOSX ...) are not packaged - only when nothing declares or
    references them."""
    declared = {package.resolve_href(i.href) for i in package.manifest if i.href}
    referenced = set()
    for n in mp.order:
        if mp.exists(n) and n.lower().endswith((".xhtml", ".html", ".css", ".opf", ".ncx", ".xml")):
            try:
                referenced.add(mp.get_bytes(n).decode("utf-8", errors="ignore"))
            except Exception:
                pass
    blob = "\n".join(referenced)
    changes = []
    allowed = Allowed()
    for n in list(mp.order):
        if not mp.exists(n) or not JUNK_RE.search(n) or n in declared:
            continue
        if posixpath.basename(n) in blob:
            continue
        mp.remove_entry(n)
        allowed.removed_files.add(n)
        changes.append(_change(n, "archive", n, "(not packaged)", "temporary / OS / backup file", 1.0))
    return _result(changes, allowed=allowed)


def fix_dcterms_modified(mp, package):
    """EPUB 3 requires exactly one <meta property="dcterms:modified">. When
    it is MISSING, the package's last-modification timestamp (now, UTC) is
    added - package metadata, never book content. Several existing ones are
    only reported (metadata is never deleted automatically)."""
    import datetime
    opf = package.opf_path
    if not opf or not mp.exists(opf):
        return _result([])
    raw = mp.get_bytes(opf).decode("utf-8")
    found = re.findall(r"<meta\b[^>]*property\s*=\s*[\"']dcterms:modified[\"'][^>]*>", raw)
    if found:
        review = [{"file": opf, "reference": f"{len(found)} dcterms:modified",
                   "reason": "more than one modification date - choose which to keep"}] if len(found) > 1 else []
        return _result([], review)
    m = re.search(r"</(?:\w+:)?metadata\s*>", raw)
    if not m:
        return _result([])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = "opf:" if re.search(r"<opf:metadata\b", raw) else ""
    tag = f'<{prefix}meta property="dcterms:modified">{stamp}</{prefix}meta>'
    out = raw[:m.start()] + tag + raw[m.start():]
    mp.set_bytes(opf, out.encode("utf-8"))
    return _result([_change(opf, "metadata", "(missing)", tag, "required EPUB 3 last-modified date added", 0.99)])


def fix_unique_identifier(mp, package):
    """package@unique-identifier must name the id of a dc:identifier. With
    exactly ONE dc:identifier the reference is pointed at it (adding an id
    to it when it has none). No identifier at all is never invented - the
    ISBN has to come from the user."""
    opf = package.opf_path
    if not opf or not mp.exists(opf):
        return _result([])
    raw = mp.get_bytes(opf).decode("utf-8")
    pkg = re.search(r"<(?:\w+:)?package\b[^>]*>", raw)
    if not pkg:
        return _result([])
    uid = re.search(r"unique-identifier\s*=\s*[\"']([^\"']*)[\"']", pkg.group(0))
    idents = list(re.finditer(r"<dc:identifier\b([^>]*)>", raw))
    if uid and any(re.search(r"\sid\s*=\s*[\"']%s[\"']" % re.escape(uid.group(1)), m.group(1)) for m in idents):
        return _result([])
    if len(idents) != 1:
        return _result([], [{"file": opf, "reference": "dc:identifier",
                             "reason": "no dc:identifier (enter the ISBN)" if not idents else
                             f"{len(idents)} identifiers - choose the unique one"}])
    m = idents[0]
    ident_id = re.search(r"\sid\s*=\s*[\"']([^\"']+)[\"']", m.group(1))
    changes = []
    out = raw
    if ident_id:
        new_pkg = re.sub(r"unique-identifier\s*=\s*[\"'][^\"']*[\"']", f'unique-identifier="{ident_id.group(1)}"',
                         pkg.group(0)) if uid else pkg.group(0)[:-1].rstrip("/") + \
            f' unique-identifier="{ident_id.group(1)}">'
        out = out.replace(pkg.group(0), new_pkg, 1)
        changes.append(_change(opf, "package", uid.group(1) if uid else "(none)", ident_id.group(1),
                               "unique-identifier now names the package's only dc:identifier", 0.99))
    elif uid:
        new_tag = m.group(0)[:-1] + f' id="{uid.group(1)}">'
        out = out.replace(m.group(0), new_tag, 1)
        changes.append(_change(opf, "dc:identifier", m.group(0), new_tag,
                               "id added so unique-identifier resolves to the only dc:identifier", 0.99))
    if out != raw:
        mp.set_bytes(opf, out.encode("utf-8"))
    return _result(changes)


_LINK_ROLES = {"button", "checkbox", "doc-backlink", "doc-biblioref", "doc-glossref", "doc-noteref", "link",
               "menuitem", "menuitemcheckbox", "menuitemradio", "option", "radio", "switch", "tab", "treeitem"}


def fix_invalid_link_roles(mp, package):
    """A role that is not allowed on a link (<a href role="doc-chapter">,
    typical in landmarks navigation) is removed from that link only - the
    link, its text, target and epub:type stay."""
    idx = PackageIndex.build(mp, package)
    changes = []
    for name in idx.docs:
        raw_b = mp.get_bytes(name)
        if not _is_utf8(raw_b):
            continue
        raw = raw_b.decode("utf-8")

        def repl(m):
            tag = m.group(0)
            if not re.search(r"\shref\s*=", tag):
                return tag
            role = re.search(r"\srole\s*=\s*([\"'])([^\"']*)\1", tag)
            if not role or all(r in _LINK_ROLES for r in role.group(2).split()):
                return tag
            new = tag.replace(role.group(0), "", 1)
            changes.append(_change(name, "<a>", role.group(0).strip(), "(removed)",
                                   "role not allowed on a link; epub:type / text / href kept", 0.97))
            return new
        out = re.sub(r"<a\b[^>]*>", repl, raw)
        if out != raw:
            mp.set_bytes(name, out.encode("utf-8"))
    return _result(changes)
