# """EPUB-profile intermediate-XML generator.

# Reuses the SAME zoning/reading-order/hierarchy engine as core/xml_generator.py
# (no second zoning engine - core.reading_order/core.hierarchy, the exact
# modules the XML profile's generator already relies on) but emits Mapping.xml's
# OWN element vocabulary directly: for the EPUB profile, zone.tag literally IS
# the Mapping.xml element name (see profiles/epub_profile.json) - "uncapfig",
# "h1", "para", "eqn_img", etc - so most of this generator's per-zone job is
# just "extract this zone's text/image and wrap it in an element named
# zone.tag", not a second tag-translation table.

# Heading/section NESTING (h1..h6) is built here via core.hierarchy, NOT left
# to Mapping.xml's coverAbove rule as an earlier version of this file assumed -
# real production output (confirmed across two different books) keeps h1/h2/
# etc as literal <h1>/<h2> elements wrapped in a <section aria-labelledby="...">
# container, never renamed to a <sec1>/<sec2> custom element the way a literal
# reading of Mapping.xml's coverAbove rule would produce. hierarchy.py's own
# tree-building (already tested for the XML profile) does exactly this kind of
# "heading absorbs following same-or-lower-level content" nesting already, so
# reusing it directly is both more reliable and more faithful to the confirmed
# real output than reimplementing the same nesting logic as a mapping rule.

# A few other constructs (footnote/endnote collection, plain/unlabeled
# bibliography references, flat index entries) are ALSO built directly here
# rather than through Mapping.xml, because real output either contradicts the
# one Mapping.xml rule that looked relevant (footnotes: confirmed typo
# "epub_types" vs real "epub:type", plus a different wrapper shape entirely)
# or isn't covered by any rule in the Mapping.xml on hand at all (plain
# references, flat index entries) - see the docstrings below for exactly what
# was verified against real samples vs inferred by symmetry.

# The output of generate() is the "EPUB XML/intermediate structure" step of
# the spec's pipeline (PDF -> zoning -> hierarchy -> reading order -> EPUB
# intermediate XML -> Mapping.xml transformation -> XHTML) - NOT the final
# XHTML. core/mapping_engine.py performs the next step for everything this
# generator does NOT already hardcode.
# """
# import re

# from lxml import etree

# from core import reading_order, hierarchy, text_extractor, table_extractor
# from core.image_extractor import AssetManager
# from core.xml_generator import _parse_inline, normalize_title_text

# # Traceability: the source zone_id, stamped on every emitted element so
# # core/mapping_engine.py can report "source zone X, page Y" in error
# # messages (spec section 25) without a second parallel bookkeeping
# # structure. Stripped back out just before final XHTML serialization.
# SRC_ATTR = "data-zt-src"

# _ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.\-]")


# def _sanitize_id_component(value: str) -> str:
#     """Replaces any character not valid in an XML id with "-", preserving
#     the semantic value (e.g. "ix" stays "ix", " 1 " stripped to "1") -
#     used for CUPEPUB PageNum ids, which embed the zone's own printed page
#     label directly rather than a synthetic counter."""
#     return _ID_UNSAFE_RE.sub("-", value)


# _DIGIT_GAP_RE = re.compile(r"(?<=\d)\s+(?=\d)")


# def _close_digit_gap(text: str) -> str:
#     """Closes a stray whitespace gap sitting directly BETWEEN two digits
#     (e.g. a genuine printed page number "208" that PDF text extraction
#     reported as "20 8" - confirmed real symptom: font kerning/spacing on
#     some page-number glyphs can read as a word gap even though it's one
#     single number) - a PageNum zone's content is structurally always a
#     single short page label (never legitimate multi-word text), so
#     collapsing ONLY a digit-to-digit gap here is safe and narrowly scoped;
#     it never touches a gap next to a non-digit character (a genuine
#     Roman-numeral/prefixed label like "p. 20" or "iv" is untouched)."""
#     return _DIGIT_GAP_RE.sub("", text)

# # zone.attributes keys that are internal/UI bookkeeping, never copied onto
# # the emitted intermediate element as a literal XML attribute.
# # "cup_name" is CUPEPUB-only (core/cup_config.py stamps every zone with its
# # original CUP tag name, e.g. "PageNum", for core/cup_validation.py's
# # mandatory-zone check) - never set by the XML/EPUB profiles, so this is a
# # pure addition for them.
# _INTERNAL_ATTR_KEYS = {
#     "asset_kind", "img_type", "merged_formatted_text", "hyphen_keep_boundaries",
#     "hyphen_keep_chain_boundary",
#     "source", "list_type", "cup_name", "merge_join", "manual_text",
#     # Real, confirmed bug: a zone whose tag isn't text-combinable (e.g.
#     # List/Figure/Table - see core/hierarchy.py's push_single_zone) still
#     # gets its own full zone.attributes dict copied onto its output
#     # element via this same generic filter - merged_with_previous/
#     # merge_target are ZoneTool's own internal Merge Previous bookkeeping
#     # (core/zone_manager.py's merge_with_previous) and must never leak
#     # into generated XHTML as a literal (invalid) element attribute.
#     "merged_with_previous", "merge_target",
#     # auto_zoning/index_auto_zone.py's own bookkeeping (index hierarchy
#     # LEVEL and whether an entry was reconstructed from a wrapped
#     # continuation line) - internal to detection/preview, never a literal
#     # output attribute (same bug class as merge_target above).
#     "index_level", "index_wrapped",
# }


# class EpubXmlGenerator:
#     # h1's own @class, chosen by the active component_type - verified
#     # against real fmtitle/chaptitle/bibtitle samples; anything not listed
#     # falls back to "fmtitle" (the most common case: most frontmatter/
#     # backmatter component types share it).
#     _H1_CLASS_BY_COMPONENT = {
#         "chapter": "chaptitle", "part": "chaptitle", "section": "chaptitle",
#         "bibliography": "bibtitle", "index": "indtitle", "glossary": "glosstitle",
#     }

#     # Tags whose content is collected and moved to ONE trailing section at
#     # the end of the document, in reading order, rather than left in their
#     # natural position - reverse-engineered from real production output
#     # (every footnote in a chapter file ends up together in a single
#     # <section class="footnotes" epub:type="footnotes"><ol class="decimal">
#     # <li class="fn" id="{prefix}_fn{N}" epub:type="footnote">...</li>...
#     # </ol></section> at the very end of that file). Mapping.xml's own fn/en
#     # rules are bypassed for these tags (consumed here before Mapping.xml
#     # ever runs) - its <sec1 epub_types="footnotes"> rule has a confirmed
#     # typo (epub_types, not epub_type) and a different wrapper shape than
#     # the verified real output. "en" (endnotes) is inferred by direct
#     # symmetry with the verified "fn" case - not independently confirmed.
#     #
#     # 4th element (ref_token) is the reference-marker id prefix ("xfn"/
#     # "xen") - a footnote/endnote zone now produces BOTH an inline
#     # reference marker (<sup><a id="{prefix}_xfn{n}" href="#{prefix}_fn
#     # {n}">{n}</a></sup>, left at the zone's own reading-order position)
#     # and the collected target <li> (id="{prefix}_fn{n}", with a
#     # role="doc-backlink" <a> pointing back to the reference) - see
#     # _gen_footnote_zone. Previously the WHOLE zone was diverted straight
#     # into the end-of-document section via _place()'s tag-based routing,
#     # leaving literally no trace at the reference's real position - a
#     # confirmed bug (spec: footnote references must never be silently
#     # dropped from their original location).
#     _NOTE_TAGS = {"fn": ("footnotes", "footnote", "fn", "xfn"), "en": ("endnotes", "endnote", "en", "xen")}
#     # A footnote/endnote zone's own extracted text usually already begins
#     # with the source's own printed number ("1 James Tully..."), which
#     # would otherwise duplicate visually next to the generated backlink's
#     # own number (e.g. "11 James Tully..." - Part 13's reported bug).
#     _LEADING_NUM_RE = re.compile(r"^\s*(\d+)[.)]?\s+")

#     # Index Primary/Secondary/Territory: a REAL nested list (<ul><li>term
#     # <ul><li>sub-term <ul><li>sub-sub-term</li></ul></li></ul></li></ul>),
#     # not the <sec2>/<sec3>/<sec4> wrapping Mapping.xml's own indexprimary/
#     # indexsecondary/indexterritory rules literally specify - confirmed
#     # wrong by inspecting actual output (a real hierarchical index nests
#     # sub-terms under their parent term via nested lists, matching the
#     # verified flat "Index Entry" <ul>/<li> shape one level at a time, not
#     # sibling <sec>-wrapped runs). Collected here (bypassing those Mapping.xml
#     # rules entirely, same precedent as footnotes/plain-refs/flat index
#     # entries above) and assembled into one tree in generate() from the
#     # flat, reading-order-collected (level, zone) list - see
#     # _build_index_hierarchy.
#     _INDEX_HIERARCHY_LEVELS = {"indexprimary": 1, "indexsecondary": 2, "indexterritory": 3}

#     def __init__(self, zone_manager, pdf_document, assets_dir: str, prefix: str, profile: dict,
#                  component_type: str = None, jpeg_quality: int = 95, image_dpi: int = None,
#                  remove_image_background: bool = False, image_prefix: str = None):
#         self.zm = zone_manager
#         self.pdf = pdf_document
#         self.prefix = prefix
#         self.profile = profile
#         self.component_type = component_type or profile.get("default_component_type", "chapter")
#         self.image_kinds = profile.get("image_kinds", {})
#         # CUPEPUB-only (both None/True for XML/EPUB, which have neither
#         # concept - see core/cup_config.py): the CUP tag that represents a
#         # physical page-number marker, and whether pagebreaks should
#         # instead come automatically from every PDF page boundary (EPUB's
#         # existing, unchanged behavior - see _maybe_page_break).
#         self.pagenum_cup_name = profile.get("cup_pagenum_name")
#         self.auto_pagebreak = profile.get("auto_pagebreak", True)
#         # CUPEPUB-only: [start_tag, end_tag] output-tag pair for Box_Start/
#         # Box_End (e.g. ["box_start", "box_end"]) - None for XML/EPUB,
#         # which have no such concept (XML's own boxed-text-start/-end is
#         # handled unconditionally inside core/hierarchy.py itself, not via
#         # this profile-supplied pair). See _gen_node's "boxed-text" branch.
#         self.box_container_tags = profile.get("box_container_tags")
#         assets_kwargs = {} if image_dpi is None else {"dpi": image_dpi}
#         self.assets = AssetManager(assets_dir, prefix, jpeg_quality, remove_background=remove_image_background,
#                                     image_prefix=image_prefix, **assets_kwargs)
#         # Populated during generate() - non-fatal issues (e.g. an image
#         # zone whose asset_kind has no image_kinds config) surfaced to the
#         # caller for a warning dialog, never silently dropped without trace.
#         self.warnings: list[str] = []
#         # Per-generate() run state (reset in generate()). _fig_counter/
#         # _tbl_counter/_eq_counter/_heading_counters are also read/
#         # incremented by assign_missing_ids (called AFTER generate()+
#         # Mapping.xml, on this same instance) so a fresh ID assigned there
#         # can never collide with one assigned during generate() itself.
#         self._last_page = None
#         self._sec_counter = 0
#         self._ref_counter = 0
#         self._index_counter = 0
#         self._fig_counter = 0
#         self._tbl_counter = 0
#         self._table_draw_counter = 0  # semantic <table> zones (tag "table") - separate from
#         # _tbl_counter above, which numbers the UNRELATED image-crop "Table" (tblimage) asset type;
#         # sharing one counter between the two would risk colliding ids if a document has both kinds.
#         self._eq_counter = 0
#         self._heading_counters = {}
#         self._h1_seen = False
#         self._notes = {}
#         # Per-tag ("fn"/"en") sequential counters for footnote/endnote
#         # reference<->target numbering (spec: sequential, deterministic,
#         # never random/coordinate-based). NEVER reset mid-document (see
#         # generate()'s own comment) - the existing xen citation-marker
#         # mechanism (_gen_endnote_marker) computes its href purely from a
#         # marker's own raw source digit plus this same running counter, so
#         # target_id/ref_id must stay exactly this single global sequence
#         # or already-working marker<->content linking breaks.
#         self._note_counters = {}
#         self._merge_continuation_of = {}
#         # {base_pagenum_id: how many times it's been used so far} - a
#         # printed page label repeating within one document (spec 98.12/
#         # this fix's section 12) gets a deterministic "_2"/"_3" suffix
#         # instead of a silently duplicated id; never resolved by
#         # substituting the physical PDF page number.
#         self._pagenum_seen = {}
#         self._pending_header = None
#         self._index_hierarchy_items = []
#         # tag->level lookup for _gen_zone's index-hierarchy branch below -
#         # profile-driven (spec: "read the active profile / Mapping.xml...
#         # this allows future client profiles to use different tags"), never
#         # hardcoded as the only implementation. profile["index_hierarchy_
#         # tags"] is always level->tag (see core/profile_manager.py, core/
#         # cup_config.py) - inverted here to the tag->level direction this
#         # generator actually looks zones up by. Falls back to the class
#         # default (unchanged behavior) for a profile that doesn't declare
#         # its own mapping at all (e.g. an older cached profile dict).
#         level_to_tag = profile.get("index_hierarchy_tags")
#         self._index_tag_to_level = ({tag: int(level) for level, tag in level_to_tag.items()}
#                                      if level_to_tag else dict(self._INDEX_HIERARCHY_LEVELS))

#     def _hyphen_keep_at(self, zone):
#         """Hyphenated Line-Break Text Normalization (spec: "Hyphenated
#         Line-Break Normalization Before XHTML Generation") - the user's
#         post-zoning Hyphen Normalization Review overrides for zone's own
#         text (core.text_extractor.find_all_hyphen_candidates / gui.
#         dialogs.HyphenReviewDialog), a set of boundary indices (see
#         text_extractor.dehyphenate_join) where a detected line-break
#         hyphen must be KEPT rather than auto-joined. Identical to core.
#         xml_generator.XMLGenerator's own _hyphen_keep_at (never a second,
#         divergent implementation) - empty by default (no review
#         performed, or every candidate left checked), reproducing the
#         prior automatic-join behavior exactly for any project that never
#         opens the review window.

#         Confirmed as a real, pre-existing gap: every text-extraction call
#         site in this class previously passed a hardcoded set() instead of
#         this, meaning the Hyphen Normalization Review's own choices were
#         silently ignored for XHTML/CUPEPUB generation even though the
#         SAME review already worked correctly for Generate XML - this
#         method (and its call sites) close that gap, not just add the new
#         review-trigger wiring in gui/main_window.py."""
#         return set(zone.attributes.get("hyphen_keep_boundaries", []))

#     def generate(self):
#         """Returns an lxml Element: <component type="...">. Reuses
#         core.hierarchy exactly like the XML profile's XMLGenerator does, so
#         heading nesting/split-flattening/reading order all come from the
#         same tested engine - just walked into a different (EPUB) element
#         vocabulary instead of BITS."""
#         self._last_page = None
#         self._sec_counter = 0
#         self._ref_counter = 0
#         self._index_counter = 0
#         self._fig_counter = 0
#         self._tbl_counter = 0
#         self._table_draw_counter = 0  # semantic <table> zones (tag "table") - separate from
#         # _tbl_counter above, which numbers the UNRELATED image-crop "Table" (tblimage) asset type;
#         # sharing one counter between the two would risk colliding ids if a document has both kinds.
#         self._eq_counter = 0
#         self._heading_counters = {}
#         self._h1_seen = False
#         self._notes = {tag: [] for tag in self._NOTE_TAGS}
#         self._note_counters = {tag: 0 for tag in self._NOTE_TAGS}
#         # target_zone_id -> the zone_id that explicitly Merge-Previous'd
#         # onto it (attributes["merged_with_previous"]/["merge_target"]).
#         # Built once here so _note_continuation_chain can find a
#         # continuation regardless of whether the target is a plain top-
#         # level zone (already handled correctly by core.paragraph_merge's
#         # own top-level chain splicing - never double-processed here,
#         # since a zone merge_top_level_stream already absorbed into a
#         # chain is never independently passed to _gen_footnote_zone at
#         # all) or a SPLIT-PIECE CHILD, which core.reading_order.
#         # flatten_document_order deliberately never includes as a
#         # top-level stream entry (see _order_key's own docstring) - so
#         # paragraph_merge.py can only defer that case here, never resolve
#         # it itself.
#         self._merge_continuation_of = {
#             z.attributes["merge_target"]: z.zone_id
#             for z in self.zm.zones.values()
#             if z.attributes.get("merged_with_previous") and z.attributes.get("merge_target")
#         }
#         self._pagenum_seen = {}
#         self._pending_header = None
#         self._index_hierarchy_items = []

#         page_order = reading_order.compute_page_order(self.zm)
#         extra_markers = [tuple(self.box_container_tags)] if self.box_container_tags else None
#         doc_tree, tree_issues = hierarchy.build_document_tree(
#             self.zm, page_order, extra_markers,
#             page_marker_tags=self.profile.get("page_marker_tags"),
#             footnote_flow_tags=self.profile.get("footnote_flow_tags"),
#             non_flow_tags=self.profile.get("non_flow_tags"))
#         # Previously discarded entirely (a real "boundary_issues" report -
#         # e.g. an unmatched Box_Start/Box_End - never reached the user via
#         # this path); now surfaced as ordinary generation warnings, never
#         # blocking (core/cup_validation.py's own pre-generation pairing
#         # check is still what actually blocks Generate XHTML for CUPEPUB).
#         self.warnings.extend(tree_issues)

#         root = etree.Element("component", type=self.component_type)
#         for node in doc_tree["children"]:
#             # A "Subtitle" zone immediately following the document's own
#             # title absorbs into the SAME <header> as a second <h1
#             # epub_type="subtitle">, instead of becoming loose sibling
#             # content - verified against a real sample (maintitle +
#             # subtitle both inside one <header>). Only fires while a
#             # header is still "open" (i.e. nothing else has appeared yet).
#             if self._pending_header is not None and node["type"] == "zone":
#                 zone = self.zm.zones[node["zone_id"]]
#                 if zone.tag == "subtitle":
#                     for pb in self._maybe_page_break(zone.page):
#                         root.append(pb)
#                     self._absorb_subtitle(zone)
#                     continue
#             self._pending_header = None
#             for el in self._gen_node(node):
#                 self._place(root, el)

#         # "en" (endnotes) only - see _gen_footnote_zone's own docstring:
#         # each top-level "en" zone's <li> was already placed INLINE, as a
#         # "__note_li__" placeholder, at its own reading-order position
#         # during the loop above (never accumulated into self._notes["en"],
#         # which stays empty and is a no-op in the loop just below). This
#         # groups consecutive runs of those placeholders (spec Part 10) and
#         # assigns each group's own local numbering now that every "en"
#         # zone in the document has actually been visited.
#         self._wrap_note_groups(root, "en")

#         for tag, (section_class, item_epub_type, id_token, ref_token) in self._NOTE_TAGS.items():
#             if self._notes[tag]:
#                 root.append(self._build_note_section(self._notes[tag], section_class, item_epub_type))

#         if self._index_hierarchy_items:
#             root.append(self._build_index_hierarchy(self._index_hierarchy_items))

#         self._wrap_consecutive(root, lambda e: e.tag == "__ref_li__", "ol",
#                                 {"class": "none"}, retag="li")
#         self._wrap_consecutive(root, lambda e: e.tag == "__index_li__", "ul",
#                                 {"class": "none", "epub_type": "index-entry-list"}, retag="li")
#         return root

#     def _place(self, container, el):
#         """Appends el to container in its natural reading-order position.
#         A footnote/endnote zone no longer routes its WHOLE element into the
#         _notes bucket here - _gen_footnote_zone already splits it into an
#         inline reference (returned normally, placed right here like any
#         other element) and a separately-collected target <li> (appended
#         directly to self._notes at build time), so nothing arriving at
#         this method is ever itself an "fn"/"en" tagged element anymore."""
#         container.append(el)

#     # ------------------------------------------------------------- doc_tree walk
#     def _maybe_page_break(self, page) -> list:
#         # CUPEPUB has its own explicit page-number marker tag (see
#         # _gen_pagenum_zone) - a pagebreak must ONLY ever come from an
#         # actual PageNum zone for it, never automatically from crossing a
#         # PDF page boundary (spec 98.10-98.14). EPUB (auto_pagebreak
#         # defaults True - it has no such tag) keeps this exact original
#         # behavior, unchanged.
#         if not self.auto_pagebreak:
#             return []
#         if page != self._last_page:
#             self._last_page = page
#             return [self._page_break_span(page)]
#         return []

#     def _gen_node(self, node) -> list:
#         """Mirrors core.xml_generator.XMLGenerator's own doc_tree node
#         dispatch (_gen_node/_gen_sec/_gen_merged_p), just building EPUB
#         elements instead of BITS ones. Returns a list (0, 1, or more
#         elements) so a heading can return [pagebreak?, header-or-section]
#         and a merge/boxed-text node can return several independent
#         elements (see their branches below for why)."""
#         ntype = node["type"]
#         if ntype == "sec":
#             return self._gen_sec_node(node)
#         if ntype == "zone":
#             zone = self.zm.zones[node["zone_id"]]
#             out = self._maybe_page_break(zone.page)
#             if zone.tag == "title" and not self._h1_seen:
#                 # A "Title" zone (as opposed to "Heading 1") is how the XML
#                 # profile's zoning habit tags a document/chapter title -
#                 # confirmed from a real project. core.hierarchy doesn't
#                 # recognize "title" as a heading tag (core.constants.
#                 # HEADING_TAGS is h1..h6 only, shared/untouched code), so it
#                 # never reaches _gen_sec_node on its own; the FIRST such
#                 # zone gets the exact same <header><h1 epub_type="title">
#                 # treatment as a real h1 would, so it can never end up as a
#                 # bare, invalid <title> element loose in body content
#                 # (which collides with <head>/<title> and isn't valid
#                 # anywhere else in XHTML).
#                 page = self.pdf.get_page(zone.page)
#                 content = normalize_title_text(
#                     text_extractor.extract_zone_formatted_text_with_breaks(page, zone, self._hyphen_keep_at(zone)))
#                 out.append(self._build_document_header(content, zone.zone_id))
#             else:
#                 out.extend(self._resolve_zone_to_elements(zone))
#             return out
#         if ntype == "merged_p":
#             # hierarchy.py only ever builds a "merged_p" node when the
#             # chain's tag IS in TEXT_MERGE_TAGS (core/constants.py) - never
#             # reached for EPUB's own tag choices (e.g. "para"), but IS
#             # reached for CUPEPUB's "Para" (CUPLookup translates it to the
#             # literal string "p", which happens to match). An explicit
#             # Merge Previous must actually COMBINE the chain's text into
#             # ONE element (spec 98.16-98.21) - previously this emitted
#             # every zone in the chain independently regardless, which is
#             # why Merge Previous appeared to do nothing to the generated
#             # output.
#             zone_ids = [zid for zid in node["zone_ids"] if zid in self.zm.zones]
#             if not zone_ids:
#                 return []
#             out = self._maybe_page_break(self.zm.zones[zone_ids[0]].page)
#             first_tag = self.zm.zones[zone_ids[0]].tag
#             if first_tag in self._NOTE_TAGS:
#                 # A footnote/endnote split across a page break (spec
#                 # sections 23/24/56: "Patterns of Con-" continuing as
#                 # "tention, Rodriguez..." must stay footnote 1, never
#                 # become a new footnote 5) - combined via its own
#                 # dedicated renderer (ONE counter increment, ONE <li>
#                 # target for the whole chain), never the generic
#                 # _gen_merged_text_zone (which has no notion of a
#                 # footnote's own numbered target/backlink structure).
#                 merged_footnote_el = self._gen_merged_footnote_zone(zone_ids, first_tag)
#                 if merged_footnote_el is not None:
#                     out.append(merged_footnote_el)
#             else:
#                 out.append(self._gen_merged_text_zone(zone_ids))
#             return out
#         if ntype == "boxed-text":
#             start_zone = self.zm.zones.get(node["start_zone_id"])
#             if (start_zone is not None and self.box_container_tags
#                     and start_zone.tag == self.box_container_tags[0]):
#                 return self._gen_box_node(node, start_zone)
#             # Default (XML profile's own boxed-text-start/-end): the start
#             # and end MARKERS are themselves emitted as ordinary sibling
#             # elements (unchanged) flanking their children - never wrapped
#             # into one container element. Untouched by this fix.
#             out = []
#             out.extend(self._maybe_page_break(start_zone.page))
#             out.extend(self._resolve_zone_to_elements(start_zone))
#             for child in node["children"]:
#                 out.extend(self._gen_node(child))
#             end_zone = self.zm.zones.get(node.get("end_zone_id"))
#             if end_zone is not None:
#                 out.extend(self._resolve_zone_to_elements(end_zone))
#             return out
#         return []

