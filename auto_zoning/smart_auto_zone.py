"""AUTO ZONE / AUTO TAG orchestrator (page and document).

    PDF -> layout_engine (glyph lines, decorations, columns, floats, notes)
        -> semantic_classifier (role candidates with evidence)
        -> auto_tag_engine + TagKnowledgeModel (project tag, DTD/structure)
        -> ZoneManager (zones with confidence / review state)

Priority of decisions (highest first):
    USER MANUAL OVERRIDE > LOCKED ZONE > AUTO TAG MODEL > SEMANTIC > LAYOUT > OCR
A protected zone (locked, manually overridden, or drawn by hand) is never
deleted, moved or retagged here; candidate zones overlapping one are dropped.
Re-analysis replaces only this engine's own unprotected zones.

Caching: page analyses (layout + role candidates, never tag decisions) are
cached in memory and on disk keyed by PDF fingerprint + page + engine
version + document context, so changing a tag or a formatting decision never
re-runs layout analysis or OCR, and a document run can resume after a stop.
"""
import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from auto_zoning import layout_engine, semantic_classifier, auto_tag_engine, continuity, section_context
from auto_zoning.layout_engine import LayoutBlock, PageLayout, DocumentContext
from auto_zoning.semantic_classifier import RoleCandidate

ENGINE_VERSION = "layout-semantic-v1"
ANALYSIS_VERSION = 3          # bump when layout / role logic changes: cached page analyses are redone
OVERLAP_DROP = 0.5            # a candidate overlapping a protected zone by this much is dropped
_MEMORY_CACHE_MAX = 64


@dataclass
class PageResult:
    page: int
    created: list = field(default_factory=list)
    removed: int = 0
    dropped_for_protected: int = 0
    unmapped: list = field(default_factory=list)       # roles with no project tag (not zoned)
    needs_review: int = 0
    retagged: int = 0
    decisions: list = field(default_factory=list)
    continuations: list = field(default_factory=list)  # continuity.ContinuationDecision
    error: str = None


@dataclass
class DocumentRunState:
    completed: list = field(default_factory=list)
    total: int = 0
    fingerprint: str = ""

    def to_dict(self):
        return {"completed": sorted(set(self.completed)), "total": self.total, "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(completed=list(d.get("completed", [])), total=int(d.get("total", 0)),
                   fingerprint=d.get("fingerprint", ""))


def pdf_fingerprint(pdf_document) -> str:
    path = getattr(pdf_document, "path", None) or getattr(getattr(pdf_document, "doc", None), "name", "") or ""
    try:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{pdf_document.page_count}"
    except OSError:
        raw = f"{path}|{pdf_document.page_count}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


# ------------------------------------------------------------- serialise
def _block_to_cache(b: LayoutBlock, cands) -> dict:
    d = b.to_dict()
    d["roles"] = [{"role": c.role, "score": c.score, "evidence": list(c.evidence)} for c in cands]
    d["line_sizes"] = [li.font_size for li in b.lines]
    return d


def _block_from_cache(d: dict):
    b = LayoutBlock(kind=d["kind"], bbox=tuple(d["bbox"]), features=dict(d.get("features", {})),
                    column=d.get("column", 0), band=d.get("band", 0), order=d.get("order", 0),
                    region=d.get("region", "body"))
    b.stored_text = d.get("text", "")
    b.features.setdefault("_line_sizes", d.get("line_sizes", []))
    cands = [RoleCandidate(r["role"], r["score"], list(r.get("evidence", []))) for r in d.get("roles", [])]
    return b, cands


