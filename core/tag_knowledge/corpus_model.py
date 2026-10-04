"""Statistical model of a reference XML / XHTML corpus.

Every file is parsed (namespaces stripped) and each element is resolved to a
zone-level element name - either directly (the corpus is intermediate XML
using the project's own zone tags) or through Mapping.xml's reverse map (the
corpus is final XHTML, e.g. <p class="poemline"> -> poemline). The model
then records, across ALL files (never a single sample):

  * element frequencies and document frequencies (in how many files);
  * parent -> child counts, both for raw elements and zone-level ones;
  * reading-order succession (which zone-level element follows which);
  * attribute names/values per element;
  * inline formatting elements (elements found inside mixed content);
  * per-element text features (length, leading marker, final punctuation,
    share of italic / underlined text) - the "what does a <x> look like"
    evidence Auto Tag compares candidate zones against.
"""
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from lxml import etree

_MARKER_RE = re.compile(r"^\s*(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxlcdmIVXLCDM]{1,6}[.)]|[•·●○◦▪■\-–—*])\s+")
_DIGIT_START_RE = re.compile(r"^\s*\[?\d")


def _local(tag) -> str:
    if not isinstance(tag, str):
        return None
    return tag.split("}", 1)[1] if "}" in tag else tag


@dataclass
class TextStats:
    count: int = 0
    total_len: int = 0
    total_len_sq: float = 0.0
    marker: int = 0
    digit_start: int = 0
    end_punct: int = 0
    short: int = 0
    italic_chars: int = 0
    decorated_chars: int = 0
    chars: int = 0

    def add(self, text: str, italic_chars: int = 0, decorated_chars: int = 0):
        t = (text or "").strip()
        n = len(t)
        self.count += 1
        self.total_len += n
        self.total_len_sq += n * n
        self.chars += n
        self.italic_chars += italic_chars
        self.decorated_chars += decorated_chars
        if _MARKER_RE.match(t):
            self.marker += 1
        if _DIGIT_START_RE.match(t):
            self.digit_start += 1
        if t and t[-1] in ".!?:;\"'”’)":
            self.end_punct += 1
        if n <= 60:
            self.short += 1

    def summary(self) -> dict:
        c = max(self.count, 1)
        mean = self.total_len / c
        var = max(0.0, self.total_len_sq / c - mean * mean)
        return {
            "count": self.count, "mean_len": round(mean, 1), "std_len": round(var ** 0.5, 1),
            "p_marker": round(self.marker / c, 3), "p_digit_start": round(self.digit_start / c, 3),
            "p_end_punct": round(self.end_punct / c, 3), "p_short": round(self.short / c, 3),
            "italic_ratio": round(self.italic_chars / max(self.chars, 1), 3),
            "decorated_ratio": round(self.decorated_chars / max(self.chars, 1), 3),
        }