#     def _gen_box_node(self, node, start_zone):
#         """CUPEPUB's Box_Start/Box_End (spec: "structural zoning markers
#         only - MUST NOT appear as final XHTML elements"): builds ONE real
#         <box> element from the Start zone's own attributes (e.g. a
#         configured "type"), with every zone between Start and End
#         (core/hierarchy.py's existing reading-order/stack-based grouping -
#         see build_document_tree's extra_container_markers) recursively
#         generated as ITS CHILDREN. Neither Start nor End zone is ever
#         itself resolved into an element - _resolve_zone_to_elements is
#         deliberately never called for them here, unlike the default
#         boxed-text branch above. Mapping.xml's own //box[child::boxtitle]
#         rule (which needs exactly this shape: a real <box> containing a
#         <boxtitle> child) then transforms it - no hardcoded final XHTML
#         structure is built here, only the correct INTERMEDIATE tree."""
#         box_el = etree.Element("box")
#         box_el.set(SRC_ATTR, start_zone.zone_id)
#         for key, value in start_zone.attributes.items():
#             if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
#                 continue
#             box_el.set(key, value)
#         out = self._maybe_page_break(start_zone.page)
#         for child in node["children"]:
#             for child_el in self._gen_node(child):
#                 box_el.append(child_el)
#         out.append(box_el)
#         return out

#     def _gen_sec_node(self, node) -> list:
#         """A heading (h1..h6). Verified against real production output
#         (two different books): NOT renamed to a <sec1>/<sec2> element - the
#         heading survives as a literal <h{level}>, and the nesting instead
#         comes from a wrapper:
#           - the document's own FIRST h1 -> <header><h1 id="{prefix}_1"
#             class="{...per component_type...}" epub_type="title">...</h1>
#             </header>, matching every real chapter/frontmatter/backmatter
#             title sample seen.
#           - every other heading (h2..h6, or a later stray h1) ->
#             <section aria-labelledby="{id}"><h{level} id="{id}">...</h{level}>
#             ...absorbed children...</section>, matching every real
#             sub-section sample seen (e.g. NOTES/WORKS CITED sections)."""
#         zone_ids = node.get("merged_zone_ids") or [node["zone_id"]]
#         first_zone = self.zm.zones[zone_ids[0]]
#         level = node["level"]
#         out = self._maybe_page_break(first_zone.page)

#         heading_parts = []
#         for zid in zone_ids:
#             z = self.zm.zones[zid]
#             page = self.pdf.get_page(z.page)
#             content = normalize_title_text(
#                 text_extractor.extract_zone_formatted_text_with_breaks(page, z, self._hyphen_keep_at(z)))
#             if content:
#                 heading_parts.append(content)
#         heading_content = "<break/>".join(heading_parts)
#         tag = f"h{level}"
#         h_el = _parse_inline(f"<{tag}>{heading_content}</{tag}>") if heading_content else etree.Element(tag)
#         h_el.set(SRC_ATTR, first_zone.zone_id)

#         children_out = []
#         for child in node["children"]:
#             children_out.extend(self._gen_node(child))

#         is_document_title = level == 1 and not self._h1_seen
#         if is_document_title:
#             self._h1_seen = True
#             h_el.set("id", f"{self.prefix}_1")
#             h_el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
#             h_el.set("epub_type", "title")
#             header = etree.Element("header")
#             header.append(h_el)
#             self._pending_header = header
#             out.append(header)
#             for c in children_out:
#                 self._place_into(out, c)
#         else:
#             self._sec_counter += 1
#             sec_id = f"{self.prefix}_sec{self._sec_counter}"
#             h_el.set("id", sec_id)
#             section = etree.Element("section", **{"aria-labelledby": sec_id})
#             section.append(h_el)
#             for c in children_out:
#                 section.append(c)
#             out.append(section)
#         return out

#     @staticmethod
#     def _place_into(out_list, el):
#         out_list.append(el)

#     def _build_document_header(self, content, src_zone_id):
#         """Shared by both routes into "this zone IS the document/chapter
#         title": a real h1 zone (_gen_sec_node) and a "Title"-tagged zone
#         (_gen_node's "zone" branch, above) - same <header><h1 id=
#         "{prefix}_1" class="..." epub_type="title">...</h1></header> shape
#         either way, and either way self._pending_header is left set so a
#         following "Subtitle" zone can still absorb into it (see generate())."""
#         h_el = _parse_inline(f"<h1>{content}</h1>") if content else etree.Element("h1")
#         h_el.set(SRC_ATTR, src_zone_id)
#         h_el.set("id", f"{self.prefix}_1")
#         h_el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
#         h_el.set("epub_type", "title")
#         header = etree.Element("header")
#         header.append(h_el)
#         self._h1_seen = True
#         self._pending_header = header
#         return header

#     def _absorb_subtitle(self, zone):
#         page = self.pdf.get_page(zone.page)
#         content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#         h_el = _parse_inline(f"<h1>{content}</h1>") if content else etree.Element("h1")
#         h_el.set(SRC_ATTR, zone.zone_id)
#         h_el.set("epub_type", "subtitle")
#         h_el.set("role", "doc-subtitle")
#         self._pending_header.append(h_el)

#     def _build_note_section(self, elements, section_class, item_epub_type):
#         """elements are already-complete <li> targets (id, backlink and all
#         - built by _gen_footnote_zone at the reference's own reading-order
#         position) - this only wraps them in the collected end-of-document
#         <section><ol> container. The section's own id is left for
#         assign_missing_ids (main_window.generate_xhtml) to assign, same as
#         every other bare <section> this generator produces."""
#         section = etree.Element("section", **{"class": section_class, "epub_type": section_class})
#         ol = etree.SubElement(section, "ol", **{"class": "decimal"})
#         for li in elements:
#             ol.append(li)
#         return section

#     def _wrap_note_groups(self, root, tag):
#         """Groups consecutive runs of "__note_li__" placeholders (built by
#         _gen_footnote_zone/_gen_merged_footnote_zone for tag, currently
#         only ever "en") into separate <section class="endnotes"><ol>
#         groups (spec Part 10 - "endnote group structure"), each
#         independently numbered from 1. Checked under every element in the
#         tree (not just root's own direct children - mirrors
#         _wrap_consecutive's own reasoning), because whether a run of
#         top-level "en" zones ends up as root's own direct children or
#         nested one level inside a real heading's own <section> wrapper
#         depends entirely on whether that heading's tag is one
#         core.hierarchy recognizes (h1-h6) - a project whose own subsection
#         headings use a different tag (e.g. a dedicated "EnHead"/"enhead"
#         tag, confirmed from a real production sample) never gets that
#         wrapper at all, and its "en" zones stay flat siblings of root
#         instead. Both shapes must group correctly, without assuming either
#         one.

#         A run tolerates page-break <span role="doc-pagebreak"> markers
#         WITHOUT treating them as a group boundary (spec Part 10.4.1:
#         "an endnote group may continue across multiple PDF pages... do
#         NOT split a group merely because the page number changes") - they
#         are preserved, just relocated to sit immediately before the
#         group's own <ol> instead of interior to it (a <span> can never be
#         a valid direct child of <ol> - only <li> can). Only a REAL other
#         content element - a heading (whatever its own tag), a paragraph,
#         anything that is not this tag's own note or a bare pagebreak
#         marker - ends a run and starts a new logical group.

#         This groups PURELY by structural adjacency in the already-correct
#         reading-order stream doc_tree/generate() produced - never by
#         page number, zone id, or object creation order (spec Part 10.4/
#         14/18) - so it works identically regardless of which raw tag a
#         given project uses for its own endnote-group headings."""

#         def is_note(el):
#             return el.tag == "__note_li__" and el.get("_note_tag") == tag

#         def is_pagebreak(el):
#             return el.tag == "span" and el.get("role") == "doc-pagebreak"

#         section_class, item_epub_type, _id_token, _ref_token = self._NOTE_TAGS[tag]
#         for parent in list(root.iter()):
#             children = list(parent)
#             i = 0
#             while i < len(children):
#                 if not is_note(children[i]):
#                     i += 1
#                     continue
#                 run = [children[i]]
#                 interior_pagebreaks = []
#                 j = i + 1
#                 while j < len(children):
#                     if is_note(children[j]):
#                         run.append(children[j])
#                         j += 1
#                     elif is_pagebreak(children[j]) and j + 1 < len(children) and is_note(children[j + 1]):
#                         interior_pagebreaks.append(children[j])
#                         j += 1
#                     else:
#                         break

#                 # Local numbering + duplicate-visible-number stripping, now
#                 # that this group's own membership/order is finally known.
#                 for local_n, li in enumerate(run, start=1):
#                     backlink = li.find(".//a[@role='doc-backlink']")
#                     if backlink is not None:
#                         backlink.text = str(local_n)
#                     backlink_sup = backlink.getparent() if backlink is not None else None
#                     if backlink_sup is not None and backlink_sup.tail:
#                         backlink_sup.tail = self._strip_leading_source_number(backlink_sup.tail, local_n)

#                 idx = children.index(run[0])
#                 for pb in interior_pagebreaks:
#                     parent.remove(pb)
#                 for el in run:
#                     parent.remove(el)
#                     el.tag = "li"
#                     el.attrib.pop("_note_tag", None)
#                 section = self._build_note_section(run, section_class, item_epub_type)

#                 insert_at = idx
#                 for pb in interior_pagebreaks:
#                     parent.insert(insert_at, pb)
#                     insert_at += 1
#                 parent.insert(insert_at, section)

#                 children = list(parent)
#                 i = insert_at + 1

#     def _build_index_hierarchy(self, items):
#         """Builds ONE real nested list from the flat, reading-order (level,
#         zone) list collected in _gen_zone: level 1 (Index Primary) terms are
#         top-level <li>, level 2 (Index Secondary) nests as a new <ul> INSIDE
#         the most recent level-1 <li>, level 3 (Index Territory) nests the
#         same way inside the most recent level-2 <li>. A level-2/3 term
#         encountered with no open parent at the right level (e.g. Territory
#         right after Primary, no Secondary in between) degrades gracefully by
#         attaching to whatever's currently open, never raises/crashes."""
#         root_ul = etree.Element("ul", **{"class": "none", "epub_type": "index-entry-list"})
#         stack = []  # [(level, li_element)], innermost open term last
#         for level, zone in items:
#             page = self.pdf.get_page(zone.page)
#             content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#             self._index_counter += 1
#             li = etree.Element("li", **{"epub_type": "index-entry", "id": f"{self.prefix}_ind{self._index_counter}"})
#             li.set(SRC_ATTR, zone.zone_id)
#             if content:
#                 inline = _parse_inline(f"<x>{content}</x>")
#                 li.text = inline.text
#                 for c in inline:
#                     li.append(c)

#             while stack and stack[-1][0] >= level:
#                 stack.pop()
#             if not stack:
#                 root_ul.append(li)
#             else:
#                 parent_li = stack[-1][1]
#                 nested_ul = parent_li.find("ul")
#                 if nested_ul is None:
#                     nested_ul = etree.SubElement(parent_li, "ul")
#                 nested_ul.append(li)
#             stack.append((level, li))
#         return root_ul

#     @staticmethod
#     def _wrap_consecutive(root, predicate, wrapper_tag, wrapper_attrs, retag=None):
#         """Wraps every maximal run of CONSECUTIVE SIBLING children matching
#         predicate() in one new wrapper element, in place - checked under
#         every element in the tree (not just root's own direct children),
#         since a "ref"/"indexentry" zone could in principle end up nested
#         inside a heading section rather than at the document's top level.
#         Used for the "ref"/"indexentry" placeholder tags below (built as
#         __ref_li__/__index_li__ so this pass can find them unambiguously,
#         then retagged to their real "li" name once wrapped). Mirrors
#         core/mapping_engine.py's own _apply_enclose algorithm."""
#         for parent in list(root.iter()):
#             children = list(parent)
#             i = 0
#             while i < len(children):
#                 if predicate(children[i]):
#                     j = i
#                     while j < len(children) and predicate(children[j]):
#                         j += 1
#                     run = children[i:j]
#                     wrapper = etree.Element(wrapper_tag, **wrapper_attrs)
#                     idx = list(parent).index(run[0])
#                     for el in run:
#                         parent.remove(el)
#                         if retag:
#                             el.tag = retag
#                         wrapper.append(el)
#                     parent.insert(idx, wrapper)
#                     children = list(parent)
#                     i = idx + 1
#                 else:
#                     i += 1

#     @staticmethod
#     def _page_break_span(page):
#         return etree.Element(
#             "span", id=f"page_{page}", role="doc-pagebreak",
#             **{"aria-label": str(page), "epub_type": "pagebreak"},
#         )

#     # ------------------------------------------------------------- zone -> element
#     def _is_split_parent(self, zone) -> bool:
#         """Based on gui/zone_panel.py ZoneTreePanel._zone_status and
#         core/xml_generator.py's own flatten logic (mirrored here, not
#         imported, since both live methods are bound to unrelated internal
#         state) - a zone whose children are ALL split pieces of itself is
#         superseded by them and must never also emit its own element.

#         Deliberately DOES NOT also require c.tag == zone.tag the way those
#         two do: this is a real, expected EPUB workflow - draw one zone over
#         an "Alarcón, Pedro Antonio de / El Niño de la Bola, 20 / El Sombrero
#         de tres Picos, 20-1" index block, Horizontal Split it into one piece
#         per line, then retag the first piece Index Primary and the rest
#         Index Secondary/Territory individually. Requiring every split piece
#         to keep the SAME tag as its parent (as the XML profile's own check
#         does, since IT never needs pieces to diverge) would make the
#         original, now-superseded zone reappear as "still active" the moment
#         any one piece's tag differs from the others - producing the exact
#         kind of duplicate content the split-parent check exists to prevent
#         in the first place. Only is_split + source_zone_id (i.e. "this IS a
#         piece of THIS split") are checked here."""
#         if not zone.children:
#             return False
#         children = [self.zm.zones[c] for c in zone.children if c in self.zm.zones]
#         return bool(children) and all(
#             c.is_split and c.source_zone_id == zone.zone_id for c in children)

#     def _resolve_zone_to_elements(self, zone) -> list:
#         """Only ACTIVE/leaf zones are ever exported - a SPLIT-PARENT zone
#         (see _is_split_parent) is skipped entirely; its split children take
#         its EXACT position in the stream instead (as siblings, not nested
#         inside a surviving parent element), recursively, since a split
#         piece can itself be split again. Matches xml_generator.py's
#         documented, tested behavior for the XML profile exactly.

#         _gen_zone normally returns ONE element, but for a figure/image zone
#         whose containment children turn out to hold more than one
#         independently-numbered image+caption pairing (see _gen_zone's own
#         docstring), it returns a LIST of separate, independent elements
#         instead - one per real pairing, never one combined element - and
#         that list is used here directly rather than wrapped as a single
#         result."""
#         if self._is_split_parent(zone):
#             out = []
#             for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
#                 child = self.zm.zones.get(cid)
#                 if child is not None:
#                     out.extend(self._resolve_zone_to_elements(child))
#             return out
#         el = self._gen_zone(zone)
#         if isinstance(el, list):
#             return el
#         return [el] if el is not None else []

#     def _gen_zone(self, zone):
#         kind = zone.attributes.get("asset_kind")
#         if zone.tag in self._index_tag_to_level:
#             # Collected for _build_index_hierarchy at the end of generate() -
#             # never placed at this position directly (a secondary/territory
#             # term belongs NESTED under its primary/secondary parent, not
#             # here in flat reading-order position). Originally assumed a
#             # hierarchy zone never has real containment children of its own
#             # (manually zoned index content historically relied ENTIRELY on
#             # tag-implied level + reading-order position, never zone.
#             # parent_id) - auto_zoning/index_auto_zone.py's Auto Zone Index
#             # feature is the first caller that DOES create real parent_id
#             # links for Secondary/Territory children (spec: "create parent
#             # relationships... Reading Order and Level are separate
#             # properties"), so those children must still be visited and
#             # collected here too, or they silently vanish from the output
#             # entirely (confirmed: _build_index_hierarchy never even learns
#             # they exist otherwise). Each child is ALSO index-hierarchy-
#             # tagged (the only kind Auto Zone Index ever nests this way), so
#             # recursing back into this same branch for it - never appending
#             # a real element for it here (there is nowhere to append one:
#             # this zone itself produces no element, see `return None`
#             # below).
#             self._index_hierarchy_items.append((self._index_tag_to_level[zone.tag], zone))
#             for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
#                 child_zone = self.zm.zones.get(cid)
#                 if child_zone is not None:
#                     self._resolve_zone_to_elements(child_zone)
#             return None
#         if self.pagenum_cup_name and zone.attributes.get("cup_name") == self.pagenum_cup_name:
#             el = self._gen_pagenum_zone(zone)
#         elif zone.tag in self._NOTE_TAGS:
#             el = self._gen_footnote_zone(zone, zone.tag)
#         elif kind:
#             el = self._gen_image_zone(zone, kind)
#         elif zone.tag == "ref":
#             el = self._gen_ref_zone(zone)
#         elif zone.tag == "indexentry":
#             el = self._gen_index_entry_zone(zone)
#         elif zone.tag == "table":
#             el = self._gen_table_zone(zone)
#         else:
#             el = self._gen_text_zone(zone)
#         if el is None:
#             return None
#         child_ids = reading_order.sort_children_ids(self.zm, zone.zone_id)
#         if kind:
#             split_elements = self._split_combined_figure_children(zone, child_ids, el)
#             if split_elements is not None:
#                 return split_elements
#         if zone.tag == "p" and child_ids:
#             # Nested/inline zone support (spec: "NESTED ZONE / INLINE ZONE
#             # SUPPORT") - _gen_text_zone already spliced every one of this
#             # Paragraph's own geometrically-nested children INLINE into
#             # its own text (or the correct one of several split
#             # paragraphs' own text - spec: "MULTIPLE PARAGRAPHS PER
#             # ZONE") at their exact position - appending them AGAIN here
#             # as trailing structural XML children would duplicate them.
#             # `el` may be a single element or (multiple detected
#             # paragraphs) a list - either way it's already exactly what
#             # this zone should contribute, unchanged. Scoped to "p" only:
#             # every OTHER tag with real structural children (list,
#             # title-group, boxed-text, ...) keeps its EXISTING, unchanged
#             # behavior via the loop below.
#             return el
#         for cid in child_ids:
#             child_zone = self.zm.zones.get(cid)
#             if child_zone is None:
#                 continue
#             for child_el in self._resolve_zone_to_elements(child_zone):
#                 el.append(child_el)
#         return el

#     def _split_combined_figure_children(self, zone, child_ids, outer_el):
#         """Real, confirmed structural defect this guards against (spec:
#         "FIX FIGURE ORDER + FIGURE CROSS-LINKS" - "Multiple independently
#         numbered figures must not be incorrectly combined into one logical
#         figure"): this generator's own containment recursion (the loop just
#         above) has no limit on how many children a figure/image zone can
#         have - if upstream zone containment (parent_id, assigned outside
#         this file) ever nests a SECOND, independently-drawn, independently-
#         numbered image (itself asset_kind-truthy) under ONE outer
#         figure-tagged zone, this method previously combined it (plus its own
#         caption) into the outer zone's own <figure> element as if it were
#         just more content. No later post-processing pass can undo that once
#         it has already happened - the fix has to live here, at generation
#         time.

#         Returns None (the overwhelmingly common case: no nested image-kind
#         child at all) - the caller's own normal single-element path handles
#         it unchanged, zero behavior change. Returns a LIST of independent
#         elements - the outer zone's OWN already-built `outer_el` (never
#         discarded - it is the first, real, independently-numbered image)
#         plus one freshly-generated element per nested image-kind child -
#         when at least one nested image-kind child is found: `outer_el`
#         absorbs every child up to (not including) the first nested
#         image-kind child (its own caption, if drawn before the image in the
#         stream); each subsequent group starts at a nested image-kind child
#         and absorbs every immediately-following non-image-kind child (its
#         own caption) up to (not including) the next image-kind child - so
#         every real caption stays paired with its own real image and nothing
#         is ever dropped. A warning is recorded via the existing
#         self.warnings mechanism so the anomaly is surfaced rather than
#         silently produced."""
#         groups = [[]]
#         for cid in child_ids:
#             child = self.zm.zones.get(cid)
#             if child is not None and child.attributes.get("asset_kind"):
#                 groups.append([cid])
#             else:
#                 groups[-1].append(cid)
#         if len(groups) == 1:
#             return None

#         self.warnings.append(
#             f"Zone {zone.zone_id} contains {len(groups)} independently-numbered images - "
#             f"generated as {len(groups)} separate figures instead of one combined figure.")