class SmartAutoZoner:
    def __init__(self, pdf_document, zone_manager, profile: dict, settings: dict = None,
                 reference_template=None, cache_dir: str = None, knowledge=None):
        self.pdf = pdf_document
        self.zm = zone_manager
        self.profile = profile
        self.settings = settings if settings is not None else {}
        self.reference_template = reference_template
        self.cache_dir = cache_dir
        self._knowledge = knowledge
        self._context = None
        self._context_whole = False
        self._line_cache = {}
        self._memory = OrderedDict()
        self._lock = threading.RLock()
        self.fingerprint = pdf_fingerprint(pdf_document)

    # ------------------------------------------------------- knowledge
    @property
    def knowledge(self):
        if self._knowledge is None:
            from core.tag_knowledge import TagKnowledgeModel
            self._knowledge = TagKnowledgeModel.build(self.profile, self.settings, self.reference_template)
        return self._knowledge

    def thresholds(self) -> dict:
        return self.settings.get("auto_zone_thresholds") or {"high": 90, "medium": 75}

    # --------------------------------------------------------- context
    def context(self, around_page: int = None, whole_document: bool = False) -> DocumentContext:
        with self._lock:
            if self._context is not None and (not whole_document or self._context_whole):
                return self._context
            cached = self._disk_read("context.json")
            n = self.pdf.page_count
            if cached and (not whole_document or cached.get("whole")):
                ctx = DocumentContext(body_size=cached["body_size"], heading_sizes=cached["heading_sizes"],
                                      running_signatures=set(cached["running_signatures"]),
                                      paragraph_indent=cached["paragraph_indent"],
                                      pages_seen=cached.get("pages_seen", []))
                self._context, self._context_whole = ctx, bool(cached.get("whole"))
                return ctx
            if whole_document or n <= 40:
                pages = list(range(1, n + 1))
                whole = True
            else:
                c = around_page or 1
                pages = sorted(set(range(max(1, c - 8), min(n, c + 8) + 1)) |
                               set(range(1, n + 1, max(1, n // 12))))
                whole = False
            ctx = layout_engine.build_document_context(self.pdf, pages, self._line_cache)
            self._context, self._context_whole = ctx, whole
            d = ctx.to_dict()
            d["whole"] = whole
            self._disk_write("context.json", d)
            return ctx

    def _context_key(self, ctx) -> str:
        d = ctx.to_dict()
        d.pop("pages_seen", None)
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:10]

    # ----------------------------------------------------------- cache
    @property
    def cache_folder(self):
        """This document's analysis cache folder (versioned with ANALYSIS_VERSION)."""
        return os.path.join(self.cache_dir, f"{self.fingerprint}-a{ANALYSIS_VERSION}") if self.cache_dir else None

    def _disk_path(self, name):
        if not self.cache_dir:
            return None
        d = os.path.join(self.cache_dir, f"{self.fingerprint}-a{ANALYSIS_VERSION}")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def _disk_read(self, name):
        path = self._disk_path(name)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if data.get("engine") == ENGINE_VERSION else None
        except (OSError, ValueError):
            return None

    def _disk_write(self, name, data):
        path = self._disk_path(name)
        if not path:
            return
        try:
            data = dict(data, engine=ENGINE_VERSION)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def invalidate(self, page: int = None):
        with self._lock:
            if page is None:
                self._memory.clear()
                self._context = None
            else:
                for k in [k for k in self._memory if k[0] == page]:
                    self._memory.pop(k, None)
            if self.cache_dir:
                d = os.path.join(self.cache_dir, f"{self.fingerprint}-a{ANALYSIS_VERSION}")
                if os.path.isdir(d):
                    for name in os.listdir(d):
                        if page is None or name.startswith(f"p{page:05d}_"):
                            try:
                                os.remove(os.path.join(d, name))
                            except OSError:
                                pass

    # -------------------------------------------------------- analysis
    def analyse(self, page: int, force: bool = False):
        """(layout, role_map) for a page - from cache when possible."""
        ctx = self.context(around_page=page)
        key = (page, self._context_key(ctx))
        name = f"p{page:05d}_{key[1]}.json"
        with self._lock:
            if not force and key in self._memory:
                self._memory.move_to_end(key)
                return self._memory[key]
        if not force:
            cached = self._disk_read(name)
            if cached:
                layout = PageLayout(page=page, width=cached["width"], height=cached["height"],
                                    source=cached.get("source", "pdf"),
                                    footnote_rule_y=cached.get("footnote_rule_y"),
                                    columns=cached.get("columns", []), stats=cached.get("stats", {}))
                role_map = {}
                for bd in cached["blocks"]:
                    b, cands = _block_from_cache(bd)
                    layout.blocks.append(b)
                    role_map[id(b)] = cands
                self._remember(key, (layout, role_map))
                return layout, role_map
        ocr_result = self._cached_ocr(page)
        layout = layout_engine.analyse_page(self.pdf, page, ctx, ocr_result=ocr_result)
        role_map = semantic_classifier.classify_page(layout, ctx, page_index_in_doc=page - 1)
        data = layout.to_dict()
        data["blocks"] = [_block_to_cache(b, role_map[id(b)]) for b in layout.blocks]
        self._disk_write(name, data)
        self._remember(key, (layout, role_map))
        return layout, role_map

    def _remember(self, key, value):
        with self._lock:
            self._memory[key] = value
            while len(self._memory) > _MEMORY_CACHE_MAX:
                self._memory.popitem(last=False)

    def _cached_ocr(self, page: int):
        """Cached OCR only - Auto Zone never starts an OCR run by itself
        (run 'Prepare OCR Cache' / 'OCR Entire Document' first for scans)."""
        try:
            from core.ocr import ocr_service
            return ocr_service.get_cached_ocr_for_page(self.pdf, page)
        except Exception:
            return None

    # ------------------------------------------------- section context
    def sections(self):
        """Document-wide references / endnotes section map (text layer
        only, built once, cached on disk)."""
        with self._lock:
            if getattr(self, "_sections", None) is not None:
                return self._sections
            cached = self._disk_read("sections.json")
            if cached:
                self._sections = section_context.SectionMap.from_dict(cached)
            else:
                try:
                    self._sections = section_context.build_section_map(self.pdf, self.pdf.page_count)
                except Exception:
                    self._sections = section_context.SectionMap()
                self._disk_write("sections.json", self._sections.to_dict())
            return self._sections

    def decide(self, page: int, force_analysis: bool = False):
        layout, role_map = self.analyse(page, force=force_analysis)
        if self.settings.get("auto_zone_sections", True):
            smap = self.sections()
            on_page = smap.has_sections and (smap.region_at(page, 0) or smap.region_at(page, 10 ** 6) or
                                             any(h.page == page and h.kind for h in smap.headings))
            if on_page:
                try:
                    lines = section_context.page_text_lines(self.pdf.get_page(page))
                except Exception:
                    lines = []
                layout, role_map = section_context.split_section_entries(layout, role_map, smap, page, lines,
                                                                         LayoutBlock)
            role_map = section_context.adjust_roles(layout, role_map, smap, page, semantic_classifier.RoleCandidate)
        decisions = auto_tag_engine.decide_page(layout, role_map, self.knowledge, self.thresholds())
        return layout, decisions

    # ---------------------------------------------------------- apply
    def _protected_on_page(self, page):
        return [z for z in self.zm.zones_on_page(page) if z.protected or
                z.attributes.get("auto_engine") != ENGINE_VERSION]

    def _engine_zones_on_page(self, page):
        return [z for z in self.zm.zones_on_page(page)
                if z.attributes.get("auto_engine") == ENGINE_VERSION and not z.protected]

    def auto_zone_page(self, page: int, reanalyse: bool = False, decisions=None, layout=None) -> PageResult:
        """Creates zones for one page. Must run on the thread that owns the
        ZoneManager (the GUI thread)."""
        result = PageResult(page=page)
        try:
            if decisions is None:
                layout, decisions = self.decide(page, force_analysis=reanalyse)
        except Exception as e:  # noqa: BLE001 - one bad page never aborts a document run
            result.error = f"{type(e).__name__}: {e}"
            return result
        result.decisions = decisions
        self.zm.begin_batch()
        try:
            if reanalyse:
                for z in self._engine_zones_on_page(page):
                    self.zm.delete_zone(z.zone_id, cascade=False)
                    result.removed += 1
            else:
                # Without explicit re-analysis, a page that already has this
                # engine's zones is left alone (idempotent Auto Zone).
                if self._engine_zones_on_page(page):
                    return result
            protected = self._protected_on_page(page)
            new_zones = []
            for dec in decisions:
                if dec.option is None:
                    if dec.role:
                        result.unmapped.append(dec.role)
                    continue
                bbox = list(dec.block.bbox)
                if any(_overlap(bbox, z.bbox) > OVERLAP_DROP for z in protected):
                    result.dropped_for_protected += 1
                    continue
                attrs = dec.zone_attributes()
                ocr_block = layout is not None and layout.source == "ocr"
                if ocr_block:
                    attrs["source"] = "ocr"   # no digital text layer: keep the OCR text as the zone text
                zone = self.zm.add_zone(page, dec.tag, bbox, attributes=attrs, auto_parent=True)
                if ocr_block and dec.block.text:
                    zone.text = dec.block.text
                new_zones.append((dec, zone))
                result.created.append(zone.zone_id)
                if dec.needs_review:
                    result.needs_review += 1
            self._order_page(page, layout, new_zones, protected)
            if new_zones and self.settings.get("auto_zone_continuations", True):
                result.continuations = self.link_continuations(page)
        finally:
            self.zm.end_batch()
        return result

    def link_continuations(self, page: int) -> list:
        """Cross-page continuation (auto_zoning.continuity) at both edges
        of `page`: its first zones against the previous page, and the next
        page's first zones against this page (when the next page is already
        zoned). Recorded as ordinary Merge Previous links."""
        ctx = self.context(around_page=page)
        cache = {}
        out = []
        for p in (page, page + 1):
            if 2 <= p <= self.pdf.page_count and self.zm.zones_on_page(p) and self.zm.zones_on_page(p - 1):
                try:
                    out.extend(continuity.link_page_boundary(
                        self.zm, self.pdf, p, self.profile, paragraph_indent=ctx.paragraph_indent,
                        body_size=ctx.body_size, line_cache=cache))
                except Exception:  # noqa: BLE001 - continuation linking never breaks Auto Zone
                    pass
        return out

    def _order_page(self, page, layout, new_zones, protected):
        """Reading order from the layout analysis: new zones take their
        layout order; pre-existing zones are slotted in at the position of
        the layout block they overlap most (or by their top edge)."""
        if not new_zones:
            return
        page_marker_tags = set(self.profile.get("page_marker_tags") or [])
        keyed = []
        for dec, zone in new_zones:
            keyed.append((dec.block.order, zone))
        blocks = list(layout.blocks) if layout is not None else [d.block for d, _ in new_zones]
        for z in protected:
            if z.parent_id is not None:
                continue
            best, best_ov = None, 0.0
            for b in blocks:
                ov = _overlap(z.bbox, b.bbox)
                if ov > best_ov:
                    best, best_ov = b, ov
            if best is not None and best_ov > 0.2:
                keyed.append((best.order - 0.5, z))
            else:
                prior = [b.order for b in blocks if b.bbox[1] <= z.bbox[1]]
                keyed.append(((max(prior) if prior else 0) + 0.25, z))
        keyed.sort(key=lambda kz: (0 if kz[1].tag in page_marker_tags else 1, kz[0]))
        for i, (_k, z) in enumerate(keyed, start=1):
            if z.parent_id is None:
                z.serial = i
        self.zm.renumber_reading_order(page)

    def auto_tag_page(self, page: int, force: bool = False) -> PageResult:
        """Re-tags EXISTING zones on a page from the analysis (no zones are
        created or deleted). Protected zones are skipped unless `force`;
        locked zones are never retagged."""
        result = PageResult(page=page)
        try:
            layout, decisions = self.decide(page)
        except Exception as e:  # noqa: BLE001
            result.error = f"{type(e).__name__}: {e}"
            return result
        result.decisions = decisions
        self.zm.begin_batch()
        try:
            for z in self.zm.zones_on_page(page):
                if z.locked or (z.protected and not force) or z.parent_id is not None:
                    continue
                best, best_ov = None, 0.0
                for dec in decisions:
                    ov = _overlap(z.bbox, dec.block.bbox)
                    if ov > best_ov:
                        best, best_ov = dec, ov
                if best is None or best.option is None or best_ov < 0.5:
                    continue
                attrs = best.zone_attributes()
                if best.option.tag == z.tag and attrs.get("cup_name") == z.attributes.get("cup_name"):
                    z.attributes.update({k: v for k, v in attrs.items() if k not in z.attributes or k in (
                        "confidence", "confidence_breakdown", "auto_role", "auto_evidence", "auto_alternatives",
                        "needs_review", "review_reasons", "auto_engine")})
                    if not best.needs_review:
                        z.attributes.pop("needs_review", None)
                        z.attributes.pop("review_reasons", None)
                    continue
                if self.zm.apply_auto_tag(z.zone_id, best.option.tag, attrs, force=force):
                    result.retagged += 1
                    if best.needs_review:
                        result.needs_review += 1
        finally:
            self.zm.end_batch()
        return result

    # ------------------------------------------------------- document
    def compute_document(self, pages, cancel_event=None, progress=None, state: DocumentRunState = None):
        """Generator for a worker thread: yields (page, layout, decisions)
        for every page not already completed in `state`. Pure analysis -
        the caller applies results on the GUI thread (auto_zone_page with
        decisions=...)."""
        state = state or DocumentRunState(fingerprint=self.fingerprint)
        self.context(whole_document=True)
        todo = [p for p in pages if p not in set(state.completed)]
        total = len(todo)
        for i, page in enumerate(todo, start=1):
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                layout, decisions = self.decide(page)
                yield page, layout, decisions, None
            except Exception as e:  # noqa: BLE001
                yield page, None, None, f"{type(e).__name__}: {e}"
            if progress:
                progress(i, total, page)


def record_manual_retag(settings: dict, zone, old_tag, old_attrs):
    """Auto Tag learning: the user replaced an automatic decision - remember
    role -> chosen tag so future decisions for that role prefer it."""
    role = old_attrs.get("auto_role")
    if not role:
        return
    label = zone.attributes.get("tag_label") or zone.attributes.get("cup_name") or zone.tag
    learning = settings.setdefault("auto_tag_learning", {})
    per_role = learning.setdefault(role, {})
    per_role[label] = per_role.get(label, 0) + 1