@dataclass
class CorpusModel:
    files: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    element_counts: Counter = field(default_factory=Counter)
    doc_frequency: Counter = field(default_factory=Counter)
    raw_parent_child: Counter = field(default_factory=Counter)
    zone_parent_child: Counter = field(default_factory=Counter)
    succession: Counter = field(default_factory=Counter)
    first_elements: Counter = field(default_factory=Counter)
    attributes: dict = field(default_factory=lambda: defaultdict(Counter))
    inline_elements: Counter = field(default_factory=Counter)
    text_stats: dict = field(default_factory=lambda: defaultdict(TextStats))

    @property
    def available(self) -> bool:
        return bool(self.files)

    # ------------------------------------------------------------ build
    @classmethod
    def from_directory(cls, directory: str, known_elements=None, mapping=None,
                       italic_tags=("i", "em", "italic"), decoration_tags=("u", "underline", "s", "strike")):
        paths = []
        if directory and os.path.isdir(directory):
            for root, _dirs, files in os.walk(directory):
                for name in sorted(files):
                    if name.lower().endswith((".xml", ".xhtml", ".html", ".htm")):
                        paths.append(os.path.join(root, name))
        return cls.from_files(paths, known_elements, mapping, italic_tags, decoration_tags)

    @classmethod
    def from_files(cls, paths, known_elements=None, mapping=None,
                   italic_tags=("i", "em", "italic"), decoration_tags=("u", "underline", "s", "strike")):
        model = cls()
        known = set(known_elements or [])
        parser = etree.XMLParser(recover=True, remove_comments=True, resolve_entities=False, no_network=True)
        for path in paths:
            try:
                root = etree.parse(path, parser).getroot()
            except Exception as e:  # noqa: BLE001
                model.errors.append(f"{path}: {e}")
                continue
            if root is None:
                model.errors.append(f"{path}: empty document")
                continue
            model.files.append(path)
            model._analyse(root, known, mapping, set(italic_tags), set(decoration_tags))
        return model

    def _zone_name(self, el, known, mapping):
        local = _local(el.tag)
        if local is None:
            return None
        # A final-output element whose class Mapping.xml produces from a
        # specific zone element (<p class="poemline"> <- poemline) maps back
        # to that element first; otherwise the element name itself.
        if mapping is not None and el.get("class"):
            back = mapping.intermediate_for(local, el.get("class"))
            if back:
                return back
        if local in known:
            return local
        if mapping is not None:
            return mapping.intermediate_for(local, None)
        return None

    def _analyse(self, root, known, mapping, italic_tags, decoration_tags):
        seen_in_doc = set()
        sequence = []
        for el in root.iter():
            local = _local(el.tag)
            if local is None:
                continue
            self.element_counts[local] += 1
            seen_in_doc.add(local)
            parent = el.getparent()
            if parent is not None and _local(parent.tag):
                self.raw_parent_child[(_local(parent.tag), local)] += 1
                if (parent.text and parent.text.strip()) or any(
                        (sib.tail and sib.tail.strip()) for sib in parent):
                    self.inline_elements[local] += 1
            for k, v in el.attrib.items():
                self.attributes[local][(_local(k) or k, v if len(v) < 60 else v[:60])] += 1
            zone = self._zone_name(el, known, mapping)
            if zone is None:
                continue
            sequence.append(zone)
            seen_in_doc.add(zone)
            anc = el.getparent()
            zone_parent = None
            while anc is not None:
                zone_parent = self._zone_name(anc, known, mapping) or _local(anc.tag)
                if zone_parent:
                    break
                anc = anc.getparent()
            if zone_parent:
                self.zone_parent_child[(zone_parent, zone)] += 1
            text = "".join(el.itertext())
            italic = sum(len("".join(x.itertext())) for x in el.iter() if _local(x.tag) in italic_tags)
            deco = sum(len("".join(x.itertext())) for x in el.iter() if _local(x.tag) in decoration_tags)
            self.text_stats[zone].add(text, italic, deco)
        if sequence:
            self.first_elements[sequence[0]] += 1
        for a, b in zip(sequence, sequence[1:]):
            self.succession[(a, b)] += 1
        for name in seen_in_doc:
            self.doc_frequency[name] += 1

    # ------------------------------------------------------------ query
    def frequency(self, element: str) -> float:
        total = sum(self.text_stats[e].count for e in self.text_stats) or 1
        return self.text_stats[element].count / total if element in self.text_stats else 0.0

    def succession_probability(self, prev: str, element: str):
        """P(element | prev) from the corpus, or None if prev never seen."""
        total = sum(c for (a, _b), c in self.succession.items() if a == prev)
        if not total:
            return None
        return self.succession.get((prev, element), 0) / total

    def seen_parent_child(self, parent: str, child: str) -> bool:
        return (parent, child) in self.zone_parent_child or (parent, child) in self.raw_parent_child

    def stats_for(self, element: str) -> dict:
        return self.text_stats[element].summary() if element in self.text_stats else None

    def summary(self) -> dict:
        return {
            "files": len(self.files),
            "errors": list(self.errors),
            "zone_elements": {k: v.summary() for k, v in sorted(self.text_stats.items())},
            "top_succession": [(f"{a} -> {b}", c) for (a, b), c in self.succession.most_common(25)],
            "inline_elements": dict(self.inline_elements.most_common(20)),
        }