#         def _fill(target_el, group_child_ids):
#             for cid in group_child_ids:
#                 child_zone = self.zm.zones.get(cid)
#                 if child_zone is None:
#                     continue
#                 for child_el in self._resolve_zone_to_elements(child_zone):
#                     target_el.append(child_el)

#         _fill(outer_el, groups[0])
#         elements = [outer_el]
#         for group in groups[1:]:
#             leading_zone = self.zm.zones[group[0]]
#             group_el = self._gen_image_zone(leading_zone, leading_zone.attributes.get("asset_kind"))
#             if group_el is None:
#                 continue
#             _fill(group_el, group[1:])
#             elements.append(group_el)
#         return elements

#     def _gen_text_zone(self, zone):
#         """The generic fallback for any zone not handled by a dedicated
#         _gen_*_zone method (pagenum/footnote/image/ref/indexentry) -
#         covers ordinary text-bearing tags (p, title, reference, ...) AND
#         container tags with real geometric children (list, table, ...),
#         since neither this profile nor Mapping.xml's own tag vocabulary
#         needs a separate per-container-tag method the way _gen_image_zone/
#         _gen_footnote_zone do.

#         A zone WITH children never re-extracts its own bbox text (real,
#         confirmed bug otherwise: a List/Table container's own bbox
#         typically geometrically SPANS its own children's text regions, so
#         re-extracting from it duplicates whatever the children
#         independently produce a moment later in _gen_zone's own child-
#         appending loop - confirmed directly: a List zone with 2 real
#         List-Item children produced "<ul>Item one. Item two.<li>Item
#         one.</li><li>Item two.</li></ul>", the flat text and the <li>
#         children both present, before this guard existed). Matches
#         core.xml_generator.py's own established pattern for the exact
#         same situation (its _zone_list/_gen_zone_list_grouped already
#         check `if children:` before ever calling extract_zone_
#         formatted_text) - this generalizes that same, already-correct
#         rule to every tag here instead of only "list".

#         EXCEPT for "p" specifically, handled entirely separately below
#         (spec: "NESTED ZONE / INLINE ZONE SUPPORT" + "MULTIPLE PARAGRAPHS
#         PER ZONE"). Every other tag's existing all-or-nothing content=""
#         behavior is completely unchanged."""
#         page = self.pdf.get_page(zone.page)
#         if zone.tag == "p":
#             # A Paragraph's own geometrically-nested children (spec:
#             # "NESTED ZONE / INLINE ZONE SUPPORT") are a real, confirmed,
#             # DIFFERENT situation from List/Table's structural children -
#             # they represent an inline unit (a small nested image, or a
#             # run of distinctly-formatted text) meant to sit INSIDE the
#             # paragraph's own running text at its exact position, never a
#             # separate block sibling:
#             #   - an image-kind child (asset_kind set) is rendered via the
#             #     EXISTING, unchanged _gen_image_zone - the same method a
#             #     top-level inline image already uses - then serialized.
#             #   - any other child has its own bbox extracted directly via
#             #     the EXISTING, unchanged extract_zone_formatted_text.
#             # text_extractor.extract_zone_paragraphs then buckets each
#             # (child_bbox, markup) pair into whichever geometrically
#             # detected PDF paragraph its own bbox falls into (spec:
#             # "MULTIPLE PDF PARAGRAPHS INSIDE ONE SAVED ZONE" - a single
#             # drawn zoning box can legitimately span several real PDF
#             # paragraphs) - returning exactly one string for the
#             # overwhelming common single-paragraph case (identical output
#             # to before this existed), or one string per genuinely
#             # detected paragraph otherwise. _gen_zone's own generic
#             # child-appending loop is skipped for "p" (see there), so
#             # this is the ONLY place these children are ever emitted,
#             # never duplicated.
#             children = []
#             for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
#                 child = self.zm.zones.get(cid)
#                 if child is None:
#                     continue
#                 child_kind = child.attributes.get("asset_kind")
#                 if child_kind:
#                     child_el = self._gen_image_zone(child, child_kind)
#                     markup = etree.tostring(child_el, encoding="unicode") if child_el is not None else ""
#                 elif child.tag == "en":
#                     # ENDNOTE XEN ORPHAN/WRONG-PLACEMENT FIX: a nested "en"
#                     # child is a genuine in-text citation marker at its own
#                     # exact position - see _gen_endnote_marker's own
#                     # docstring. Falls back to ordinary plain-text
#                     # extraction (never a fabricated marker) when the
#                     # zone's own text isn't cleanly just a reference number.
#                     marker_markup = self._gen_endnote_marker(page, child)
#                     markup = marker_markup if marker_markup is not None else \
#                         text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
#                 else:
#                     markup = text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
#                 if markup:
#                     children.append((child.bbox, markup))
#             contents = text_extractor.extract_zone_paragraphs(page, zone, children, self._hyphen_keep_at(zone))
#             elements = []
#             for content in contents:
#                 p_el = _parse_inline(f"<p>{content}</p>") if content else etree.Element("p")
#                 p_el.set(SRC_ATTR, zone.zone_id)
#                 for key, value in zone.attributes.items():
#                     if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
#                         continue
#                     p_el.set(key, value)
#                 elements.append(p_el)
#             if not elements:
#                 p_el = etree.Element("p")
#                 p_el.set(SRC_ATTR, zone.zone_id)
#                 return p_el
#             return elements if len(elements) > 1 else elements[0]

#         if zone.children:
#             content = ""
#         elif zone.tag == "title":
#             # Same break-joining behavior as the XML profile's _zone_title
#             # (title + byline lines joined with <break/>, not a space).
#             content = normalize_title_text(
#                 text_extractor.extract_zone_formatted_text_with_breaks(page, zone, self._hyphen_keep_at(zone)))
#         else:
#             content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#         el = _parse_inline(f"<{zone.tag}>{content}</{zone.tag}>") if content else etree.Element(zone.tag)
#         el.set(SRC_ATTR, zone.zone_id)
#         for key, value in zone.attributes.items():
#             if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
#                 continue
#             el.set(key, value)
#         return el

#     def _gen_endnote_marker(self, page, child):
#         """Spec: "ENDNOTE XEN ORPHAN/WRONG-PLACEMENT FIX" - builds the
#         genuine in-text citation marker
#         (<sup><a id="{prefix}_xen{n}" href="#{prefix}_en{n}">{n}</a></sup>)
#         for a footnote/endnote-tagged zone found NESTED inside a
#         paragraph (i.e. auto-parented at its own true citation position in
#         the running text, via the same containment mechanism an inline
#         image/formatted-run child already uses) - never for a top-level
#         "en" zone, which is always the endnote's own CONTENT, not a
#         marker (see _gen_footnote_zone's own docstring for why those two
#         are structurally different things that must never be conflated).

#         The marker's own reference number is read directly from the
#         zone's own already-recognized text - never invented, never
#         assigned from a running counter - so it links to EXACTLY the
#         {prefix}_en{n} id _gen_footnote_zone already assigns that same
#         endnote's own content by construction (spec: "Use the existing
#         endnote numbering/mapping... do NOT renumber"). Returns None
#         (never a fabricated marker) unless the zone's own text is
#         cleanly JUST a reference number - a real source marker, not a
#         zone that merely happens to contain a digit somewhere - matching
#         this fix's own "verify its reference number... do not create any
#         xen if no actual source marker exists" requirement exactly."""
#         _section_class, _item_epub_type, id_token, ref_token = self._NOTE_TAGS["en"]
#         text = text_extractor.extract_zone_plain_text(page, child, self._hyphen_keep_at(child))
#         m = re.fullmatch(r"\s*(\d+)\s*", text or "")
#         if not m:
#             return None
#         n = m.group(1)
#         ref_sup = etree.Element("sup")
#         ref_sup.set(SRC_ATTR, child.zone_id)
#         ref_a = etree.SubElement(ref_sup, "a", id=f"{self.prefix}_{ref_token}{n}",
#                                   href=f"#{self.prefix}_{id_token}{n}")
#         ref_a.text = n
#         return etree.tostring(ref_sup, encoding="unicode")

#     def _gen_pagenum_zone(self, zone):
#         """The profile's designated page-marker tag (CUPEPUB's "PageNum" -
#         see self.pagenum_cup_name) becomes a pagebreak span built from the
#         zone's own zone.text (spec: the PRINTED page label IS the EPUB
#         page number - "xiii", "1", "102" - the PDF's physical page index
#         is the zone's source LOCATION only and must never enter the id or
#         aria-label, and must never be used as a fallback), at its exact
#         reading-order position (this runs through the exact same
#         _resolve_zone_to_elements/_gen_zone path as every other zone, so
#         it lands wherever the zone actually sits in reading order, never
#         collected/moved elsewhere). Deliberately bypasses _gen_text_zone/
#         Mapping.xml entirely: CUPLookup.xml translates "PageNum" to the
#         literal tag "pagenum", which unrelatedly collides with a DIFFERENT
#         Mapping.xml rule (exhead|expara|pagenum -> a <sec1> EXERCISE
#         wrapper - confirmed by inspection, nothing to do with page
#         markers) that would otherwise wrap it in a nonsensical structure.

#         Reads zone.text rather than re-extracting from the PDF bbox live
#         (unlike _gen_text_zone) - confirmed necessary: ZoneManager.
#         set_zone_text (the manual PageNum editor's own Apply action) only
#         ever writes zone.text, so re-extracting from the PDF at generation
#         time would silently discard every manual correction and always
#         regenerate the OCR/extracted (possibly empty or wrong) value
#         instead - exactly the scanned-PDF failure mode this exists to fix.
#         zone.text already reflects automatic PDF/OCR extraction for a
#         digital PDF (ZoneManager._refresh_text populates it at zone-
#         creation time) unless/until manually overridden, so this one field
#         correctly serves both the digital and scanned/manual-entry cases.

#         An empty value (common for a scanned page OCR couldn't read) is
#         NOT a fatal error: the pagebreak is simply skipped (this zone
#         contributes nothing to the output) and a non-blocking warning is
#         recorded in self.warnings - generation of the rest of the document
#         continues normally. The zone itself is untouched in the project
#         (ZoneTool never deletes/alters it), so the user can fill it in and
#         regenerate later.

#         id is "page_{value}" (note the underscore before the value -
#         "page_1", never "page1") where {value} is the zone's own trimmed
#         text, sanitized to valid id characters; a repeated value within one
#         document gets a deterministic "_2"/"_3" suffix rather than a
#         silently duplicated id.

#         CRITICAL (spec: "ZONETOOL - MASTER PRODUCTION FIX & RESTORATION
#         PROMPT" sections 40/54): the pagebreak id must NEVER be prefixed
#         with the document's own semantic file prefix - "fm6_page_x" and
#         "bm3_page_271" are both wrong; only "page_x"/"page_271" are
#         correct. The semantic prefix (self.prefix) is a SEPARATE ID
#         namespace used everywhere else in this generator (headings,
#         sections, index entries, tables) - pagebreaks are the one
#         exception, by explicit spec rule, so self.prefix must never appear
#         here even though it's readily available on self."""
#         content = _close_digit_gap((zone.text or "").strip())
#         if not content:
#             self.warnings.append(
#                 f"PageNum on physical PDF page {zone.page} has no printed value and was skipped.")
#             return None
#         value = _sanitize_id_component(content)
#         base_id = f"page_{value}"
#         seen = self._pagenum_seen.get(base_id, 0)
#         self._pagenum_seen[base_id] = seen + 1
#         page_id = base_id if seen == 0 else f"{base_id}_{seen + 1}"
#         el = etree.Element(
#             "span", id=page_id, role="doc-pagebreak", epub_type="pagebreak",
#             **{"aria-label": content},
#         )
#         el.set(SRC_ATTR, zone.zone_id)
#         return el

#     def _strip_leading_source_number(self, content, expected_n):
#         """Only strips a leading digit run that EXACTLY matches the number
#         already being assigned to THIS entry (spec Part 13: "Do not
#         blindly delete all leading numbers... determine whether a number
#         is source-visible endnote number... using role, zone structure,
#         context") - an unrelated leading numeral (a different number, or
#         no match at all) is always left completely untouched, never
#         guessed at."""
#         if not content:
#             return content
#         m = self._LEADING_NUM_RE.match(content)
#         if m and m.group(1) == str(expected_n):
#             return content[m.end():]
#         return content

#     def _note_continuation_chain(self, zone_id):
#         """Returns the ordered list of zone_ids that explicitly Merge-
#         Previous'd onto zone_id, zone_id's own continuation's own
#         continuation, and so on (empty if none) - built from
#         self._merge_continuation_of (see generate()'s own comment for why
#         this exists: a footnote/endnote zone's explicit merge target can
#         be a SPLIT-PIECE CHILD that core.paragraph_merge.
#         merge_top_level_stream can only detect and defer, never itself
#         resolve, since core.reading_order.flatten_document_order never
#         includes a split piece as its own top-level stream entry).
#         Guards against a cyclic/self-referential merge_target (never
#         possible through the normal GUI action, but a hand-edited project
#         file could contain one) with a visited-set, rather than looping
#         forever."""
#         chain = []
#         seen = {zone_id}
#         current = zone_id
#         while True:
#             nxt = self._merge_continuation_of.get(current)
#             if nxt is None or nxt in seen:
#                 break
#             chain.append(nxt)
#             seen.add(nxt)
#             current = nxt
#         return chain

#     def _gen_footnote_zone(self, zone, tag):
#         """A footnote/endnote zone produces the actual note TEXT as a
#         fully-formed <li> target (id="{prefix}_fn{n}"/"{prefix}_en{n}",
#         with a role="doc-backlink" <a> pointing back to a reference marker).
#         target_id/ref_id/href always come from self._note_counters - a
#         single running counter per tag, NEVER reset mid-document - because
#         the existing xen citation-marker mechanism (_gen_endnote_marker)
#         computes its own href purely from a marker's raw source digit plus
#         this same counter, so resetting it would break that already-
#         working, separately-tested linkage.

#         For tag == "fn": appended directly to self._notes["fn"] for
#         _build_note_section to collect into ONE whole-document list at the
#         end of generate() - completely unchanged, existing behavior,
#         including its DISPLAYED number (which is simply n, the id counter,
#         since footnotes are never grouped - spec Part 25: keep footnote
#         behavior unchanged). This also returns an inline reference marker
#         (<sup><a id="{prefix}_xfn{n}" href="#{prefix}_fn{n}">{n}</a></sup>),
#         placed at the zone's own exact reading-order position like any
#         other element - unchanged, existing behavior.

#         For tag == "en": returns the <li> itself (tagged with the internal
#         placeholder "__note_li__", never a bare "li" - the SAME "build a
#         placeholder, wrap consecutive runs of it later" pattern already
#         used for ref/index lists, see _wrap_consecutive), placed INLINE at
#         this top-level content zone's own reading-order position (a top-
#         level "en" zone - no parent - is ALWAYS the endnote's own CONTENT,
#         never a genuine in-text citation marker - see
#         _nested_inline_content_for_paragraph's own "en" branch for where a
#         REAL citation marker is recognized instead, nested inside a citing
#         paragraph). Its DISPLAYED number and any leading source-visible
#         duplicate number are intentionally left unset/unstripped here -
#         _wrap_note_groups (called once, at the end of generate(), after
#         every "en" zone in the whole document has been visited) groups
#         consecutive runs of these placeholders (spec Part 10 - "endnote
#         group structure": two logical endnote groups, e.g. a PREFACE's own
#         notes and a following chapter's own notes, must stay two separate,
#         independently-numbered lists, restarting at 1, never one flat
#         whole-document list silently merging both with continuous
#         numbering) and only THEN knows each entry's correct
#         group-local position to number it from.

#         Before any of that: if some OTHER zone explicitly Merge-Previous'd
#         onto THIS one (self._merge_continuation_of/_note_continuation_chain
#         - most commonly a page-2 continuation fragment whose merge target
#         is a split-piece child, the one case core.paragraph_merge's own
#         top-level chain splicing cannot resolve itself, see generate()'s
#         own comment), delegate entirely to _gen_merged_footnote_zone for
#         the WHOLE chain instead - reusing its existing text-combining/id/
#         backlink logic rather than duplicating it, and guaranteeing the
#         continuation's own text lands INSIDE this single <li>, in the
#         correct position, never as a separate, independently-numbered
#         entry of its own."""
#         continuation = self._note_continuation_chain(zone.zone_id)
#         if continuation:
#             return self._gen_merged_footnote_zone([zone.zone_id] + continuation, tag)

#         section_class, item_epub_type, id_token, ref_token = self._NOTE_TAGS[tag]
#         page = self.pdf.get_page(zone.page)
#         content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#         self._note_counters[tag] += 1
#         n = self._note_counters[tag]
#         target_id = f"{self.prefix}_{id_token}{n}"
#         ref_id = f"{self.prefix}_{ref_token}{n}"

#         if tag == "fn":
#             content = self._strip_leading_source_number(content, n)

#         li = etree.Element("li" if tag == "fn" else "__note_li__",
#                             **{"class": id_token, "id": target_id, "epub_type": item_epub_type})
#         if tag != "fn":
#             li.set("_note_tag", tag)
#         li.set(SRC_ATTR, zone.zone_id)
#         backlink_sup = etree.SubElement(li, "sup")
#         backlink = etree.SubElement(backlink_sup, "a", **{
#             # No "id" here (Part 11 fix): role + href fully identify and
#             # link the backlink already; an extra id on this <a> served no
#             # purpose and nothing else in the codebase reads/depends on it
#             # (confirmed by search) - role/href/the <li>'s own id are the
#             # only load-bearing attributes.
#             "role": "doc-backlink", "href": f"#{ref_id}",
#         })
#         if tag == "fn":
#             backlink.text = str(n)
#         if content:
#             inline = _parse_inline(f"<x>{content}</x>")
#             backlink_sup.tail = inline.text
#             for c in inline:
#                 li.append(c)

#         if tag == "fn":
#             self._notes[tag].append(li)
#             ref_sup = etree.Element("sup")
#             ref_sup.set(SRC_ATTR, zone.zone_id)
#             ref_a = etree.SubElement(ref_sup, "a", id=ref_id, href=f"#{target_id}")
#             ref_a.text = str(n)
#             return ref_sup

#         return li

#     def _gen_merged_footnote_zone(self, zone_ids, tag):
#         """A footnote/endnote chain that continues across a page break
#         (core.paragraph_merge's footnote-to-footnote continuation - spec
#         sections 23/24/56) - the SAME single-marker/single-target output
#         _gen_footnote_zone produces for one zone, except the <li> target's
#         text is the COMBINED text of every zone in the chain (using each
#         later zone's own merge_join/hyphen-boundary handling, exactly like
#         _gen_merged_text_zone's own text-combining loop - never a second,
#         divergent join algorithm) and the counter increments exactly ONCE
#         for the whole chain, never once per constituent zone (that
#         one-per-zone increment was the actual root cause of "4 real
#         footnotes becoming 5" - a continuation fragment silently claiming
#         its own number instead of extending footnote 1's).

#         Also extends zone_ids with any FURTHER explicit continuation
#         chained off the last zone (self._merge_continuation_of/
#         _note_continuation_chain - see _gen_footnote_zone's own docstring)
#         - a three-or-more-fragment chain where only the last hop is a
#         split-piece-target continuation (the one case core.paragraph_merge
#         cannot resolve itself) would otherwise still split off its own
#         independent entry even after the first hop is correctly combined
#         here."""
#         continuation = self._note_continuation_chain(zone_ids[-1])
#         if continuation:
#             zone_ids = list(zone_ids) + continuation

#         section_class, item_epub_type, id_token, ref_token = self._NOTE_TAGS[tag]
#         combined = ""
#         for zid in zone_ids:
#             zone = self.zm.zones[zid]
#             text = text_extractor.extract_zone_formatted_text(
#                 self.pdf.get_page(zone.page), zone, self._hyphen_keep_at(zone))
#             if not text:
#                 continue
#             if combined:
#                 combined = text_extractor.join_boundary(
#                     combined, text, bool(zone.attributes.get("hyphen_keep_chain_boundary")),
#                     zone.attributes.get("merge_join", " "))
#             else:
#                 combined = text

#         first_zone = self.zm.zones[zone_ids[0]]
#         self._note_counters[tag] += 1
#         n = self._note_counters[tag]
#         target_id = f"{self.prefix}_{id_token}{n}"
#         ref_id = f"{self.prefix}_{ref_token}{n}"

#         if tag == "fn":
#             combined = self._strip_leading_source_number(combined, n)

#         li = etree.Element("li" if tag == "fn" else "__note_li__",
#                             **{"class": id_token, "id": target_id, "epub_type": item_epub_type})
#         if tag != "fn":
#             li.set("_note_tag", tag)
#         li.set(SRC_ATTR, first_zone.zone_id)
#         backlink_sup = etree.SubElement(li, "sup")
#         backlink = etree.SubElement(backlink_sup, "a", **{
#             "role": "doc-backlink", "href": f"#{ref_id}",   # no id - Part 11 fix, see _gen_footnote_zone
#         })
#         if tag == "fn":
#             backlink.text = str(n)
#         if combined:
#             inline = _parse_inline(f"<x>{combined}</x>")
#             backlink_sup.tail = inline.text
#             for c in inline:
#                 li.append(c)

#         if tag == "fn":
#             self._notes[tag].append(li)
#             ref_sup = etree.Element("sup")
#             ref_sup.set(SRC_ATTR, first_zone.zone_id)
#             ref_a = etree.SubElement(ref_sup, "a", id=ref_id, href=f"#{target_id}")
#             ref_a.text = str(n)
#             return ref_sup

#         # "en": same deferred-numbering placeholder as _gen_footnote_zone -
#         # _wrap_note_groups assigns the real group-local number later.
#         return li

#     def _gen_merged_text_zone(self, zone_ids):
#         """Explicit Merge Previous / automatic cross-page continuation,
#         ACTUALLY combining text (spec 98.16-98.21) - each later zone's own
#         extracted text is appended to the running total using THAT zone's
#         own merge_join attribute (" " or "", recorded by ZoneManager.
#         merge_with_previous; defaults to " " for an automatic continuation,
#         which never sets this attribute) as the separator, in chain order.
#         Produces exactly ONE output element, using the chain's FIRST zone's
#         own tag/attrs - every other zone in the chain is never
#         independently emitted (spec 98.21: the old second zone must not
#         appear on its own).

#         A PageNum zone_id found INSIDE the chain (core.paragraph_merge
#         splices one in, in position, whenever it sits between two
#         continuing fragments) becomes its own inline <span role=
#         doc-pagebreak> marker via the exact same _gen_pagenum_zone this
#         module already uses for a standalone PageNum zone - never merged
#         text, never dropped (spec 9).

