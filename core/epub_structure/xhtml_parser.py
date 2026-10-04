"""Step 2 (spec section 25) - parses every XHTML file SAFELY. A single,
tolerant entry point every later analysis step (registry/heading/pagebreak/
link) calls, so a malformed file is reported once, consistently, rather
than each analyzer independently deciding how to react to a parse
failure."""
from dataclasses import dataclass
from io import BytesIO

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NSMAP = {"x": XHTML_NS, "epub": EPUB_NS}


@dataclass
class ParsedDocument:
    path: str                 # project-relative POSIX path
    tree: object = None        # lxml root Element, or None on failure
    raw_bytes: bytes = b""
    error: str = ""             # non-empty only when tree is None
    doctype: str = ""           # e.g. "<!DOCTYPE html>", or "" if the source file had none


def parse_xhtml_file(scan, relative_path: str) -> ParsedDocument:
    abs_path = scan.abspath(relative_path)
    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return ParsedDocument(path=relative_path, error=f"Could not read file: {e}")
    try:
        parser = etree.XMLParser(resolve_entities=False, recover=False)
        # etree.parse (not etree.fromstring) so the original file's own
        # DOCTYPE is available via .docinfo before it's discarded - a real,
        # confirmed bug otherwise: .fromstring only ever returns a root
        # Element, with no live connection to any DOCTYPE the source file
        # had, so writing that Element back out later (orchestrator.
        # _write_tree) silently dropped every "<!DOCTYPE html>" line this
        # pipeline ever rewrote, in every project, on every run.
        doc = etree.parse(BytesIO(raw), parser=parser)
        tree = doc.getroot()
        doctype = doc.docinfo.doctype or ""
    except etree.XMLSyntaxError as e:
        return ParsedDocument(path=relative_path, raw_bytes=raw, error=f"Not well-formed XML: {e}")
    return ParsedDocument(path=relative_path, tree=tree, raw_bytes=raw, doctype=doctype)


def local_name(tag) -> str:
    """el.tag with any {namespace} prefix stripped, or "" for a comment/
    processing-instruction node (whose .tag is a callable, not a string) -
    the one place every analyzer strips namespaces, so "h1" always means
    the same thing whether or not a given document declares the XHTML
    namespace by default."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def epub_type(el) -> str:
    return el.get(f"{{{EPUB_NS}}}type") or el.get("epub:type") or ""


def iter_by_local_name(tree, *names):
    wanted = set(names)
    for el in tree.iter():
        if local_name(el.tag) in wanted:
            yield el