#         A footnote/endnote zone_id found INSIDE the chain (core.
#         paragraph_merge splices one in the same way, at its own correct
#         relative position, whenever it sits between two continuing
#         fragments - a real, confirmed bug fixed there: it used to always
#         become its own independent top-level element, stranding the
#         footnote's own reference marker AFTER the whole merged paragraph
#         instead of at its true in-text position) is generated via the
#         SAME _gen_footnote_zone/_gen_merged_footnote_zone this module
#         already uses for a standalone footnote - never re-implemented
#         here, never merged as plain text. Consecutive same-tag footnote
#         zone_ids (the footnote's OWN text itself continued across a page
#         while it happened to be embedded here) are grouped and passed to
#         _gen_merged_footnote_zone TOGETHER, so the note-counter still
#         increments exactly once per logical footnote, never once per
#         constituent zone (the same "4 real footnotes becoming 5" bug
#         class _gen_merged_footnote_zone's own docstring documents)."""
#         first_zone = self.zm.zones[zone_ids[0]]
#         combined = ""
#         i, n = 0, len(zone_ids)
#         while i < n:
#             zid = zone_ids[i]
#             zone = self.zm.zones[zid]
#             if self.pagenum_cup_name and zone.attributes.get("cup_name") == self.pagenum_cup_name:
#                 pagebreak_el = self._gen_pagenum_zone(zone)
#                 if pagebreak_el is not None:
#                     combined += etree.tostring(pagebreak_el, encoding="unicode")
#                 i += 1
#                 continue
#             if zone.tag in self._NOTE_TAGS:
#                 group = [zid]
#                 j = i + 1
#                 while j < n and self.zm.zones[zone_ids[j]].tag == zone.tag:
#                     group.append(zone_ids[j])
#                     j += 1
#                 ref_sup = self._gen_footnote_zone(zone, zone.tag) if len(group) == 1 \
#                     else self._gen_merged_footnote_zone(group, zone.tag)
#                 if ref_sup is not None:
#                     combined += etree.tostring(ref_sup, encoding="unicode")
#                 i = j
#                 continue
#             text = text_extractor.extract_zone_formatted_text(
#                 self.pdf.get_page(zone.page), zone, self._hyphen_keep_at(zone))
#             if not text:
#                 i += 1
#                 continue
#             if combined:
#                 # Hyphenated Line-Break Text Normalization (spec sections
#                 # 13/14/20): a genuine line-break hyphen sitting exactly at
#                 # this merge-chain boundary (e.g. "inter-" | "national")
#                 # must dehyphenate the SAME way a within-zone one does,
#                 # not fall through to the chain's own plain merge_join
#                 # separator - text_extractor.join_boundary is the single
#                 # place that decides between the two, using this zone's
#                 # own "keep this specific boundary's hyphen" override
#                 # (hyphen_keep_chain_boundary, set only via gui.dialogs.
#                 # HyphenReviewDialog's chain-boundary candidates).
#                 combined = text_extractor.join_boundary(
#                     combined, text, bool(zone.attributes.get("hyphen_keep_chain_boundary")),
#                     zone.attributes.get("merge_join", " "))
#             else:
#                 combined = text
#             i += 1
#         tag = first_zone.tag
#         el = _parse_inline(f"<{tag}>{combined}</{tag}>") if combined else etree.Element(tag)
#         el.set(SRC_ATTR, first_zone.zone_id)
#         for key, value in first_zone.attributes.items():
#             if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
#                 continue
#             el.set(key, value)
#         return el

#     def _gen_ref_zone(self, zone):
#         """"Reference (Plain)" - a bibliography entry with NO numbered/
#         author-date styling (contrast core/mapping_engine.py's ref_n/ref_d
#         handling, which IS Mapping.xml-driven and separately verified
#         against a different real book). Verified against a real sample:
#         <li class="biblioentry" epub:type="biblioref"><span class="reflabel"
#         id="{prefix}_refN"/>entry text</li>, with consecutive entries
#         wrapped in <ol class="none"> by _wrap_consecutive (generate())."""
#         page = self.pdf.get_page(zone.page)
#         content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#         self._ref_counter += 1
#         ref_id = f"{self.prefix}_ref{self._ref_counter}"
#         el = etree.Element("__ref_li__", **{"class": "biblioentry", "epub_type": "biblioref"})
#         el.set(SRC_ATTR, zone.zone_id)
#         label = etree.SubElement(el, "span", **{"class": "reflabel", "id": ref_id})
#         if content:
#             inline = _parse_inline(f"<x>{content}</x>")
#             label.tail = inline.text
#             for c in inline:
#                 el.append(c)
#         return el

#     def _gen_index_entry_zone(self, zone):
#         """"Index Entry" - verified against a real sample: a FLAT list (no
#         primary/secondary/territory nesting - contrast the separate,
#         unverified indexprimary/indexsecondary/indexterritory tags, which
#         stay available for a book whose index genuinely needs that
#         hierarchy). <li epub:type="index-entry" id="{prefix}_indN">term,
#         page refs...</li>, wrapped in <ul class="none" epub:type=
#         "index-entry-list"> by _wrap_consecutive (generate()).

#         Known limitation: real output wraps each page number in its own
#         <a epub:type="index-locator" href="{chapter file}#page_{n}">
#         cross-file link - building that requires knowing which OTHER
#         already-generated file contains a given page number, which spans
#         multiple PDFs/projects and is out of scope for a single Generate
#         XHTML run. Page numbers are emitted as plain extracted text here;
#         turning them into cross-file links is future, multi-file-assembly
#         work."""
#         page = self.pdf.get_page(zone.page)
#         content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
#         self._index_counter += 1
#         idx_id = f"{self.prefix}_ind{self._index_counter}"
#         el = etree.Element("__index_li__", **{"epub_type": "index-entry", "id": idx_id})
#         el.set(SRC_ATTR, zone.zone_id)
#         if content:
#             inline = _parse_inline(f"<x>{content}</x>")
#             el.text = inline.text
#             for c in inline:
#                 el.append(c)
#         return el

#     def _gen_table_zone(self, zone):
#         """Real, semantically-structured HTML table (spec: "EPUBForge -
#         TABLE DRAW - COMPLETE IMPLEMENTATION" - "Do not generate a
#         visually fake table using <div>/<p>/<br> when a real HTML table
#         is required"). This is the XHTML/EPUB-profile counterpart of
#         core.xml_generator.py's own proven _zone_table, reusing the EXACT
#         SAME detection engine (core.table_extractor.analyze_table - real
#         PDF layout geometry: ruling lines, word gaps, never DTD/BITS
#         rules, and never a second/duplicate table-structure detector).
#         Manual row/column split overrides (gui.pdf_viewer's Row/Column
#         Split Mode, zone.attributes["horizontal_splits"]/
#         ["vertical_splits"]) are honored identically to the XML profile's
#         own table generation - same attribute keys, same meaning.

#         Distinct from the EPUB profile's existing "Table" tag (tblimage,
#         asset_kind-based - captures the table as a cropped IMAGE, a
#         completely different and unmodified feature): this handles the
#         NEW "Table Draw" tag (tag literal "table", matching the XML
#         profile's own TAG_TABLE constant), for a table that should become
#         real semantic markup instead of a picture.

#         Deliberately simpler than _zone_table in one respect, disclosed
#         rather than silently approximated: it does not match manually-
#         drawn CHILD zones nested inside the Table zone to override a
#         specific cell's content (core.xml_generator.py's own
#         _match_table_cell_children) - every cell's text comes directly
#         from table_extractor's own real PDF-geometry-based extraction,
#         which already reuses core.text_extractor for exact bold/italic/
#         superscript/subscript/hyphenation-aware text, so Unicode/special-
#         character fidelity is identical either way; only the "let a
#         manually-zoned child override one specific cell" convenience is
#         not yet wired up for this profile."""
#         self._table_draw_counter += 1
#         tbl_num = self._table_draw_counter
#         page = self.pdf.get_page(zone.page)
#         structure = table_extractor.analyze_table(
#             page, zone.bbox, self._hyphen_keep_at(zone),
#             manual_row_splits=zone.attributes.get("horizontal_splits"),
#             manual_column_splits=zone.attributes.get("vertical_splits"))

#         # Visible table styling (spec: generated tables must actually
#         # render with borders/padding, not rely on a reader/browser
#         # supplying its own table CSS - many EPUB readers apply none at
#         # all to a bare <table>). Plain inline HTML attributes/style,
#         # same convention <b>/<i>/etc. inline tags already use elsewhere
#         # in this generator - not a new styling mechanism, and never
#         # touches core.xml_generator.py's own BITS/semantic-XML table
#         # output (that path targets further processing, not final
#         # rendering, so it deliberately carries no presentation markup).
#         table_el = etree.Element("table", id=f"{self.prefix}_table{tbl_num}",
#                                    border="1", cellpadding="8", cellspacing="0",
#                                    style="border-collapse: collapse; width: 100%;")
#         header_rows = structure.rows[:structure.header_row_count]
#         body_rows = structure.rows[structure.header_row_count:]
#         if header_rows:
#             thead_el = etree.SubElement(table_el, "thead")
#             for row_idx, row_cells in enumerate(header_rows):
#                 thead_el.append(self._table_row_element_html(row_cells, "th", tbl_num, row_idx))
#         if body_rows:
#             tbody_el = etree.SubElement(table_el, "tbody")
#             for row_idx, row_cells in enumerate(body_rows):
#                 tbody_el.append(self._table_row_element_html(row_cells, "td", tbl_num,
#                                                                 len(header_rows) + row_idx))
#         table_el.set(SRC_ATTR, zone.zone_id)
#         return table_el

#     def _table_row_element_html(self, row_cells, cell_tag: str, tbl_num: int, row_idx: int):
#         """One <tr> of real <th>/<td> cells (spec section 15/16: genuine
#         header/body cell types, never every first row converted to <th>
#         without evidence - the caller already decided header vs body rows
#         from table_extractor's own header_row_count, a real signal - bold-
#         text-ratio + top-of-table position, see table_extractor.py's
#         TABLE_HEADER_BOLD_RATIO - never "the first row always"). Cell ids
#         are deterministic (table number + row + column), so regenerating
#         the same document twice produces identical ids (spec section 40:
#         "Table ID stability... Do not generate random IDs")."""
#         tr_el = etree.Element("tr")
#         if cell_tag == "th":
#             tr_el.set("style", "background-color: #f2f2f2;")
#         cell_style = ("border: 1px solid #ddd; padding: 8px; text-align: left;" if cell_tag == "th"
#                       else "border: 1px solid #ddd; padding: 8px;")
#         col = 0
#         for cell in row_cells:
#             if cell is None:
#                 col += 1
#                 continue
#             cell_id = f"{self.prefix}_table{tbl_num}_r{row_idx + 1}c{cell.col + 1}"
#             cell_el = etree.SubElement(tr_el, cell_tag, id=cell_id)
#             cell_el.set("style", cell_style)
#             if cell.colspan > 1:
#                 cell_el.set("colspan", str(cell.colspan))
#             if cell.rowspan > 1:
#                 cell_el.set("rowspan", str(cell.rowspan))
#             if getattr(cell, "is_list", False) and cell.list_items:
#                 # A genuine detected bullet list stays as its own <ul>
#                 # INSIDE this cell (spec section 11: "within each cell,
#                 # text must be ordered top-to-bottom... do not combine
#                 # text from neighboring cells") - never flattened into
#                 # marker-prefixed plain text.
#                 ul_el = etree.SubElement(cell_el, "ul")
#                 for item_text in cell.list_items:
#                     li_el = etree.SubElement(ul_el, "li")
#                     if item_text:
#                         inline = _parse_inline(f"<x>{item_text}</x>")
#                         li_el.text = inline.text
#                         for c in inline:
#                             li_el.append(c)
#             elif cell.text:
#                 inline = _parse_inline(f"<x>{cell.text}</x>")
#                 cell_el.text = inline.text
#                 for c in inline:
#                     cell_el.append(c)
#             col += cell.colspan
#         return tr_el

#     def _gen_image_zone(self, zone, kind):
#         cfg = self.image_kinds.get(kind)
#         if cfg is None:
#             self.warnings.append(
#                 f"Zone {zone.zone_id} (tag={zone.tag!r}, page {zone.page}): no image_kinds "
#                 f"config for asset_kind={kind!r} in the active profile - image skipped.")
#             return None
#         # counter_key lets two DIFFERENT image_kinds entries (Inline
#         # Equation's "ineq_img" and Inline Figure's "inlinefig") share ONE
#         # sequential counter - confirmed with the user (spec section 16):
#         # they intentionally number as one shared "inline" sequence, not
#         # two independent ones that would otherwise both start at 1 and
#         # collide on the same first filename. Every other image_kinds
#         # entry has no counter_key, so it falls back to `kind` itself -
#         # completely unchanged, independent-per-type counting.
#         counter_key = cfg.get("counter_key", kind)
#         filename = self.assets.save_image_asset(
#             self.pdf, zone.page, zone.bbox, kind=counter_key, token=cfg.get("token", kind),
#             ext=cfg.get("ext", "png"), digits=cfg.get("digits", 2), no_prefix=cfg.get("no_prefix", False))
#         # The Cover image is matched by Mapping.xml via
#         # //component[@type='cover']/img (a plain <img>, not a <cover>
#         # element) - see profiles/epub_profile.json's comment on the
#         # "cover" image_kinds entry.
#         tag = "img" if zone.tag == "cover" else zone.tag
#         # Output-folder-naming spec: images live in a sibling "images/"
#         # directory next to the component's own XHTML file (never a flat,
#         # cross-component shared folder) - src must be the forward-slashed
#         # relative reference to it, never a bare filename or absolute path.
#         el = etree.Element(tag, src=f"images/{filename}")
#         el.set(SRC_ATTR, zone.zone_id)
#         img_type = zone.attributes.get("img_type")
#         if img_type:
#             el.set("type", img_type)
#         return el

#     # ------------------------------------------------------- ID assignment
#     def assign_missing_ids(self, root):
#         """Final deterministic-ID pass (spec 98.4-98.9, 98.36-98.40) - run
#         by the caller AFTER Mapping.xml's transformation AND
#         xhtml_writer.rename_internal_wrapper_tags (so a former sec1-6/ssec/
#         bssec wrapper, now <section>, is covered too). Anything that
#         already has a real id (most headings/notes/refs/index entries are
#         already id'd directly during generate()) is left untouched; an id
#         containing a literal "{" (an unsubstituted Mapping.xml placeholder
#         - e.g. glosshead's id='glos{0}', which has no <attrib> wired up to
#         fill it in) is treated as missing and replaced. Every new id is
#         "{prefix}_{kind}{N}" (spec 98.5-98.7 - prefix is this project's own
#         chapter/file identifier, e.g. "ch3"), continuing THIS instance's
#         own counters so nothing here can collide with an id already
#         assigned during generate()."""
#         for el in root.iter():
#             if not isinstance(el.tag, str):
#                 continue
#             local = etree.QName(el).localname if "}" in el.tag else el.tag
#             current_id = el.get("id")
#             if current_id and "{" not in current_id:
#                 continue
#             cls = el.get("class") or ""
#             if local == "section":
#                 self._sec_counter += 1
#                 el.set("id", f"{self.prefix}_sec{self._sec_counter}")
#             elif local in ("h1", "h2", "h3", "h4", "h5", "h6"):
#                 self._heading_counters[local] = self._heading_counters.get(local, 0) + 1
#                 el.set("id", f"{self.prefix}_{local}_{self._heading_counters[local]}")
#             elif local == "figure":
#                 if "Table" in cls:
#                     self._tbl_counter += 1
#                     el.set("id", f"{self.prefix}_tbl{self._tbl_counter}")
#                 else:
#                     self._fig_counter += 1
#                     el.set("id", f"{self.prefix}_fig{self._fig_counter}")
#             elif local == "div" and "equation" in cls:
#                 self._eq_counter += 1
#                 el.set("id", f"{self.prefix}_eq{self._eq_counter}")

#     def assign_biblioentry_reflabel_ids(self, root):
#         """Injects <span class="reflabel" id="{prefix}_ref_{n}"/> as the
#         first child of every CUPEPUB Mapping.xml-produced bibliography
#         entry (<li class="biblioentry" epub_type="biblioentry"> - see
#         profiles/CUPEPUB/Mapping.xml's own `find="//ref_d | //ref_n"`
#         rule). That rule only renames/wraps the original ref_d/ref_n
#         element via <replace_ele> - it never adds an id or reflabel span
#         of its own, so these entries previously had no id at all.

#         {prefix} is this generator's own self.prefix - already the
#         COMPLETE filename stem (settings["prefix"] = Path(pdf_path).stem,
#         set in App.open_pdf() - e.g. "01_095AR_ch100", never just "ch"),
#         so no separate prefix-extraction logic is needed here.

#         Deliberately a SEPARATE counter/id format from the existing
#         "Reference (Plain)" zone tag's own _gen_ref_zone/_ref_counter
#         ({prefix}_refN, no underscore before the number, epub_type=
#         "biblioref") - a completely different code path for a different
#         zone tag, left untouched; reusing that counter/format was not
#         required and risked coupling two otherwise-independent features.

#         Idempotent - checked via the reflabel span itself (a biblioentry
#         <li> never gets an id of its own), safe to call more than once.
#         Numbers sequentially per call (per generated document), starting
#         at 1, with no gaps or reuse."""
#         counter = 0
#         for el in root.iter():
#             if not isinstance(el.tag, str) or el.tag != "li":
#                 continue
#             if el.get("epub_type") != "biblioentry":
#                 continue
#             if el.find("span[@class='reflabel']") is not None:
#                 continue
#             counter += 1
#             span = etree.Element("span", **{"class": "reflabel", "id": f"{self.prefix}_ref_{counter}"})
#             # el.insert(0, span) alone is NOT enough to make the span
#             # serialize FIRST: lxml's element.text (the <li>'s own direct
#             # text content, immediately after its start tag) is a
#             # SEPARATE property from its children and always renders
#             # before them regardless of child order - inserting at index
#             # 0 without moving it produces <li>TEXT<span/></li>, not the
#             # required <li><span/>TEXT</li> (confirmed as a real bug via
#             # a real App.generate_xhtml() run, not assumed). Moving the
#             # <li>'s own text onto the new span's OWN tail (the text that
#             # follows the span's end tag) fixes this correctly.
#             span.tail = el.text
#             el.text = None
#             el.insert(0, span)

#     def promote_maintitle_to_h1(self, root):
#         """CUPEPUB's own maintitle|subtitle|chapau|chapaff Mapping.xml rule
#         (see profiles/CUPEPUB/Mapping.xml) only ENCLOSES a run of those tags
#         in a <header> - it never renames <maintitle> itself, so a real
#         generated document previously kept a literal <maintitle>Bibliography
#         </maintitle> forever (confirmed directly: output/xhtml/*.xhtml
#         samples on disk). <maintitle> is the exact same logical concept as
#         the "title" zone tag _gen_node already promotes straight to <h1>
#         during generate() (see _build_document_header) - given the SAME
#         <header><h1 id=... class=... epub_type="title"> shape here, no
#         second title-element convention introduced. Must run AFTER
#         Mapping.xml (maintitle only exists post-transform) and BEFORE
#         assign_missing_ids, so the renamed element is picked up by that
#         same existing id-assignment pass instead of a new one."""
#         already_has_title_h1 = any(
#             isinstance(el.tag, str) and el.tag == "h1" and el.get("epub_type") == "title"
#             for el in root.iter())
#         for el in root.iter():
#             if not isinstance(el.tag, str) or el.tag != "maintitle":
#                 continue
#             el.tag = "h1"
#             if not already_has_title_h1:
#                 el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
#                 el.set("epub_type", "title")
#                 already_has_title_h1 = True

#     # Generic, human-readable fallback title per content type (spec:
#     # "ZONETOOL - MASTER PRODUCTION FIX" sections 5/71/72 - "<title>Index
#     # </title>", never "<title>27_63802_bm3</title>") - used ONLY when the
#     # document has no maintitle/title heading at all (extract_document_
#     # title returns None); a real title zone always wins. Deliberately
#     # NOT exhaustive - an unlisted/unusual component_type falls back to a
#     # simple title-cased version of its own key (e.g. "foreword" ->
#     # "Foreword"), never the raw file prefix.
#     _DEFAULT_TITLE_FOR_COMPONENT_TYPE = {
#         "index": "Index", "preface": "Preface", "bibliography": "Bibliography",
#         "glossary": "Glossary", "appendix": "Appendix", "foreword": "Foreword",
#         "introduction": "Introduction", "dedication": "Dedication", "toc": "Contents",
#         "acknow": "Acknowledgments", "contributor": "Contributors", "series": "Series",
#         "copyrightpage": "Copyright", "titlepage": "Title Page", "halftitle": "Half Title",
#         "cover": "Cover", "fm": "Front Matter", "bm": "Back Matter",
#     }

#     def default_title_for_component_type(self, component_type):
#         if not component_type:
#             return None
#         key = component_type.strip().lower()
#         return self._DEFAULT_TITLE_FOR_COMPONENT_TYPE.get(key) or key.replace("_", " ").title() or None

#     def extract_document_title(self, root):
#         """Document title = the SAME logical maintitle used for the visible
#         <h1> (never independently re-searched from raw zones) - finds the
#         h1 already marked epub_type="title" (set either by
#         promote_maintitle_to_h1 above or by the pre-existing "title" zone ->
#         h1 route in _build_document_header, both the same marker) and
#         flattens its text, including any inline-markup children (e.g. an
#         italicized word in the title) via itertext(). Returns None (caller
#         falls back to the filename) if the document has no such heading at
#         all - never raises, never returns an empty/whitespace string."""
#         for el in root.iter():
#             if isinstance(el.tag, str) and el.tag == "h1" and el.get("epub_type") == "title":
#                 text = "".join(el.itertext()).strip()
#                 return text or None
#         return None



"""EPUB-profile intermediate-XML generator.

Reuses the SAME zoning/reading-order/hierarchy engine as core/xml_generator.py
(no second zoning engine - core.reading_order/core.hierarchy, the exact
modules the XML profile's generator already relies on) but emits Mapping.xml's
OWN element vocabulary directly: for the EPUB profile, zone.tag literally IS
the Mapping.xml element name (see profiles/epub_profile.json) - "uncapfig",
"h1", "para", "eqn_img", etc - so most of this generator's per-zone job is
just "extract this zone's text/image and wrap it in an element named
zone.tag", not a second tag-translation table.

Heading/section NESTING (h1..h6) is built here via core.hierarchy, NOT left
to Mapping.xml's coverAbove rule as an earlier version of this file assumed -
real production output (confirmed across two different books) keeps h1/h2/
etc as literal <h1>/<h2> elements wrapped in a <section aria-labelledby="...">
container, never renamed to a <sec1>/<sec2> custom element the way a literal
reading of Mapping.xml's coverAbove rule would produce. hierarchy.py's own
tree-building (already tested for the XML profile) does exactly this kind of
"heading absorbs following same-or-lower-level content" nesting already, so
reusing it directly is both more reliable and more faithful to the confirmed
real output than reimplementing the same nesting logic as a mapping rule.

A few other constructs (footnote/endnote collection, plain/unlabeled
bibliography references, flat index entries) are ALSO built directly here
rather than through Mapping.xml, because real output either contradicts the
one Mapping.xml rule that looked relevant (footnotes: confirmed typo
"epub_types" vs real "epub:type", plus a different wrapper shape entirely)
or isn't covered by any rule in the Mapping.xml on hand at all (plain
references, flat index entries) - see the docstrings below for exactly what
was verified against real samples vs inferred by symmetry.

The output of generate() is the "EPUB XML/intermediate structure" step of
the spec's pipeline (PDF -> zoning -> hierarchy -> reading order -> EPUB
intermediate XML -> Mapping.xml transformation -> XHTML) - NOT the final
XHTML. core/mapping_engine.py performs the next step for everything this
generator does NOT already hardcode.
"""
import re

from lxml import etree

from core import reading_order, hierarchy, text_extractor, table_extractor, epub_lists
from core.image_extractor import AssetManager
from core.xml_generator import _parse_inline, normalize_title_text

# Traceability: the source zone_id, stamped on every emitted element so
# core/mapping_engine.py can report "source zone X, page Y" in error
# messages (spec section 25) without a second parallel bookkeeping
# structure. Stripped back out just before final XHTML serialization.
SRC_ATTR = "data-zt-src"

_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _sanitize_id_component(value: str) -> str:
    """Replaces any character not valid in an XML id with "-", preserving
    the semantic value (e.g. "ix" stays "ix", " 1 " stripped to "1") -
    used for CUPEPUB PageNum ids, which embed the zone's own printed page
    label directly rather than a synthetic counter."""
    return _ID_UNSAFE_RE.sub("-", value)


_DIGIT_GAP_RE = re.compile(r"(?<=\d)\s+(?=\d)")


def _close_digit_gap(text: str) -> str:
    """Closes a stray whitespace gap sitting directly BETWEEN two digits
    (e.g. a genuine printed page number "208" that PDF text extraction
    reported as "20 8" - confirmed real symptom: font kerning/spacing on
    some page-number glyphs can read as a word gap even though it's one
    single number) - a PageNum zone's content is structurally always a
    single short page label (never legitimate multi-word text), so
    collapsing ONLY a digit-to-digit gap here is safe and narrowly scoped;
    it never touches a gap next to a non-digit character (a genuine
    Roman-numeral/prefixed label like "p. 20" or "iv" is untouched)."""
    return _DIGIT_GAP_RE.sub("", text)

# zone.attributes keys that are internal/UI bookkeeping, never copied onto
# the emitted intermediate element as a literal XML attribute.
# "cup_name" is CUPEPUB-only (core/cup_config.py stamps every zone with its
# original CUP tag name, e.g. "PageNum", for core/cup_validation.py's
# mandatory-zone check) - never set by the XML/EPUB profiles, so this is a
# pure addition for them.
_INTERNAL_ATTR_KEYS = {
    "asset_kind", "img_type", "merged_formatted_text", "hyphen_keep_boundaries",
    "hyphen_keep_chain_boundary",
    "source", "list_type", "cup_name", "merge_join", "manual_text",
    # Real, confirmed bug: a zone whose tag isn't text-combinable (e.g.
    # List/Figure/Table - see core/hierarchy.py's push_single_zone) still
    # gets its own full zone.attributes dict copied onto its output
    # element via this same generic filter - merged_with_previous/
    # merge_target are ZoneTool's own internal Merge Previous bookkeeping
    # (core/zone_manager.py's merge_with_previous) and must never leak
    # into generated XHTML as a literal (invalid) element attribute.
    "merged_with_previous", "merge_target",
    # auto_zoning/index_auto_zone.py's own bookkeeping (index hierarchy
    # LEVEL and whether an entry was reconstructed from a wrapped
    # continuation line) - internal to detection/preview, never a literal
    # output attribute (same bug class as merge_target above).
    "index_level", "index_wrapped",
    # Auto Zone / Auto Tag engine bookkeeping (auto_zoning/auto_tag_engine.py,
    # core/zone_manager.py lock/override state) - never output attributes.
    "auto_engine", "auto_role", "auto_evidence", "auto_alternatives", "confidence", "confidence_breakdown",
    "needs_review", "review_reasons", "locked", "manual_override",
}


class EpubXmlGenerator:
    # h1's own @class, chosen by the active component_type - verified
    # against real fmtitle/chaptitle/bibtitle samples; anything not listed
    # falls back to "fmtitle" (the most common case: most frontmatter/
    # backmatter component types share it).
    _H1_CLASS_BY_COMPONENT = {
        "chapter": "chaptitle", "part": "chaptitle", "section": "chaptitle",
        "bibliography": "bibtitle", "index": "indtitle", "glossary": "glosstitle",
    }

    # Tags whose content is collected and moved to ONE trailing section at
    # the end of the document, in reading order, rather than left in their
    # natural position - reverse-engineered from real production output
    # (every footnote in a chapter file ends up together in a single
    # <section class="footnotes" epub:type="footnotes"><ol class="decimal">
    # <li class="fn" id="{prefix}_fn{N}" epub:type="footnote">...</li>...
    # </ol></section> at the very end of that file). Mapping.xml's own fn/en
    # rules are bypassed for these tags (consumed here before Mapping.xml
    # ever runs) - its <sec1 epub_types="footnotes"> rule has a confirmed
    # typo (epub_types, not epub_type) and a different wrapper shape than
    # the verified real output. "en" (endnotes) is inferred by direct
    # symmetry with the verified "fn" case - not independently confirmed.
    #
    # 4th element (ref_token) is the reference-marker id prefix ("xfn"/
    # "xen") - a footnote/endnote zone now produces BOTH an inline
    # reference marker (<sup><a id="{prefix}_xfn{n}" href="#{prefix}_fn
    # {n}">{n}</a></sup>, left at the zone's own reading-order position)
    # and the collected target <li> (id="{prefix}_fn{n}", with a
    # role="doc-backlink" <a> pointing back to the reference) - see
    # _gen_footnote_zone. Previously the WHOLE zone was diverted straight
    # into the end-of-document section via _place()'s tag-based routing,
    # leaving literally no trace at the reference's real position - a
    # confirmed bug (spec: footnote references must never be silently
    # dropped from their original location).
    _NOTE_TAGS = {"fn": ("footnotes", "footnote", "fn", "xfn"), "en": ("endnotes", "endnote", "en", "xen")}
    # A footnote/endnote zone's own extracted text usually already begins
    # with the source's own printed number ("1 James Tully..."), which
    # would otherwise duplicate visually next to the generated backlink's
    # own number (e.g. "11 James Tully..." - Part 13's reported bug).
    _LEADING_NUM_RE = re.compile(r"^\s*(\d+)[.)]?\s+")

    # Index Primary/Secondary/Territory: a REAL nested list (<ul><li>term
    # <ul><li>sub-term <ul><li>sub-sub-term</li></ul></li></ul></li></ul>),
    # not the <sec2>/<sec3>/<sec4> wrapping Mapping.xml's own indexprimary/
    # indexsecondary/indexterritory rules literally specify - confirmed
    # wrong by inspecting actual output (a real hierarchical index nests
    # sub-terms under their parent term via nested lists, matching the
    # verified flat "Index Entry" <ul>/<li> shape one level at a time, not
    # sibling <sec>-wrapped runs). Collected here (bypassing those Mapping.xml
    # rules entirely, same precedent as footnotes/plain-refs/flat index
    # entries above) and assembled into one tree in generate() from the
    # flat, reading-order-collected (level, zone) list - see
    # _build_index_hierarchy.
    _INDEX_HIERARCHY_LEVELS = {"indexprimary": 1, "indexsecondary": 2, "indexterritory": 3}

    def __init__(self, zone_manager, pdf_document, assets_dir: str, prefix: str, profile: dict,
                 component_type: str = None, jpeg_quality: int = 95, image_dpi: int = None,
                 remove_image_background: bool = False, image_prefix: str = None):
        self.zm = zone_manager
        self.pdf = pdf_document
        self.prefix = prefix
        self.profile = profile
        self.component_type = component_type or profile.get("default_component_type", "chapter")
        self.image_kinds = profile.get("image_kinds", {})
        # CUPEPUB-only (both None/True for XML/EPUB, which have neither
        # concept - see core/cup_config.py): the CUP tag that represents a
        # physical page-number marker, and whether pagebreaks should
        # instead come automatically from every PDF page boundary (EPUB's
        # existing, unchanged behavior - see _maybe_page_break).
        self.pagenum_cup_name = profile.get("cup_pagenum_name")
        self.auto_pagebreak = profile.get("auto_pagebreak", True)
        # CUPEPUB-only: [start_tag, end_tag] output-tag pair for Box_Start/
        # Box_End (e.g. ["box_start", "box_end"]) - None for XML/EPUB,
        # which have no such concept (XML's own boxed-text-start/-end is
        # handled unconditionally inside core/hierarchy.py itself, not via
        # this profile-supplied pair). See _gen_node's "boxed-text" branch.
        self.box_container_tags = profile.get("box_container_tags")
        assets_kwargs = {} if image_dpi is None else {"dpi": image_dpi}
        self.assets = AssetManager(assets_dir, prefix, jpeg_quality, remove_background=remove_image_background,
                                    image_prefix=image_prefix, **assets_kwargs)
        # Populated during generate() - non-fatal issues (e.g. an image
        # zone whose asset_kind has no image_kinds config) surfaced to the
        # caller for a warning dialog, never silently dropped without trace.
        self.warnings: list[str] = []
        # Per-generate() run state (reset in generate()). _fig_counter/
        # _tbl_counter/_eq_counter/_heading_counters are also read/
        # incremented by assign_missing_ids (called AFTER generate()+
        # Mapping.xml, on this same instance) so a fresh ID assigned there
        # can never collide with one assigned during generate() itself.
        self._last_page = None
        self._sec_counter = 0
        self._ref_counter = 0
        self._index_counter = 0
        self._fig_counter = 0
        self._tbl_counter = 0
        self._table_draw_counter = 0  # semantic <table> zones (tag "table") - separate from
        # _tbl_counter above, which numbers the UNRELATED image-crop "Table" (tblimage) asset type;
        # sharing one counter between the two would risk colliding ids if a document has both kinds.
        self._eq_counter = 0
        self._heading_counters = {}
        self._h1_seen = False
        self._notes = {}
        # Per-tag ("fn"/"en") sequential counters for footnote/endnote
        # reference<->target numbering (spec: sequential, deterministic,
        # never random/coordinate-based). NEVER reset mid-document (see
        # generate()'s own comment) - the existing xen citation-marker
        # mechanism (_gen_endnote_marker) computes its href purely from a
        # marker's own raw source digit plus this same running counter, so
        # target_id/ref_id must stay exactly this single global sequence
        # or already-working marker<->content linking breaks.
        self._note_counters = {}
        self._merge_continuation_of = {}
        # {base_pagenum_id: how many times it's been used so far} - a
        # printed page label repeating within one document (spec 98.12/
        # this fix's section 12) gets a deterministic "_2"/"_3" suffix
        # instead of a silently duplicated id; never resolved by
        # substituting the physical PDF page number.
        self._pagenum_seen = {}
        self._pending_header = None
        self._index_hierarchy_items = []
        # tag->level lookup for _gen_zone's index-hierarchy branch below -
        # profile-driven (spec: "read the active profile / Mapping.xml...
        # this allows future client profiles to use different tags"), never
        # hardcoded as the only implementation. profile["index_hierarchy_
        # tags"] is always level->tag (see core/profile_manager.py, core/
        # cup_config.py) - inverted here to the tag->level direction this
        # generator actually looks zones up by. Falls back to the class
        # default (unchanged behavior) for a profile that doesn't declare
        # its own mapping at all (e.g. an older cached profile dict).
        level_to_tag = profile.get("index_hierarchy_tags")
        self._index_tag_to_level = ({tag: int(level) for level, tag in level_to_tag.items()}
                                     if level_to_tag else dict(self._INDEX_HIERARCHY_LEVELS))

    def _hyphen_keep_at(self, zone):
        """Hyphenated Line-Break Text Normalization (spec: "Hyphenated
        Line-Break Normalization Before XHTML Generation") - the user's
        post-zoning Hyphen Normalization Review overrides for zone's own
        text (core.text_extractor.find_all_hyphen_candidates / gui.
        dialogs.HyphenReviewDialog), a set of boundary indices (see
        text_extractor.dehyphenate_join) where a detected line-break
        hyphen must be KEPT rather than auto-joined. Identical to core.
        xml_generator.XMLGenerator's own _hyphen_keep_at (never a second,
        divergent implementation) - empty by default (no review
        performed, or every candidate left checked), reproducing the
        prior automatic-join behavior exactly for any project that never
        opens the review window.

        Confirmed as a real, pre-existing gap: every text-extraction call
        site in this class previously passed a hardcoded set() instead of
        this, meaning the Hyphen Normalization Review's own choices were
        silently ignored for XHTML/CUPEPUB generation even though the
        SAME review already worked correctly for Generate XML - this
        method (and its call sites) close that gap, not just add the new
        review-trigger wiring in gui/main_window.py."""
        return set(zone.attributes.get("hyphen_keep_boundaries", []))

    @text_extractor.generation_ocr_scope()
    def generate(self):
        """Returns an lxml Element: <component type="...">. Reuses
        core.hierarchy exactly like the XML profile's XMLGenerator does, so
        heading nesting/split-flattening/reading order all come from the
        same tested engine - just walked into a different (EPUB) element
        vocabulary instead of BITS."""
        self._last_page = None
        self._sec_counter = 0
        self._ref_counter = 0
        self._index_counter = 0
        self._fig_counter = 0
        self._tbl_counter = 0
        self._table_draw_counter = 0  # semantic <table> zones (tag "table") - separate from
        # _tbl_counter above, which numbers the UNRELATED image-crop "Table" (tblimage) asset type;
        # sharing one counter between the two would risk colliding ids if a document has both kinds.
        self._eq_counter = 0
        self._heading_counters = {}
        self._h1_seen = False
        self._notes = {tag: [] for tag in self._NOTE_TAGS}
        self._note_counters = {tag: 0 for tag in self._NOTE_TAGS}
        # target_zone_id -> the zone_id that explicitly Merge-Previous'd
        # onto it (attributes["merged_with_previous"]/["merge_target"]).
        # Built once here so _note_continuation_chain can find a
        # continuation regardless of whether the target is a plain top-
        # level zone (already handled correctly by core.paragraph_merge's
        # own top-level chain splicing - never double-processed here,
        # since a zone merge_top_level_stream already absorbed into a
        # chain is never independently passed to _gen_footnote_zone at
        # all) or a SPLIT-PIECE CHILD, which core.reading_order.
        # flatten_document_order deliberately never includes as a
        # top-level stream entry (see _order_key's own docstring) - so
        # paragraph_merge.py can only defer that case here, never resolve
        # it itself.
        self._merge_continuation_of = {
            z.attributes["merge_target"]: z.zone_id
            for z in self.zm.zones.values()
            if z.attributes.get("merged_with_previous") and z.attributes.get("merge_target")
        }
        self._pagenum_seen = {}
        self._pending_header = None
        self._index_hierarchy_items = []
        # A zoned MainTitle IS the document title (Mapping.xml + promote_
        # maintitle_to_h1 turn it into <header><h1 epub:type="title">). Then
        # no body heading may become a second title <header> - every body
        # heading opens its own <section> instead (see _gen_sec_node).
        if any(z.tag == "maintitle" for z in self.zm.zones.values()):
            self._h1_seen = True

        page_order = reading_order.compute_page_order(self.zm)
        extra_markers = [tuple(self.box_container_tags)] if self.box_container_tags else None
        doc_tree, tree_issues = hierarchy.build_document_tree(
            self.zm, page_order, extra_markers,
            page_marker_tags=self.profile.get("page_marker_tags"),
            footnote_flow_tags=self.profile.get("footnote_flow_tags"),
            non_flow_tags=self.profile.get("non_flow_tags"))
        # Previously discarded entirely (a real "boundary_issues" report -
        # e.g. an unmatched Box_Start/Box_End - never reached the user via
        # this path); now surfaced as ordinary generation warnings, never
        # blocking (core/cup_validation.py's own pre-generation pairing
        # check is still what actually blocks Generate XHTML for CUPEPUB).
        self.warnings.extend(tree_issues)

        root = etree.Element("component", type=self.component_type)
        for node in doc_tree["children"]:
            # A "Subtitle" zone immediately following the document's own
            # title absorbs into the SAME <header> as a second <h1
            # epub_type="subtitle">, instead of becoming loose sibling
            # content - verified against a real sample (maintitle +
            # subtitle both inside one <header>). Only fires while a
            # header is still "open" (i.e. nothing else has appeared yet).
            if self._pending_header is not None and node["type"] == "zone":
                zone = self.zm.zones[node["zone_id"]]
                if zone.tag == "subtitle":
                    for pb in self._maybe_page_break(zone.page):
                        root.append(pb)
                    self._absorb_subtitle(zone)
                    continue
            self._pending_header = None
            for el in self._gen_node(node):
                self._place(root, el)

        # "en" (endnotes) only - see _gen_footnote_zone's own docstring:
        # each top-level "en" zone's <li> was already placed INLINE, as a
        # "__note_li__" placeholder, at its own reading-order position
        # during the loop above (never accumulated into self._notes["en"],
        # which stays empty and is a no-op in the loop just below). This
        # groups consecutive runs of those placeholders (spec Part 10) and
        # assigns each group's own local numbering now that every "en"
        # zone in the document has actually been visited.
        self._wrap_note_groups(root, "en")

        for tag, (section_class, item_epub_type, id_token, ref_token) in self._NOTE_TAGS.items():
            if self._notes[tag]:
                root.append(self._build_note_section(self._notes[tag], section_class, item_epub_type))

        if self._index_hierarchy_items:
            root.append(self._build_index_hierarchy(self._index_hierarchy_items))

        self._wrap_consecutive(root, lambda e: e.tag == "__ref_li__", "ol",
                                {"class": "none"}, retag="li")
        self._wrap_consecutive(root, lambda e: e.tag == "__index_li__", "ul",
                                {"class": "none", "epub_type": "index-entry-list"}, retag="li")
        # Real, grouped, nested <ol>/<ul> lists from NumList/BullList/...
        # item zones (see core/epub_lists.py) - previously emitted as bare
        # <numlist>/<bulllist> elements that no Mapping.xml rule converts.
        epub_lists.build_lists(
            root,
            zone_x0=lambda zid: self.zm.zones[zid].bbox[0] if zid in self.zm.zones else None,
            src_attr=SRC_ATTR)
        return root

    def _place(self, container, el):
        """Appends el to container in its natural reading-order position.
        A footnote/endnote zone no longer routes its WHOLE element into the
        _notes bucket here - _gen_footnote_zone already splits it into an
        inline reference (returned normally, placed right here like any
        other element) and a separately-collected target <li> (appended
        directly to self._notes at build time), so nothing arriving at
        this method is ever itself an "fn"/"en" tagged element anymore."""
        container.append(el)

    # ------------------------------------------------------------- doc_tree walk
    def _maybe_page_break(self, page) -> list:
        # CUPEPUB has its own explicit page-number marker tag (see
        # _gen_pagenum_zone) - a pagebreak must ONLY ever come from an
        # actual PageNum zone for it, never automatically from crossing a
        # PDF page boundary (spec 98.10-98.14). EPUB (auto_pagebreak
        # defaults True - it has no such tag) keeps this exact original
        # behavior, unchanged.
        if not self.auto_pagebreak:
            return []
        if page != self._last_page:
            self._last_page = page
            return [self._page_break_span(page)]
        return []

    def _gen_node(self, node) -> list:
        """Mirrors core.xml_generator.XMLGenerator's own doc_tree node
        dispatch (_gen_node/_gen_sec/_gen_merged_p), just building EPUB
        elements instead of BITS ones. Returns a list (0, 1, or more
        elements) so a heading can return [pagebreak?, header-or-section]
        and a merge/boxed-text node can return several independent
        elements (see their branches below for why)."""
        ntype = node["type"]
        if ntype == "sec":
            return self._gen_sec_node(node)
        if ntype == "zone":
            zone = self.zm.zones[node["zone_id"]]
            out = self._maybe_page_break(zone.page)
            if zone.tag == "title" and not self._h1_seen:
                # A "Title" zone (as opposed to "Heading 1") is how the XML
                # profile's zoning habit tags a document/chapter title -
                # confirmed from a real project. core.hierarchy doesn't
                # recognize "title" as a heading tag (core.constants.
                # HEADING_TAGS is h1..h6 only, shared/untouched code), so it
                # never reaches _gen_sec_node on its own; the FIRST such
                # zone gets the exact same <header><h1 epub_type="title">
                # treatment as a real h1 would, so it can never end up as a
                # bare, invalid <title> element loose in body content
                # (which collides with <head>/<title> and isn't valid
                # anywhere else in XHTML).
                page = self.pdf.get_page(zone.page)
                content = normalize_title_text(
                    text_extractor.extract_zone_formatted_text_with_breaks(page, zone, self._hyphen_keep_at(zone)))
                out.append(self._build_document_header(content, zone.zone_id))
            else:
                out.extend(self._resolve_zone_to_elements(zone))
            return out
        if ntype == "merged_p":
            # hierarchy.py only ever builds a "merged_p" node when the
            # chain's tag IS in TEXT_MERGE_TAGS (core/constants.py) - never
            # reached for EPUB's own tag choices (e.g. "para"), but IS
            # reached for CUPEPUB's "Para" (CUPLookup translates it to the
            # literal string "p", which happens to match). An explicit
            # Merge Previous must actually COMBINE the chain's text into
            # ONE element (spec 98.16-98.21) - previously this emitted
            # every zone in the chain independently regardless, which is
            # why Merge Previous appeared to do nothing to the generated
            # output.
            zone_ids = [zid for zid in node["zone_ids"] if zid in self.zm.zones]
            if not zone_ids:
                return []
            out = self._maybe_page_break(self.zm.zones[zone_ids[0]].page)
            first_tag = self.zm.zones[zone_ids[0]].tag
            if first_tag in self._NOTE_TAGS:
                # A footnote/endnote split across a page break (spec
                # sections 23/24/56: "Patterns of Con-" continuing as
                # "tention, Rodriguez..." must stay footnote 1, never
                # become a new footnote 5) - combined via its own
                # dedicated renderer (ONE counter increment, ONE <li>
                # target for the whole chain), never the generic
                # _gen_merged_text_zone (which has no notion of a
                # footnote's own numbered target/backlink structure).
                merged_footnote_el = self._gen_merged_footnote_zone(zone_ids, first_tag)
                if merged_footnote_el is not None:
                    out.append(merged_footnote_el)
            else:
                merged = self._gen_merged_text_zone(zone_ids)
                out.extend(merged if isinstance(merged, list) else [merged])
            return out
        if ntype == "boxed-text":
            start_zone = self.zm.zones.get(node["start_zone_id"])
            if (start_zone is not None and self.box_container_tags
                    and start_zone.tag == self.box_container_tags[0]):
                return self._gen_box_node(node, start_zone)
            # Default (XML profile's own boxed-text-start/-end): the start
            # and end MARKERS are themselves emitted as ordinary sibling
            # elements (unchanged) flanking their children - never wrapped
            # into one container element. Untouched by this fix.
            out = []
            out.extend(self._maybe_page_break(start_zone.page))
            out.extend(self._resolve_zone_to_elements(start_zone))
            for child in node["children"]:
                out.extend(self._gen_node(child))
            end_zone = self.zm.zones.get(node.get("end_zone_id"))
            if end_zone is not None:
                out.extend(self._resolve_zone_to_elements(end_zone))
            return out
        return []

    def _gen_box_node(self, node, start_zone):
        """CUPEPUB's Box_Start/Box_End (spec: "structural zoning markers
        only - MUST NOT appear as final XHTML elements"): builds ONE real
        <box> element from the Start zone's own attributes (e.g. a
        configured "type"), with every zone between Start and End
        (core/hierarchy.py's existing reading-order/stack-based grouping -
        see build_document_tree's extra_container_markers) recursively
        generated as ITS CHILDREN. Neither Start nor End zone is ever
        itself resolved into an element - _resolve_zone_to_elements is
        deliberately never called for them here, unlike the default
        boxed-text branch above. Mapping.xml's own //box[child::boxtitle]
        rule (which needs exactly this shape: a real <box> containing a
        <boxtitle> child) then transforms it - no hardcoded final XHTML
        structure is built here, only the correct INTERMEDIATE tree."""
        box_el = etree.Element("box")
        box_el.set(SRC_ATTR, start_zone.zone_id)
        for key, value in start_zone.attributes.items():
            if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
                continue
            box_el.set(key, value)
        out = self._maybe_page_break(start_zone.page)
        for child in node["children"]:
            for child_el in self._gen_node(child):
                box_el.append(child_el)
        out.append(box_el)
        return out

    def _gen_sec_node(self, node) -> list:
        """A heading (h1..h6). Verified against real production output
        (two different books): NOT renamed to a <sec1>/<sec2> element - the
        heading survives as a literal <h{level}>, and the nesting instead
        comes from a wrapper:
          - the document's own FIRST h1 -> <header><h1 id="{prefix}_1"
            class="{...per component_type...}" epub_type="title">...</h1>
            </header>, matching every real chapter/frontmatter/backmatter
            title sample seen.
          - every other heading (h2..h6, or a later stray h1) ->
            <section aria-labelledby="{id}"><h{level} id="{id}">...</h{level}>
            ...absorbed children...</section>, matching every real
            sub-section sample seen (e.g. NOTES/WORKS CITED sections)."""
        zone_ids = node.get("merged_zone_ids") or [node["zone_id"]]
        first_zone = self.zm.zones[zone_ids[0]]
        level = node["level"]
        out = self._maybe_page_break(first_zone.page)

        heading_parts = []
        for zid in zone_ids:
            z = self.zm.zones[zid]
            page = self.pdf.get_page(z.page)
            content = normalize_title_text(
                text_extractor.extract_zone_formatted_text_with_breaks(page, z, self._hyphen_keep_at(z)))
            if content:
                heading_parts.append(content)
        heading_content = "<break/>".join(heading_parts)
        is_document_title = level == 1 and not self._h1_seen
        # <h1> belongs to the document title only. A heading inside the body
        # is written one level down (H1 zone -> <h2>, H2 -> <h3>, ... max
        # h6), inside its own <section>.
        tag = "h1" if is_document_title else f"h{min(level + 1, 6)}"
        h_el = _parse_inline(f"<{tag}>{heading_content}</{tag}>") if heading_content else etree.Element(tag)
        h_el.set(SRC_ATTR, first_zone.zone_id)
        if is_document_title:
            self._h1_seen = True  # set BEFORE the children run, so none of them becomes a 2nd title
        else:
            # Numbered in reading order (a section's own number is taken
            # BEFORE its nested sub-sections are generated).
            self._sec_counter += 1
            sec_id = f"{self.prefix}_sec{self._sec_counter}"

        children_out = []
        for child in node["children"]:
            children_out.extend(self._gen_node(child))

        if is_document_title:
            h_el.set("id", f"{self.prefix}_1")
            h_el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
            h_el.set("epub_type", "title")
            header = etree.Element("header")
            header.append(h_el)
            self._pending_header = header
            out.append(header)
            for c in children_out:
                self._place_into(out, c)
        else:
            h_el.set("id", sec_id)
            section = etree.Element("section", **{"aria-labelledby": sec_id})
            section.append(h_el)
            for c in children_out:
                section.append(c)
            out.append(section)
        return out

    @staticmethod
    def _place_into(out_list, el):
        out_list.append(el)

    def _build_document_header(self, content, src_zone_id):
        """Shared by both routes into "this zone IS the document/chapter
        title": a real h1 zone (_gen_sec_node) and a "Title"-tagged zone
        (_gen_node's "zone" branch, above) - same <header><h1 id=
        "{prefix}_1" class="..." epub_type="title">...</h1></header> shape
        either way, and either way self._pending_header is left set so a
        following "Subtitle" zone can still absorb into it (see generate())."""
        h_el = _parse_inline(f"<h1>{content}</h1>") if content else etree.Element("h1")
        h_el.set(SRC_ATTR, src_zone_id)
        h_el.set("id", f"{self.prefix}_1")
        h_el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
        h_el.set("epub_type", "title")
        header = etree.Element("header")
        header.append(h_el)
        self._h1_seen = True
        self._pending_header = header
        return header

    def _absorb_subtitle(self, zone):
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        h_el = _parse_inline(f"<h2>{content}</h2>") if content else etree.Element("h2")
        h_el.set(SRC_ATTR, zone.zone_id)
        h_el.set("class", "subtitle")
        h_el.set("epub_type", "subtitle")
        self._pending_header.append(h_el)

    def _build_note_section(self, elements, section_class, item_epub_type):
        """elements are already-complete <li> targets (id, backlink and all
        - built by _gen_footnote_zone at the reference's own reading-order
        position) - this only wraps them in the collected end-of-document
        <section><ol> container. The section's own id is left for
        assign_missing_ids (main_window.generate_xhtml) to assign, same as
        every other bare <section> this generator produces."""
        section = etree.Element("section", **{"class": section_class, "epub_type": section_class})
        ol = etree.SubElement(section, "ol", **{"class": "decimal"})
        for li in elements:
            ol.append(li)
        return section

    def _wrap_note_groups(self, root, tag):
        """Groups consecutive runs of "__note_li__" placeholders (built by
        _gen_footnote_zone/_gen_merged_footnote_zone for tag, currently
        only ever "en") into separate <section class="endnotes"><ol>
        groups (spec Part 10 - "endnote group structure"), each
        independently numbered from 1. Checked under every element in the
        tree (not just root's own direct children - mirrors
        _wrap_consecutive's own reasoning), because whether a run of
        top-level "en" zones ends up as root's own direct children or
        nested one level inside a real heading's own <section> wrapper
        depends entirely on whether that heading's tag is one
        core.hierarchy recognizes (h1-h6) - a project whose own subsection
        headings use a different tag (e.g. a dedicated "EnHead"/"enhead"
        tag, confirmed from a real production sample) never gets that
        wrapper at all, and its "en" zones stay flat siblings of root
        instead. Both shapes must group correctly, without assuming either
        one.

        A run tolerates page-break <span role="doc-pagebreak"> markers
        WITHOUT treating them as a group boundary (spec Part 10.4.1:
        "an endnote group may continue across multiple PDF pages... do
        NOT split a group merely because the page number changes") - they
        are preserved, just relocated to sit immediately before the
        group's own <ol> instead of interior to it (a <span> can never be
        a valid direct child of <ol> - only <li> can). Only a REAL other
        content element - a heading (whatever its own tag), a paragraph,
        anything that is not this tag's own note or a bare pagebreak
        marker - ends a run and starts a new logical group.

        This groups PURELY by structural adjacency in the already-correct
        reading-order stream doc_tree/generate() produced - never by
        page number, zone id, or object creation order (spec Part 10.4/
        14/18) - so it works identically regardless of which raw tag a
        given project uses for its own endnote-group headings."""

        def is_note(el):
            return el.tag == "__note_li__" and el.get("_note_tag") == tag

        def is_pagebreak(el):
            return el.tag == "span" and el.get("role") == "doc-pagebreak"

        section_class, item_epub_type, _id_token, _ref_token = self._NOTE_TAGS[tag]
        for parent in list(root.iter()):
            children = list(parent)
            i = 0
            while i < len(children):
                if not is_note(children[i]):
                    i += 1
                    continue
                run = [children[i]]
                interior_pagebreaks = []
                j = i + 1
                while j < len(children):
                    if is_note(children[j]):
                        run.append(children[j])
                        j += 1
                    elif is_pagebreak(children[j]) and j + 1 < len(children) and is_note(children[j + 1]):
                        interior_pagebreaks.append((children[j], run[-1]))
                        j += 1
                    else:
                        break

                # Local numbering + duplicate-visible-number stripping, now
                # that this group's own membership/order is finally known.
                for local_n, li in enumerate(run, start=1):
                    backlink = li.find(".//a[@role='doc-backlink']")
                    if backlink is not None:
                        backlink.text = str(local_n)
                    backlink_sup = backlink.getparent() if backlink is not None else None
                    if backlink_sup is not None and backlink_sup.tail:
                        backlink_sup.tail = self._strip_leading_source_number(backlink_sup.tail, local_n)

                idx = children.index(run[0])
                for pb, note_before in interior_pagebreaks:
                    # The page break stays where it is in the book: at the
                    # end of the note it follows (a <span> is valid inside
                    # <li>). It used to be moved in front of the whole
                    # <section class="endnotes">, i.e. to the TOP of the
                    # notes list, far from the page it marks.
                    parent.remove(pb)
                    note_before.append(pb)
                for el in run:
                    parent.remove(el)
                    el.tag = "li"
                    el.attrib.pop("_note_tag", None)
                section = self._build_note_section(run, section_class, item_epub_type)

                insert_at = idx
                parent.insert(insert_at, section)

                children = list(parent)
                i = insert_at + 1

    def _build_index_hierarchy(self, items):
        """Builds ONE real nested list from the flat, reading-order (level,
        zone) list collected in _gen_zone: level 1 (Index Primary) terms are
        top-level <li>, level 2 (Index Secondary) nests as a new <ul> INSIDE
        the most recent level-1 <li>, level 3 (Index Territory) nests the
        same way inside the most recent level-2 <li>. A level-2/3 term
        encountered with no open parent at the right level (e.g. Territory
        right after Primary, no Secondary in between) degrades gracefully by
        attaching to whatever's currently open, never raises/crashes."""
        root_ul = etree.Element("ul", **{"class": "none", "epub_type": "index-entry-list"})
        stack = []  # [(level, li_element)], innermost open term last
        for level, zone in items:
            if level is None:
                # A page-break marker (see _gen_zone): stays at its own
                # position - at the end of the entry it follows.
                if stack:
                    stack[-1][1].append(zone)
                elif len(root_ul):
                    root_ul[-1].append(zone)
                continue
            page = self.pdf.get_page(zone.page)
            content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
            self._index_counter += 1
            li = etree.Element("li", **{"epub_type": "index-entry", "id": f"{self.prefix}_ind{self._index_counter}"})
            li.set(SRC_ATTR, zone.zone_id)
            if content:
                inline = _parse_inline(f"<x>{content}</x>")
                li.text = inline.text
                for c in inline:
                    li.append(c)

            while stack and stack[-1][0] >= level:
                stack.pop()
            if not stack:
                root_ul.append(li)
            else:
                parent_li = stack[-1][1]
                nested_ul = parent_li.find("ul")
                if nested_ul is None:
                    nested_ul = etree.SubElement(parent_li, "ul")
                nested_ul.append(li)
            stack.append((level, li))
        return root_ul

    @staticmethod
    def _wrap_consecutive(root, predicate, wrapper_tag, wrapper_attrs, retag=None):
        """Wraps every maximal run of CONSECUTIVE SIBLING children matching
        predicate() in one new wrapper element, in place - checked under
        every element in the tree (not just root's own direct children),
        since a "ref"/"indexentry" zone could in principle end up nested
        inside a heading section rather than at the document's top level.
        Used for the "ref"/"indexentry" placeholder tags below (built as
        __ref_li__/__index_li__ so this pass can find them unambiguously,
        then retagged to their real "li" name once wrapped). Mirrors
        core/mapping_engine.py's own _apply_enclose algorithm."""
        for parent in list(root.iter()):
            children = list(parent)
            i = 0
            while i < len(children):
                if predicate(children[i]):
                    j = i
                    while j < len(children):
                        if predicate(children[j]):
                            j += 1
                        elif (children[j].tag == "span" and children[j].get("role") == "doc-pagebreak"
                              and j + 1 < len(children) and predicate(children[j + 1])):
                            j += 1  # page break BETWEEN two items: kept inside the list (see below)
                        else:
                            break
                    run = children[i:j]
                    wrapper = etree.Element(wrapper_tag, **wrapper_attrs)
                    idx = list(parent).index(run[0])
                    for el in run:
                        parent.remove(el)
                        if not predicate(el):
                            wrapper[-1].append(el)  # page break -> end of the item it follows
                            continue
                        if retag:
                            el.tag = retag
                        wrapper.append(el)
                    parent.insert(idx, wrapper)
                    children = list(parent)
                    i = idx + 1
                else:
                    i += 1

    @staticmethod
    def _page_break_span(page):
        return etree.Element(
            "span", id=f"page_{page}", role="doc-pagebreak",
            **{"aria-label": str(page), "epub_type": "pagebreak"},
        )

    # ------------------------------------------------------------- zone -> element
    def _is_split_parent(self, zone) -> bool:
        """Based on gui/zone_panel.py ZoneTreePanel._zone_status and
        core/xml_generator.py's own flatten logic (mirrored here, not
        imported, since both live methods are bound to unrelated internal
        state) - a zone whose children are ALL split pieces of itself is
        superseded by them and must never also emit its own element.

        Deliberately DOES NOT also require c.tag == zone.tag the way those
        two do: this is a real, expected EPUB workflow - draw one zone over
        an "Alarcón, Pedro Antonio de / El Niño de la Bola, 20 / El Sombrero
        de tres Picos, 20-1" index block, Horizontal Split it into one piece
        per line, then retag the first piece Index Primary and the rest
        Index Secondary/Territory individually. Requiring every split piece
        to keep the SAME tag as its parent (as the XML profile's own check
        does, since IT never needs pieces to diverge) would make the
        original, now-superseded zone reappear as "still active" the moment
        any one piece's tag differs from the others - producing the exact
        kind of duplicate content the split-parent check exists to prevent
        in the first place. Only is_split + source_zone_id (i.e. "this IS a
        piece of THIS split") are checked here."""
        if not zone.children:
            return False
        children = [self.zm.zones[c] for c in zone.children if c in self.zm.zones]
        return bool(children) and all(
            c.is_split and c.source_zone_id == zone.zone_id for c in children)

    def _resolve_zone_to_elements(self, zone) -> list:
        """Only ACTIVE/leaf zones are ever exported - a SPLIT-PARENT zone
        (see _is_split_parent) is skipped entirely; its split children take
        its EXACT position in the stream instead (as siblings, not nested
        inside a surviving parent element), recursively, since a split
        piece can itself be split again. Matches xml_generator.py's
        documented, tested behavior for the XML profile exactly.

        _gen_zone normally returns ONE element, but for a figure/image zone
        whose containment children turn out to hold more than one
        independently-numbered image+caption pairing (see _gen_zone's own
        docstring), it returns a LIST of separate, independent elements
        instead - one per real pairing, never one combined element - and
        that list is used here directly rather than wrapped as a single
        result."""
        if self._is_split_parent(zone):
            out = []
            for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
                child = self.zm.zones.get(cid)
                if child is not None:
                    out.extend(self._resolve_zone_to_elements(child))
            return out
        el = self._gen_zone(zone)
        if isinstance(el, list):
            return el
        return [el] if el is not None else []

    def _gen_zone(self, zone):
        kind = zone.attributes.get("asset_kind")
        if zone.tag in self._index_tag_to_level:
            # Collected for _build_index_hierarchy at the end of generate() -
            # never placed at this position directly (a secondary/territory
            # term belongs NESTED under its primary/secondary parent, not
            # here in flat reading-order position). Originally assumed a
            # hierarchy zone never has real containment children of its own
            # (manually zoned index content historically relied ENTIRELY on
            # tag-implied level + reading-order position, never zone.
            # parent_id) - auto_zoning/index_auto_zone.py's Auto Zone Index
            # feature is the first caller that DOES create real parent_id
            # links for Secondary/Territory children (spec: "create parent
            # relationships... Reading Order and Level are separate
            # properties"), so those children must still be visited and
            # collected here too, or they silently vanish from the output
            # entirely (confirmed: _build_index_hierarchy never even learns
            # they exist otherwise). Each child is ALSO index-hierarchy-
            # tagged (the only kind Auto Zone Index ever nests this way), so
            # recursing back into this same branch for it - never appending
            # a real element for it here (there is nowhere to append one:
            # this zone itself produces no element, see `return None`
            # below).
            self._index_hierarchy_items.append((self._index_tag_to_level[zone.tag], zone))
            for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
                child_zone = self.zm.zones.get(cid)
                if child_zone is not None:
                    self._resolve_zone_to_elements(child_zone)
            return None
        if self.pagenum_cup_name and zone.attributes.get("cup_name") == self.pagenum_cup_name:
            el = self._gen_pagenum_zone(zone)
            if el is not None and self._index_hierarchy_items:
                # Inside an index: the index list is assembled at the end of
                # generate() (_build_index_hierarchy), so a page break placed
                # here in the flow would end up BEFORE the whole index. Keep
                # it in the index stream instead, after the entry it follows.
                self._index_hierarchy_items.append((None, el))
                return None
        elif zone.tag in self._NOTE_TAGS:
            el = self._gen_footnote_zone(zone, zone.tag)
        elif kind:
            el = self._gen_image_zone(zone, kind)
        elif zone.tag == "ref":
            el = self._gen_ref_zone(zone)
        elif zone.tag == "indexentry":
            el = self._gen_index_entry_zone(zone)
        elif zone.tag == "table":
            el = self._gen_table_zone(zone)
        else:
            el = self._gen_text_zone(zone)
        if el is None:
            return None
        child_ids = reading_order.sort_children_ids(self.zm, zone.zone_id)
        if kind:
            split_elements = self._split_combined_figure_children(zone, child_ids, el)
            if split_elements is not None:
                return split_elements
        if child_ids and self._is_inline_list_item(zone):
            # A list item zone with nested inline zones (italic title,
            # superscript, small image ...): _gen_text_zone already spliced
            # them into the item's own text, exactly like a Paragraph.
            return el
        if zone.tag == "p" and child_ids:
            # Nested/inline zone support (spec: "NESTED ZONE / INLINE ZONE
            # SUPPORT") - _gen_text_zone already spliced every one of this
            # Paragraph's own geometrically-nested children INLINE into
            # its own text (or the correct one of several split
            # paragraphs' own text - spec: "MULTIPLE PARAGRAPHS PER
            # ZONE") at their exact position - appending them AGAIN here
            # as trailing structural XML children would duplicate them.
            # `el` may be a single element or (multiple detected
            # paragraphs) a list - either way it's already exactly what
            # this zone should contribute, unchanged. Scoped to "p" only:
            # every OTHER tag with real structural children (list,
            # title-group, boxed-text, ...) keeps its EXISTING, unchanged
            # behavior via the loop below.
            return el
        for cid in child_ids:
            child_zone = self.zm.zones.get(cid)
            if child_zone is None:
                continue
            for child_el in self._resolve_zone_to_elements(child_zone):
                el.append(child_el)
        return el

    def _split_combined_figure_children(self, zone, child_ids, outer_el):
        """Real, confirmed structural defect this guards against (spec:
        "FIX FIGURE ORDER + FIGURE CROSS-LINKS" - "Multiple independently
        numbered figures must not be incorrectly combined into one logical
        figure"): this generator's own containment recursion (the loop just
        above) has no limit on how many children a figure/image zone can
        have - if upstream zone containment (parent_id, assigned outside
        this file) ever nests a SECOND, independently-drawn, independently-
        numbered image (itself asset_kind-truthy) under ONE outer
        figure-tagged zone, this method previously combined it (plus its own
        caption) into the outer zone's own <figure> element as if it were
        just more content. No later post-processing pass can undo that once
        it has already happened - the fix has to live here, at generation
        time.

        Returns None (the overwhelmingly common case: no nested image-kind
        child at all) - the caller's own normal single-element path handles
        it unchanged, zero behavior change. Returns a LIST of independent
        elements - the outer zone's OWN already-built `outer_el` (never
        discarded - it is the first, real, independently-numbered image)
        plus one freshly-generated element per nested image-kind child -
        when at least one nested image-kind child is found: `outer_el`
        absorbs every child up to (not including) the first nested
        image-kind child (its own caption, if drawn before the image in the
        stream); each subsequent group starts at a nested image-kind child
        and absorbs every immediately-following non-image-kind child (its
        own caption) up to (not including) the next image-kind child - so
        every real caption stays paired with its own real image and nothing
        is ever dropped. A warning is recorded via the existing
        self.warnings mechanism so the anomaly is surfaced rather than
        silently produced."""
        groups = [[]]
        for cid in child_ids:
            child = self.zm.zones.get(cid)
            if child is not None and child.attributes.get("asset_kind"):
                groups.append([cid])
            else:
                groups[-1].append(cid)
        if len(groups) == 1:
            return None

        self.warnings.append(
            f"Zone {zone.zone_id} contains {len(groups)} independently-numbered images - "
            f"generated as {len(groups)} separate figures instead of one combined figure.")

        def _fill(target_el, group_child_ids):
            for cid in group_child_ids:
                child_zone = self.zm.zones.get(cid)
                if child_zone is None:
                    continue
                for child_el in self._resolve_zone_to_elements(child_zone):
                    target_el.append(child_el)

        _fill(outer_el, groups[0])
        elements = [outer_el]
        for group in groups[1:]:
            leading_zone = self.zm.zones[group[0]]
            group_el = self._gen_image_zone(leading_zone, leading_zone.attributes.get("asset_kind"))
            if group_el is None:
                continue
            _fill(group_el, group[1:])
            elements.append(group_el)
        return elements

    def _gen_text_zone(self, zone):
        """The generic fallback for any zone not handled by a dedicated
        _gen_*_zone method (pagenum/footnote/image/ref/indexentry) -
        covers ordinary text-bearing tags (p, title, reference, ...) AND
        container tags with real geometric children (list, table, ...),
        since neither this profile nor Mapping.xml's own tag vocabulary
        needs a separate per-container-tag method the way _gen_image_zone/
        _gen_footnote_zone do.

        A zone WITH children never re-extracts its own bbox text (real,
        confirmed bug otherwise: a List/Table container's own bbox
        typically geometrically SPANS its own children's text regions, so
        re-extracting from it duplicates whatever the children
        independently produce a moment later in _gen_zone's own child-
        appending loop - confirmed directly: a List zone with 2 real
        List-Item children produced "<ul>Item one. Item two.<li>Item
        one.</li><li>Item two.</li></ul>", the flat text and the <li>
        children both present, before this guard existed). Matches
        core.xml_generator.py's own established pattern for the exact
        same situation (its _zone_list/_gen_zone_list_grouped already
        check `if children:` before ever calling extract_zone_
        formatted_text) - this generalizes that same, already-correct
        rule to every tag here instead of only "list".

        EXCEPT for "p" specifically, handled entirely separately below
        (spec: "NESTED ZONE / INLINE ZONE SUPPORT" + "MULTIPLE PARAGRAPHS
        PER ZONE"). Every other tag's existing all-or-nothing content=""
        behavior is completely unchanged."""
        page = self.pdf.get_page(zone.page)
        if zone.tag == "p":
            contents = self._p_paragraph_contents(zone)
            elements = []
            for content in contents:
                p_el = _parse_inline(f"<p>{content}</p>") if content else etree.Element("p")
                p_el.set(SRC_ATTR, zone.zone_id)
                for key, value in zone.attributes.items():
                    if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
                        continue
                    p_el.set(key, value)
                elements.append(p_el)
            if not elements:
                p_el = etree.Element("p")
                p_el.set(SRC_ATTR, zone.zone_id)
                return p_el
            return elements if len(elements) > 1 else elements[0]

        if zone.children and self._is_inline_list_item(zone):
            content = text_extractor.extract_formatted_text_with_nested_children(
                page, zone.bbox, self._inline_children_markup(zone, page), self._hyphen_keep_at(zone))
        elif zone.children:
            content = ""
        elif zone.tag == "title":
            # Same break-joining behavior as the XML profile's _zone_title
            # (title + byline lines joined with <break/>, not a space).
            content = normalize_title_text(
                text_extractor.extract_zone_formatted_text_with_breaks(page, zone, self._hyphen_keep_at(zone)))
        else:
            content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        el = _parse_inline(f"<{zone.tag}>{content}</{zone.tag}>") if content else etree.Element(zone.tag)
        el.set(SRC_ATTR, zone.zone_id)
        for key, value in zone.attributes.items():
            if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
                continue
            el.set(key, value)
        return el

    def _is_inline_list_item(self, zone) -> bool:
        """A list-item zone (NumList/BullList/... - see core/epub_lists.py)
        whose children are inline pieces of its own text, not further list
        items (a list zone drawn AROUND its items is a container instead)."""
        if epub_lists.item_kind(etree.Element(zone.tag)) is None or not zone.children:
            return False
        for cid in zone.children:
            child = self.zm.zones.get(cid)
            if child is not None and (epub_lists.item_kind(etree.Element(child.tag)) is not None
                                      or child.tag == epub_lists.LIST_PARA_TAG):
                return False
        return True

    def _inline_children_markup(self, zone, page) -> list:
        children = []
        for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
            child = self.zm.zones.get(cid)
            if child is None:
                continue
            kind = child.attributes.get("asset_kind")
            if kind:
                child_el = self._gen_image_zone(child, kind)
                markup = etree.tostring(child_el, encoding="unicode") if child_el is not None else ""
            else:
                markup = text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
            if markup:
                children.append((child.bbox, markup))
        return children

    def _p_paragraph_contents(self, zone) -> list:
        """One content string per real PDF paragraph inside a "p" zone,
        with the zone's own nested inline children spliced in at their
        position (moved verbatim from _gen_text_zone) - shared by
        _gen_text_zone (a single zone) and _gen_merged_text_zone (a Merge
        Previous chain), so a zone holding several paragraphs keeps its
        paragraph boundaries either way."""
        page = self.pdf.get_page(zone.page)
        if True:  # original indentation kept so the moved block is unchanged
            # A Paragraph's own geometrically-nested children (spec:
            # "NESTED ZONE / INLINE ZONE SUPPORT") are a real, confirmed,
            # DIFFERENT situation from List/Table's structural children -
            # they represent an inline unit (a small nested image, or a
            # run of distinctly-formatted text) meant to sit INSIDE the
            # paragraph's own running text at its exact position, never a
            # separate block sibling:
            #   - an image-kind child (asset_kind set) is rendered via the
            #     EXISTING, unchanged _gen_image_zone - the same method a
            #     top-level inline image already uses - then serialized.
            #   - any other child has its own bbox extracted directly via
            #     the EXISTING, unchanged extract_zone_formatted_text.
            # text_extractor.extract_zone_paragraphs then buckets each
            # (child_bbox, markup) pair into whichever geometrically
            # detected PDF paragraph its own bbox falls into (spec:
            # "MULTIPLE PDF PARAGRAPHS INSIDE ONE SAVED ZONE" - a single
            # drawn zoning box can legitimately span several real PDF
            # paragraphs) - returning exactly one string for the
            # overwhelming common single-paragraph case (identical output
            # to before this existed), or one string per genuinely
            # detected paragraph otherwise. _gen_zone's own generic
            # child-appending loop is skipped for "p" (see there), so
            # this is the ONLY place these children are ever emitted,
            # never duplicated.
            children = []
            for cid in reading_order.sort_children_ids(self.zm, zone.zone_id):
                child = self.zm.zones.get(cid)
                if child is None:
                    continue
                child_kind = child.attributes.get("asset_kind")
                if child_kind:
                    child_el = self._gen_image_zone(child, child_kind)
                    markup = etree.tostring(child_el, encoding="unicode") if child_el is not None else ""
                elif child.tag == "en":
                    # ENDNOTE XEN ORPHAN/WRONG-PLACEMENT FIX: a nested "en"
                    # child is a genuine in-text citation marker at its own
                    # exact position - see _gen_endnote_marker's own
                    # docstring. Falls back to ordinary plain-text
                    # extraction (never a fabricated marker) when the
                    # zone's own text isn't cleanly just a reference number.
                    marker_markup = self._gen_endnote_marker(page, child)
                    markup = marker_markup if marker_markup is not None else \
                        text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
                else:
                    markup = text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
                if markup:
                    children.append((child.bbox, markup))
            return text_extractor.extract_zone_paragraphs(page, zone, children, self._hyphen_keep_at(zone))

    def _gen_endnote_marker(self, page, child):
        """Spec: "ENDNOTE XEN ORPHAN/WRONG-PLACEMENT FIX" - builds the
        genuine in-text citation marker
        (<sup><a id="{prefix}_xen{n}" href="#{prefix}_en{n}">{n}</a></sup>)
        for a footnote/endnote-tagged zone found NESTED inside a
        paragraph (i.e. auto-parented at its own true citation position in
        the running text, via the same containment mechanism an inline
        image/formatted-run child already uses) - never for a top-level
        "en" zone, which is always the endnote's own CONTENT, not a
        marker (see _gen_footnote_zone's own docstring for why those two
        are structurally different things that must never be conflated).

        The marker's own reference number is read directly from the
        zone's own already-recognized text - never invented, never
        assigned from a running counter - so it links to EXACTLY the
        {prefix}_en{n} id _gen_footnote_zone already assigns that same
        endnote's own content by construction (spec: "Use the existing
        endnote numbering/mapping... do NOT renumber"). Returns None
        (never a fabricated marker) unless the zone's own text is
        cleanly JUST a reference number - a real source marker, not a
        zone that merely happens to contain a digit somewhere - matching
        this fix's own "verify its reference number... do not create any
        xen if no actual source marker exists" requirement exactly."""
        _section_class, _item_epub_type, id_token, ref_token = self._NOTE_TAGS["en"]
        text = text_extractor.extract_zone_plain_text(page, child, self._hyphen_keep_at(child))
        m = re.fullmatch(r"\s*(\d+)\s*", text or "")
        if not m:
            return None
        n = m.group(1)
        ref_sup = etree.Element("sup")
        ref_sup.set(SRC_ATTR, child.zone_id)
        ref_a = etree.SubElement(ref_sup, "a", id=f"{self.prefix}_{ref_token}{n}",
                                  href=f"#{self.prefix}_{id_token}{n}")
        ref_a.text = n
        return etree.tostring(ref_sup, encoding="unicode")

    def _gen_pagenum_zone(self, zone):
        """The profile's designated page-marker tag (CUPEPUB's "PageNum" -
        see self.pagenum_cup_name) becomes a pagebreak span built from the
        zone's own zone.text (spec: the PRINTED page label IS the EPUB
        page number - "xiii", "1", "102" - the PDF's physical page index
        is the zone's source LOCATION only and must never enter the id or
        aria-label, and must never be used as a fallback), at its exact
        reading-order position (this runs through the exact same
        _resolve_zone_to_elements/_gen_zone path as every other zone, so
        it lands wherever the zone actually sits in reading order, never
        collected/moved elsewhere). Deliberately bypasses _gen_text_zone/
        Mapping.xml entirely: CUPLookup.xml translates "PageNum" to the
        literal tag "pagenum", which unrelatedly collides with a DIFFERENT
        Mapping.xml rule (exhead|expara|pagenum -> a <sec1> EXERCISE
        wrapper - confirmed by inspection, nothing to do with page
        markers) that would otherwise wrap it in a nonsensical structure.

        Reads zone.text rather than re-extracting from the PDF bbox live
        (unlike _gen_text_zone) - confirmed necessary: ZoneManager.
        set_zone_text (the manual PageNum editor's own Apply action) only
        ever writes zone.text, so re-extracting from the PDF at generation
        time would silently discard every manual correction and always
        regenerate the OCR/extracted (possibly empty or wrong) value
        instead - exactly the scanned-PDF failure mode this exists to fix.
        zone.text already reflects automatic PDF/OCR extraction for a
        digital PDF (ZoneManager._refresh_text populates it at zone-
        creation time) unless/until manually overridden, so this one field
        correctly serves both the digital and scanned/manual-entry cases.

        An empty value (common for a scanned page OCR couldn't read) is
        NOT a fatal error: the pagebreak is simply skipped (this zone
        contributes nothing to the output) and a non-blocking warning is
        recorded in self.warnings - generation of the rest of the document
        continues normally. The zone itself is untouched in the project
        (ZoneTool never deletes/alters it), so the user can fill it in and
        regenerate later.

        id is "page_{value}" (note the underscore before the value -
        "page_1", never "page1") where {value} is the zone's own trimmed
        text, sanitized to valid id characters; a repeated value within one
        document gets a deterministic "_2"/"_3" suffix rather than a
        silently duplicated id.

        CRITICAL (spec: "ZONETOOL - MASTER PRODUCTION FIX & RESTORATION
        PROMPT" sections 40/54): the pagebreak id must NEVER be prefixed
        with the document's own semantic file prefix - "fm6_page_x" and
        "bm3_page_271" are both wrong; only "page_x"/"page_271" are
        correct. The semantic prefix (self.prefix) is a SEPARATE ID
        namespace used everywhere else in this generator (headings,
        sections, index entries, tables) - pagebreaks are the one
        exception, by explicit spec rule, so self.prefix must never appear
        here even though it's readily available on self."""
        content = _close_digit_gap((zone.text or "").strip())
        if not content:
            self.warnings.append(
                f"PageNum on physical PDF page {zone.page} has no printed value and was skipped.")
            return None
        value = _sanitize_id_component(content)
        base_id = f"page_{value}"
        seen = self._pagenum_seen.get(base_id, 0)
        self._pagenum_seen[base_id] = seen + 1
        page_id = base_id if seen == 0 else f"{base_id}_{seen + 1}"
        el = etree.Element(
            "span", id=page_id, role="doc-pagebreak", epub_type="pagebreak",
            **{"aria-label": content},
        )
        el.set(SRC_ATTR, zone.zone_id)
        return el

    def _strip_leading_source_number(self, content, expected_n):
        """Only strips a leading digit run that EXACTLY matches the number
        already being assigned to THIS entry (spec Part 13: "Do not
        blindly delete all leading numbers... determine whether a number
        is source-visible endnote number... using role, zone structure,
        context") - an unrelated leading numeral (a different number, or
        no match at all) is always left completely untouched, never
        guessed at."""
        if not content:
            return content
        m = self._LEADING_NUM_RE.match(content)
        if m and m.group(1) == str(expected_n):
            return content[m.end():]
        return content

    def _note_continuation_chain(self, zone_id):
        """Returns the ordered list of zone_ids that explicitly Merge-
        Previous'd onto zone_id, zone_id's own continuation's own
        continuation, and so on (empty if none) - built from
        self._merge_continuation_of (see generate()'s own comment for why
        this exists: a footnote/endnote zone's explicit merge target can
        be a SPLIT-PIECE CHILD that core.paragraph_merge.
        merge_top_level_stream can only detect and defer, never itself
        resolve, since core.reading_order.flatten_document_order never
        includes a split piece as its own top-level stream entry).
        Guards against a cyclic/self-referential merge_target (never
        possible through the normal GUI action, but a hand-edited project
        file could contain one) with a visited-set, rather than looping
        forever."""
        chain = []
        seen = {zone_id}
        current = zone_id
        while True:
            nxt = self._merge_continuation_of.get(current)
            if nxt is None or nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
            current = nxt
        return chain

    def _gen_footnote_zone(self, zone, tag):
        """A footnote/endnote zone produces the actual note TEXT as a
        fully-formed <li> target (id="{prefix}_fn{n}"/"{prefix}_en{n}",
        with a role="doc-backlink" <a> pointing back to a reference marker).
        target_id/ref_id/href always come from self._note_counters - a
        single running counter per tag, NEVER reset mid-document - because
        the existing xen citation-marker mechanism (_gen_endnote_marker)
        computes its own href purely from a marker's raw source digit plus
        this same counter, so resetting it would break that already-
        working, separately-tested linkage.

        For tag == "fn": appended directly to self._notes["fn"] for
        _build_note_section to collect into ONE whole-document list at the
        end of generate() - completely unchanged, existing behavior,
        including its DISPLAYED number (which is simply n, the id counter,
        since footnotes are never grouped - spec Part 25: keep footnote
        behavior unchanged). This also returns an inline reference marker
        (<sup><a id="{prefix}_xfn{n}" href="#{prefix}_fn{n}">{n}</a></sup>),
        placed at the zone's own exact reading-order position like any
        other element - unchanged, existing behavior.

        For tag == "en": returns the <li> itself (tagged with the internal
        placeholder "__note_li__", never a bare "li" - the SAME "build a
        placeholder, wrap consecutive runs of it later" pattern already
        used for ref/index lists, see _wrap_consecutive), placed INLINE at
        this top-level content zone's own reading-order position (a top-
        level "en" zone - no parent - is ALWAYS the endnote's own CONTENT,
        never a genuine in-text citation marker - see
        _nested_inline_content_for_paragraph's own "en" branch for where a
        REAL citation marker is recognized instead, nested inside a citing
        paragraph). Its DISPLAYED number and any leading source-visible
        duplicate number are intentionally left unset/unstripped here -
        _wrap_note_groups (called once, at the end of generate(), after
        every "en" zone in the whole document has been visited) groups
        consecutive runs of these placeholders (spec Part 10 - "endnote
        group structure": two logical endnote groups, e.g. a PREFACE's own
        notes and a following chapter's own notes, must stay two separate,
        independently-numbered lists, restarting at 1, never one flat
        whole-document list silently merging both with continuous
        numbering) and only THEN knows each entry's correct
        group-local position to number it from.

        Before any of that: if some OTHER zone explicitly Merge-Previous'd
        onto THIS one (self._merge_continuation_of/_note_continuation_chain
        - most commonly a page-2 continuation fragment whose merge target
        is a split-piece child, the one case core.paragraph_merge's own
        top-level chain splicing cannot resolve itself, see generate()'s
        own comment), delegate entirely to _gen_merged_footnote_zone for
        the WHOLE chain instead - reusing its existing text-combining/id/
        backlink logic rather than duplicating it, and guaranteeing the
        continuation's own text lands INSIDE this single <li>, in the
        correct position, never as a separate, independently-numbered
        entry of its own."""
        continuation = self._note_continuation_chain(zone.zone_id)
        if continuation:
            return self._gen_merged_footnote_zone([zone.zone_id] + continuation, tag)

        section_class, item_epub_type, id_token, ref_token = self._NOTE_TAGS[tag]
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        self._note_counters[tag] += 1
        n = self._note_counters[tag]
        target_id = f"{self.prefix}_{id_token}{n}"
        ref_id = f"{self.prefix}_{ref_token}{n}"

        if tag == "fn":
            content = self._strip_leading_source_number(content, n)

        li = etree.Element("li" if tag == "fn" else "__note_li__",
                            **{"class": id_token, "id": target_id, "epub_type": item_epub_type})
        if tag != "fn":
            li.set("_note_tag", tag)
        li.set(SRC_ATTR, zone.zone_id)
        backlink_sup = etree.SubElement(li, "sup")
        backlink = etree.SubElement(backlink_sup, "a", **{
            # No "id" here (Part 11 fix): role + href fully identify and
            # link the backlink already; an extra id on this <a> served no
            # purpose and nothing else in the codebase reads/depends on it
            # (confirmed by search) - role/href/the <li>'s own id are the
            # only load-bearing attributes.
            "role": "doc-backlink", "href": f"#{ref_id}",
        })
        if tag == "fn":
            backlink.text = str(n)
        if content:
            inline = _parse_inline(f"<x>{content}</x>")
            backlink_sup.tail = inline.text
            for c in inline:
                li.append(c)

        if tag == "fn":
            self._notes[tag].append(li)
            ref_sup = etree.Element("sup")
            ref_sup.set(SRC_ATTR, zone.zone_id)
            ref_a = etree.SubElement(ref_sup, "a", id=ref_id, href=f"#{target_id}")
            ref_a.text = str(n)
            return ref_sup

        return li

    def _gen_merged_footnote_zone(self, zone_ids, tag):
        """A footnote/endnote chain that continues across a page break
        (core.paragraph_merge's footnote-to-footnote continuation - spec
        sections 23/24/56) - the SAME single-marker/single-target output
        _gen_footnote_zone produces for one zone, except the <li> target's
        text is the COMBINED text of every zone in the chain (using each
        later zone's own merge_join/hyphen-boundary handling, exactly like
        _gen_merged_text_zone's own text-combining loop - never a second,
        divergent join algorithm) and the counter increments exactly ONCE
        for the whole chain, never once per constituent zone (that
        one-per-zone increment was the actual root cause of "4 real
        footnotes becoming 5" - a continuation fragment silently claiming
        its own number instead of extending footnote 1's).

        Also extends zone_ids with any FURTHER explicit continuation
        chained off the last zone (self._merge_continuation_of/
        _note_continuation_chain - see _gen_footnote_zone's own docstring)
        - a three-or-more-fragment chain where only the last hop is a
        split-piece-target continuation (the one case core.paragraph_merge
        cannot resolve itself) would otherwise still split off its own
        independent entry even after the first hop is correctly combined
        here."""
        continuation = self._note_continuation_chain(zone_ids[-1])
        if continuation:
            zone_ids = list(zone_ids) + continuation

        section_class, item_epub_type, id_token, ref_token = self._NOTE_TAGS[tag]
        combined = ""
        for zid in zone_ids:
            zone = self.zm.zones[zid]
            text = text_extractor.extract_zone_formatted_text(
                self.pdf.get_page(zone.page), zone, self._hyphen_keep_at(zone))
            if not text:
                continue
            if combined:
                combined = text_extractor.join_boundary(
                    combined, text, bool(zone.attributes.get("hyphen_keep_chain_boundary")),
                    zone.attributes.get("merge_join", " "))
            else:
                combined = text

        first_zone = self.zm.zones[zone_ids[0]]
        self._note_counters[tag] += 1
        n = self._note_counters[tag]
        target_id = f"{self.prefix}_{id_token}{n}"
        ref_id = f"{self.prefix}_{ref_token}{n}"

        if tag == "fn":
            combined = self._strip_leading_source_number(combined, n)

        li = etree.Element("li" if tag == "fn" else "__note_li__",
                            **{"class": id_token, "id": target_id, "epub_type": item_epub_type})
        if tag != "fn":
            li.set("_note_tag", tag)
        li.set(SRC_ATTR, first_zone.zone_id)
        backlink_sup = etree.SubElement(li, "sup")
        backlink = etree.SubElement(backlink_sup, "a", **{
            "role": "doc-backlink", "href": f"#{ref_id}",   # no id - Part 11 fix, see _gen_footnote_zone
        })
        if tag == "fn":
            backlink.text = str(n)
        if combined:
            inline = _parse_inline(f"<x>{combined}</x>")
            backlink_sup.tail = inline.text
            for c in inline:
                li.append(c)

        if tag == "fn":
            self._notes[tag].append(li)
            ref_sup = etree.Element("sup")
            ref_sup.set(SRC_ATTR, first_zone.zone_id)
            ref_a = etree.SubElement(ref_sup, "a", id=ref_id, href=f"#{target_id}")
            ref_a.text = str(n)
            return ref_sup

        # "en": same deferred-numbering placeholder as _gen_footnote_zone -
        # _wrap_note_groups assigns the real group-local number later.
        return li

    def _gen_merged_text_zone(self, zone_ids):
        """Explicit Merge Previous / automatic cross-page continuation,
        ACTUALLY combining text (spec 98.16-98.21) - each later zone's own
        extracted text is appended to the running total using THAT zone's
        own merge_join attribute (" " or "", recorded by ZoneManager.
        merge_with_previous; defaults to " " for an automatic continuation,
        which never sets this attribute) as the separator, in chain order.
        Produces exactly ONE output element, using the chain's FIRST zone's
        own tag/attrs - every other zone in the chain is never
        independently emitted (spec 98.21: the old second zone must not
        appear on its own).

        A PageNum zone_id found INSIDE the chain (core.paragraph_merge
        splices one in, in position, whenever it sits between two
        continuing fragments) becomes its own inline <span role=
        doc-pagebreak> marker via the exact same _gen_pagenum_zone this
        module already uses for a standalone PageNum zone - never merged
        text, never dropped (spec 9).

        A footnote/endnote zone_id found INSIDE the chain (core.
        paragraph_merge splices one in the same way, at its own correct
        relative position, whenever it sits between two continuing
        fragments - a real, confirmed bug fixed there: it used to always
        become its own independent top-level element, stranding the
        footnote's own reference marker AFTER the whole merged paragraph
        instead of at its true in-text position) is generated via the
        SAME _gen_footnote_zone/_gen_merged_footnote_zone this module
        already uses for a standalone footnote - never re-implemented
        here, never merged as plain text. Consecutive same-tag footnote
        zone_ids (the footnote's OWN text itself continued across a page
        while it happened to be embedded here) are grouped and passed to
        _gen_merged_footnote_zone TOGETHER, so the note-counter still
        increments exactly once per logical footnote, never once per
        constituent zone (the same "4 real footnotes becoming 5" bug
        class _gen_merged_footnote_zone's own docstring documents)."""
        first_zone = self.zm.zones[zone_ids[0]]
        # MULTIPLE PARAGRAPHS + MERGE PREVIOUS: each chain zone contributes
        # one content string per real PDF paragraph it holds (a "p" zone may
        # hold several - see _p_paragraph_contents). Merge Previous means
        # "this zone CONTINUES the previous one", so only the boundary
        # between the previous zone's LAST paragraph and this zone's FIRST
        # paragraph is joined (A1 | A2+B1 | B2); every other real paragraph
        # boundary survives as its own element - never flattened into one
        # <p>, never reordered. A single-paragraph chain (the common case)
        # produces exactly one element, identical to before.
        paragraphs = []  # [[source_zone_id, content], ...] in chain order

        def _append_inline(markup):
            if paragraphs:
                paragraphs[-1][1] += markup
            else:
                paragraphs.append([first_zone.zone_id, markup])

        i, n = 0, len(zone_ids)
        while i < n:
            zid = zone_ids[i]
            zone = self.zm.zones[zid]
            if self.pagenum_cup_name and zone.attributes.get("cup_name") == self.pagenum_cup_name:
                pagebreak_el = self._gen_pagenum_zone(zone)
                if pagebreak_el is not None:
                    _append_inline(etree.tostring(pagebreak_el, encoding="unicode"))
                i += 1
                continue
            if zone.tag in self._NOTE_TAGS:
                group = [zid]
                j = i + 1
                while j < n and self.zm.zones[zone_ids[j]].tag == zone.tag:
                    group.append(zone_ids[j])
                    j += 1
                ref_sup = self._gen_footnote_zone(zone, zone.tag) if len(group) == 1 \
                    else self._gen_merged_footnote_zone(group, zone.tag)
                if ref_sup is not None:
                    _append_inline(etree.tostring(ref_sup, encoding="unicode"))
                i = j
                continue
            if zone.tag == "p" and first_zone.tag == "p" and not zone.children:
                # children-free zones only - a merge chain has never rendered
                # a zone's nested inline children (no asset side effects here)
                parts = [c for c in text_extractor.extract_zone_paragraphs(
                    self.pdf.get_page(zone.page), zone, [], self._hyphen_keep_at(zone)) if c]
            else:
                text = text_extractor.extract_zone_formatted_text(
                    self.pdf.get_page(zone.page), zone, self._hyphen_keep_at(zone))
                parts = [text] if text else []
            if not parts:
                i += 1
                continue
            if paragraphs and paragraphs[-1][1]:
                # Hyphenated Line-Break Text Normalization (spec sections
                # 13/14/20): a genuine line-break hyphen sitting exactly at
                # this merge-chain boundary (e.g. "inter-" | "national")
                # must dehyphenate the SAME way a within-zone one does,
                # not fall through to the chain's own plain merge_join
                # separator - text_extractor.join_boundary is the single
                # place that decides between the two, using this zone's
                # own "keep this specific boundary's hyphen" override
                # (hyphen_keep_chain_boundary, set only via gui.dialogs.
                # HyphenReviewDialog's chain-boundary candidates).
                paragraphs[-1][1] = text_extractor.join_boundary(
                    paragraphs[-1][1], parts[0], bool(zone.attributes.get("hyphen_keep_chain_boundary")),
                    zone.attributes.get("merge_join", " "))
            elif paragraphs:
                paragraphs[-1][1] = parts[0]
            else:
                paragraphs.append([zid, parts[0]])
            for extra in parts[1:]:
                paragraphs.append([zid, extra])
            i += 1
        tag = first_zone.tag
        if not paragraphs:
            paragraphs = [[first_zone.zone_id, ""]]
        elements = []
        for index, (src_id, content) in enumerate(paragraphs):
            el = _parse_inline(f"<{tag}>{content}</{tag}>") if content else etree.Element(tag)
            el.set(SRC_ATTR, first_zone.zone_id if index == 0 else src_id)
            for key, value in first_zone.attributes.items():
                if key in _INTERNAL_ATTR_KEYS or not isinstance(value, str):
                    continue
                el.set(key, value)
            elements.append(el)
        return elements[0] if len(elements) == 1 else elements

    def _gen_ref_zone(self, zone):
        """"Reference (Plain)" - a bibliography entry with NO numbered/
        author-date styling (contrast core/mapping_engine.py's ref_n/ref_d
        handling, which IS Mapping.xml-driven and separately verified
        against a different real book). Verified against a real sample:
        <li class="biblioentry" epub:type="biblioref"><span class="reflabel"
        id="{prefix}_refN"/>entry text</li>, with consecutive entries
        wrapped in <ol class="none"> by _wrap_consecutive (generate())."""
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        self._ref_counter += 1
        ref_id = f"{self.prefix}_ref{self._ref_counter}"
        el = etree.Element("__ref_li__", **{"class": "biblioentry", "epub_type": "biblioref"})
        el.set(SRC_ATTR, zone.zone_id)
        label = etree.SubElement(el, "span", **{"class": "reflabel", "id": ref_id})
        if content:
            inline = _parse_inline(f"<x>{content}</x>")
            label.tail = inline.text
            for c in inline:
                el.append(c)
        return el

    def _gen_index_entry_zone(self, zone):
        """"Index Entry" - verified against a real sample: a FLAT list (no
        primary/secondary/territory nesting - contrast the separate,
        unverified indexprimary/indexsecondary/indexterritory tags, which
        stay available for a book whose index genuinely needs that
        hierarchy). <li epub:type="index-entry" id="{prefix}_indN">term,
        page refs...</li>, wrapped in <ul class="none" epub:type=
        "index-entry-list"> by _wrap_consecutive (generate()).

        Known limitation: real output wraps each page number in its own
        <a epub:type="index-locator" href="{chapter file}#page_{n}">
        cross-file link - building that requires knowing which OTHER
        already-generated file contains a given page number, which spans
        multiple PDFs/projects and is out of scope for a single Generate
        XHTML run. Page numbers are emitted as plain extracted text here;
        turning them into cross-file links is future, multi-file-assembly
        work."""
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        self._index_counter += 1
        idx_id = f"{self.prefix}_ind{self._index_counter}"
        el = etree.Element("__index_li__", **{"epub_type": "index-entry", "id": idx_id})
        el.set(SRC_ATTR, zone.zone_id)
        if content:
            inline = _parse_inline(f"<x>{content}</x>")
            el.text = inline.text
            for c in inline:
                el.append(c)
        return el

    def _gen_table_zone(self, zone):
        """Real, semantically-structured HTML table (spec: "EPUBForge -
        TABLE DRAW - COMPLETE IMPLEMENTATION" - "Do not generate a
        visually fake table using <div>/<p>/<br> when a real HTML table
        is required"). This is the XHTML/EPUB-profile counterpart of
        core.xml_generator.py's own proven _zone_table, reusing the EXACT
        SAME detection engine (core.table_extractor.analyze_table - real
        PDF layout geometry: ruling lines, word gaps, never DTD/BITS
        rules, and never a second/duplicate table-structure detector).
        Manual row/column split overrides (gui.pdf_viewer's Row/Column
        Split Mode, zone.attributes["horizontal_splits"]/
        ["vertical_splits"]) are honored identically to the XML profile's
        own table generation - same attribute keys, same meaning.

        Distinct from the EPUB profile's existing "Table" tag (tblimage,
        asset_kind-based - captures the table as a cropped IMAGE, a
        completely different and unmodified feature): this handles the
        NEW "Table Draw" tag (tag literal "table", matching the XML
        profile's own TAG_TABLE constant), for a table that should become
        real semantic markup instead of a picture.

        Deliberately simpler than _zone_table in one respect, disclosed
        rather than silently approximated: it does not match manually-
        drawn CHILD zones nested inside the Table zone to override a
        specific cell's content (core.xml_generator.py's own
        _match_table_cell_children) - every cell's text comes directly
        from table_extractor's own real PDF-geometry-based extraction,
        which already reuses core.text_extractor for exact bold/italic/
        superscript/subscript/hyphenation-aware text, so Unicode/special-
        character fidelity is identical either way; only the "let a
        manually-zoned child override one specific cell" convenience is
        not yet wired up for this profile."""
        self._table_draw_counter += 1
        tbl_num = self._table_draw_counter
        page = self.pdf.get_page(zone.page)
        structure = table_extractor.analyze_table(
            page, zone.bbox, self._hyphen_keep_at(zone),
            manual_row_splits=zone.attributes.get("horizontal_splits"),
            manual_column_splits=zone.attributes.get("vertical_splits"))

        # Visible table styling (spec: generated tables must actually
        # render with borders/padding, not rely on a reader/browser
        # supplying its own table CSS - many EPUB readers apply none at
        # all to a bare <table>). Plain inline HTML attributes/style,
        # same convention <b>/<i>/etc. inline tags already use elsewhere
        # in this generator - not a new styling mechanism, and never
        # touches core.xml_generator.py's own BITS/semantic-XML table
        # output (that path targets further processing, not final
        # rendering, so it deliberately carries no presentation markup).
        table_el = etree.Element("table", id=f"{self.prefix}_table{tbl_num}",
                                   border="1", cellpadding="8", cellspacing="0",
                                   style="border-collapse: collapse; width: 100%;")
        header_rows = structure.rows[:structure.header_row_count]
        body_rows = structure.rows[structure.header_row_count:]
        if header_rows:
            thead_el = etree.SubElement(table_el, "thead")
            for row_idx, row_cells in enumerate(header_rows):
                thead_el.append(self._table_row_element_html(row_cells, "th", tbl_num, row_idx))
        if body_rows:
            tbody_el = etree.SubElement(table_el, "tbody")
            for row_idx, row_cells in enumerate(body_rows):
                tbody_el.append(self._table_row_element_html(row_cells, "td", tbl_num,
                                                                len(header_rows) + row_idx))
        table_el.set(SRC_ATTR, zone.zone_id)
        return table_el

    def _table_row_element_html(self, row_cells, cell_tag: str, tbl_num: int, row_idx: int):
        """One <tr> of real <th>/<td> cells (spec section 15/16: genuine
        header/body cell types, never every first row converted to <th>
        without evidence - the caller already decided header vs body rows
        from table_extractor's own header_row_count, a real signal - bold-
        text-ratio + top-of-table position, see table_extractor.py's
        TABLE_HEADER_BOLD_RATIO - never "the first row always"). Cell ids
        are deterministic (table number + row + column), so regenerating
        the same document twice produces identical ids (spec section 40:
        "Table ID stability... Do not generate random IDs")."""
        tr_el = etree.Element("tr")
        if cell_tag == "th":
            tr_el.set("style", "background-color: #f2f2f2;")
        cell_style = ("border: 1px solid #ddd; padding: 8px; text-align: left;" if cell_tag == "th"
                      else "border: 1px solid #ddd; padding: 8px;")
        col = 0
        for cell in row_cells:
            if cell is None:
                col += 1
                continue
            cell_id = f"{self.prefix}_table{tbl_num}_r{row_idx + 1}c{cell.col + 1}"
            cell_el = etree.SubElement(tr_el, cell_tag, id=cell_id)
            cell_el.set("style", cell_style)
            if cell.colspan > 1:
                cell_el.set("colspan", str(cell.colspan))
            if cell.rowspan > 1:
                cell_el.set("rowspan", str(cell.rowspan))
            if getattr(cell, "is_list", False) and cell.list_items:
                # A genuine detected bullet list stays as its own <ul>
                # INSIDE this cell (spec section 11: "within each cell,
                # text must be ordered top-to-bottom... do not combine
                # text from neighboring cells") - never flattened into
                # marker-prefixed plain text.
                ul_el = etree.SubElement(cell_el, "ul")
                for item_text in cell.list_items:
                    li_el = etree.SubElement(ul_el, "li")
                    if item_text:
                        inline = _parse_inline(f"<x>{item_text}</x>")
                        li_el.text = inline.text
                        for c in inline:
                            li_el.append(c)
            elif cell.text:
                inline = _parse_inline(f"<x>{cell.text}</x>")
                cell_el.text = inline.text
                for c in inline:
                    cell_el.append(c)
            col += cell.colspan
        return tr_el

    def _gen_image_zone(self, zone, kind):
        cfg = self.image_kinds.get(kind)
        if cfg is None:
            self.warnings.append(
                f"Zone {zone.zone_id} (tag={zone.tag!r}, page {zone.page}): no image_kinds "
                f"config for asset_kind={kind!r} in the active profile - image skipped.")
            return None
        # counter_key lets two DIFFERENT image_kinds entries (Inline
        # Equation's "ineq_img" and Inline Figure's "inlinefig") share ONE
        # sequential counter - confirmed with the user (spec section 16):
        # they intentionally number as one shared "inline" sequence, not
        # two independent ones that would otherwise both start at 1 and
        # collide on the same first filename. Every other image_kinds
        # entry has no counter_key, so it falls back to `kind` itself -
        # completely unchanged, independent-per-type counting.
        counter_key = cfg.get("counter_key", kind)
        filename = self.assets.save_image_asset(
            self.pdf, zone.page, zone.bbox, kind=counter_key, token=cfg.get("token", kind),
            ext=cfg.get("ext", "png"), digits=cfg.get("digits", 2), no_prefix=cfg.get("no_prefix", False))
        # The Cover image is matched by Mapping.xml via
        # //component[@type='cover']/img (a plain <img>, not a <cover>
        # element) - see profiles/epub_profile.json's comment on the
        # "cover" image_kinds entry.
        tag = "img" if zone.tag == "cover" else zone.tag
        # Output-folder-naming spec: images live in a sibling "images/"
        # directory next to the component's own XHTML file (never a flat,
        # cross-component shared folder) - src must be the forward-slashed
        # relative reference to it, never a bare filename or absolute path.
        el = etree.Element(tag, src=f"images/{filename}")
        el.set(SRC_ATTR, zone.zone_id)
        img_type = zone.attributes.get("img_type")
        if img_type:
            el.set("type", img_type)
        return el

    # ------------------------------------------------------- ID assignment
    def assign_missing_ids(self, root):
        """Final deterministic-ID pass (spec 98.4-98.9, 98.36-98.40) - run
        by the caller AFTER Mapping.xml's transformation AND
        xhtml_writer.rename_internal_wrapper_tags (so a former sec1-6/ssec/
        bssec wrapper, now <section>, is covered too). Anything that
        already has a real id (most headings/notes/refs/index entries are
        already id'd directly during generate()) is left untouched; an id
        containing a literal "{" (an unsubstituted Mapping.xml placeholder
        - e.g. glosshead's id='glos{0}', which has no <attrib> wired up to
        fill it in) is treated as missing and replaced. Every new id is
        "{prefix}_{kind}{N}" (spec 98.5-98.7 - prefix is this project's own
        chapter/file identifier, e.g. "ch3"), continuing THIS instance's
        own counters so nothing here can collide with an id already
        assigned during generate()."""
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            local = etree.QName(el).localname if "}" in el.tag else el.tag
            current_id = el.get("id")
            if current_id and "{" not in current_id:
                continue
            cls = el.get("class") or ""
            if local == "section":
                self._sec_counter += 1
                el.set("id", f"{self.prefix}_sec{self._sec_counter}")
            elif local in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._heading_counters[local] = self._heading_counters.get(local, 0) + 1
                el.set("id", f"{self.prefix}_{local}_{self._heading_counters[local]}")
            elif local == "figure":
                if "Table" in cls:
                    self._tbl_counter += 1
                    el.set("id", f"{self.prefix}_tbl{self._tbl_counter}")
                else:
                    self._fig_counter += 1
                    el.set("id", f"{self.prefix}_fig{self._fig_counter}")
            elif local == "div" and "equation" in cls:
                self._eq_counter += 1
                el.set("id", f"{self.prefix}_eq{self._eq_counter}")

    def assign_biblioentry_reflabel_ids(self, root):
        """Injects <span class="reflabel" id="{prefix}_ref_{n}"/> as the
        first child of every CUPEPUB Mapping.xml-produced bibliography
        entry (<li class="biblioentry" epub_type="biblioentry"> - see
        profiles/CUPEPUB/Mapping.xml's own `find="//ref_d | //ref_n"`
        rule). That rule only renames/wraps the original ref_d/ref_n
        element via <replace_ele> - it never adds an id or reflabel span
        of its own, so these entries previously had no id at all.

        {prefix} is this generator's own self.prefix - already the
        COMPLETE filename stem (settings["prefix"] = Path(pdf_path).stem,
        set in App.open_pdf() - e.g. "01_095AR_ch100", never just "ch"),
        so no separate prefix-extraction logic is needed here.

        Deliberately a SEPARATE counter/id format from the existing
        "Reference (Plain)" zone tag's own _gen_ref_zone/_ref_counter
        ({prefix}_refN, no underscore before the number, epub_type=
        "biblioref") - a completely different code path for a different
        zone tag, left untouched; reusing that counter/format was not
        required and risked coupling two otherwise-independent features.

        Idempotent - checked via the reflabel span itself (a biblioentry
        <li> never gets an id of its own), safe to call more than once.
        Numbers sequentially per call (per generated document), starting
        at 1, with no gaps or reuse."""
        counter = 0
        for el in root.iter():
            if not isinstance(el.tag, str) or el.tag != "li":
                continue
            if el.get("epub_type") != "biblioentry":
                continue
            if el.find("span[@class='reflabel']") is not None:
                continue
            counter += 1
            span = etree.Element("span", **{"class": "reflabel", "id": f"{self.prefix}_ref_{counter}"})
            # el.insert(0, span) alone is NOT enough to make the span
            # serialize FIRST: lxml's element.text (the <li>'s own direct
            # text content, immediately after its start tag) is a
            # SEPARATE property from its children and always renders
            # before them regardless of child order - inserting at index
            # 0 without moving it produces <li>TEXT<span/></li>, not the
            # required <li><span/>TEXT</li> (confirmed as a real bug via
            # a real App.generate_xhtml() run, not assumed). Moving the
            # <li>'s own text onto the new span's OWN tail (the text that
            # follows the span's end tag) fixes this correctly.
            span.tail = el.text
            el.text = None
            el.insert(0, span)

    def promote_maintitle_to_h1(self, root):
        """CUPEPUB's own maintitle|subtitle|chapau|chapaff Mapping.xml rule
        (see profiles/CUPEPUB/Mapping.xml) only ENCLOSES a run of those tags
        in a <header> - it never renames <maintitle> itself, so a real
        generated document previously kept a literal <maintitle>Bibliography
        </maintitle> forever (confirmed directly: output/xhtml/*.xhtml
        samples on disk). <maintitle> is the exact same logical concept as
        the "title" zone tag _gen_node already promotes straight to <h1>
        during generate() (see _build_document_header) - given the SAME
        <header><h1 id=... class=... epub_type="title"> shape here, no
        second title-element convention introduced. Must run AFTER
        Mapping.xml (maintitle only exists post-transform) and BEFORE
        assign_missing_ids, so the renamed element is picked up by that
        same existing id-assignment pass instead of a new one."""
        already_has_title_h1 = any(
            isinstance(el.tag, str) and el.tag == "h1" and el.get("epub_type") == "title"
            for el in root.iter())
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag == "subtitle":
                # Subtitle zone -> <h2 class="subtitle" epub:type="subtitle">
                # (Mapping.xml only encloses it in the title <header>).
                el.tag = "h2"
                el.set("class", "subtitle")
                el.set("epub_type", "subtitle")
        all_ids = {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}
        for el in root.iter():
            if not isinstance(el.tag, str) or el.tag != "maintitle":
                continue
            el.tag = "h1"
            if not already_has_title_h1:
                el.set("class", self._H1_CLASS_BY_COMPONENT.get(self.component_type, "fmtitle"))
                el.set("epub_type", "title")
                already_has_title_h1 = True
                # The component <section> is labelled by ITS title. Mapping.
                # xml's aria-labelledby repair ran while this was still
                # <maintitle>, so it had picked the first BODY heading.
                if not el.get("id"):
                    title_id = f"{self.prefix}_1"
                    if title_id in all_ids:
                        title_id = f"{self.prefix}_title"
                    el.set("id", title_id)
                    all_ids.add(title_id)
                anc = el.getparent()
                while anc is not None and anc.get("aria-labelledby") is None:
                    anc = anc.getparent()
                if anc is not None:
                    anc.set("aria-labelledby", el.get("id"))

    # Generic, human-readable fallback title per content type (spec:
    # "ZONETOOL - MASTER PRODUCTION FIX" sections 5/71/72 - "<title>Index
    # </title>", never "<title>27_63802_bm3</title>") - used ONLY when the
    # document has no maintitle/title heading at all (extract_document_
    # title returns None); a real title zone always wins. Deliberately
    # NOT exhaustive - an unlisted/unusual component_type falls back to a
    # simple title-cased version of its own key (e.g. "foreword" ->
    # "Foreword"), never the raw file prefix.
    _DEFAULT_TITLE_FOR_COMPONENT_TYPE = {
        "index": "Index", "preface": "Preface", "bibliography": "Bibliography",
        "glossary": "Glossary", "appendix": "Appendix", "foreword": "Foreword",
        "introduction": "Introduction", "dedication": "Dedication", "toc": "Contents",
        "acknow": "Acknowledgments", "contributor": "Contributors", "series": "Series",
        "copyrightpage": "Copyright", "titlepage": "Title Page", "halftitle": "Half Title",
        "cover": "Cover", "fm": "Front Matter", "bm": "Back Matter",
    }

    def default_title_for_component_type(self, component_type):
        if not component_type:
            return None
        key = component_type.strip().lower()
        return self._DEFAULT_TITLE_FOR_COMPONENT_TYPE.get(key) or key.replace("_", " ").title() or None

    def extract_document_title(self, root):
        """Document title = the SAME logical maintitle used for the visible
        <h1> (never independently re-searched from raw zones) - finds the
        h1 already marked epub_type="title" (set either by
        promote_maintitle_to_h1 above or by the pre-existing "title" zone ->
        h1 route in _build_document_header, both the same marker) and
        flattens its text, including any inline-markup children (e.g. an
        italicized word in the title) via itertext(). Returns None (caller
        falls back to the filename) if the document has no such heading at
        all - never raises, never returns an empty/whitespace string."""
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag == "h1" and el.get("epub_type") == "title":
                text = "".join(el.itertext()).strip()
                return text or None
        return None
