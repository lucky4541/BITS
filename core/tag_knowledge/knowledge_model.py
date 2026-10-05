"""TAG KNOWLEDGE MODEL - everything Auto Tag knows about the project's tag
system, derived from the project itself:

    profile tag buttons      (CUPEPUB_Zoning.xml + CUPLookup.xml via cup_config)
  + zoning categories        (frontmatter / bodypart / backmatter / float)
  + Mapping.xml              (families, parent/child, section openers, outputs)
  + DTD(s)                   (content models, attributes, ordering)  - optional
  + reference XML corpus     (frequencies, succession, text patterns) - optional
  + reference projects       (zoned PDFs: geometry/font per tag)       - optional
  + user corrections         (learned role -> tag preferences)
  + mandatory-zone rules     (CUPEPUB_ZoneValidation.xml)

Nothing here names a project tag in code: semantic ROLES (profiles/<profile>/
semantic_roles.json) are resolved to the project's own tags by matching the
role vocabulary against each tag's own label/name/attribute/output tokens, and
the matches are ranked with the structural/statistical evidence above.
"""
import json
import os
from dataclasses import dataclass, field

from core.resource_path import resource_path
from core.tag_knowledge.tokens import tokenize, pieces
from core.tag_knowledge.mapping_model import MappingModel
from core.tag_knowledge.dtd_model import DTDModel
from core.tag_knowledge.corpus_model import CorpusModel

# Zone attributes that identify a tag button but are bookkeeping, not
# semantics (never fed to the role matcher).
_BOOKKEEPING_ATTRS = {"cup_name", "asset_kind"}


@dataclass
class TagOption:
    """One concrete way to tag a zone (a profile tag button)."""
    label: str
    tag: str
    attrs: dict
    category: str = None
    is_image: bool = False
    lookup_type: str = None
    tokens: set = field(default_factory=set)          # own label / name / attribute tokens
    output_tokens: set = field(default_factory=set)   # Mapping.xml output class/epub:type/role tokens

    @property
    def key(self) -> str:
        return self.label

    def zone_attributes(self) -> dict:
        return dict(self.attrs)


@dataclass
class RoleMatch:
    option: TagOption
    score: float
    reasons: list = field(default_factory=list)


@dataclass
class StructureVerdict:
    ok: bool = True               # False = violates a hard constraint (DTD)
    adjust: float = 0.0           # soft score adjustment
    reasons: list = field(default_factory=list)


class TagKnowledgeModel:
    def __init__(self, profile: dict, lexicon: dict, mapping: MappingModel, dtds=None, corpus=None,
                 reference_template=None, learned=None, zoning_meta=None, diagnostics=None):
        self.profile = profile
        self.profile_name = profile.get("name", "")
        self.lexicon = lexicon
        self.mapping = mapping
        self.dtds = list(dtds or [])
        self.corpus = corpus
        self.reference_template = reference_template
        self.learned = dict(learned or {})
        self.diagnostics = list(diagnostics or [])
        self.zoning_meta = dict(zoning_meta or {})
        self.options = self._build_options()
        self.roles = self._expand_roles(lexicon.get("roles", {}))
        self._role_cache = {}

    # ------------------------------------------------------------- build
    @classmethod
    def build(cls, profile: dict, settings: dict = None, reference_template=None) -> "TagKnowledgeModel":
        settings = settings or {}
        diagnostics = []
        profile_dir = cls.profile_dir(profile)
        lexicon = cls.load_lexicon(profile_dir, diagnostics)
        mapping_path = settings.get("mapping_xml_path") or profile.get("mapping_xml_path") or ""
        mapping = MappingModel.load(mapping_path)
        diagnostics.extend(mapping.errors)

        dtds = []
        dtd_dirs = [d for d in [profile_dir, settings.get("auto_tag_dtd_dir")] if d]
        if (profile.get("name") or "").upper() in ("BITS", "JATS"):
            # The BITS / JATS DTD describes the FINAL document (book / article /
            # sec ...), not the zone tags (verse-line, fn, contrib ... are placed
            # by core.bits.structure), so it is never a zone-level Auto Tag
            # constraint - the generated output is validated against it instead
            # (core.bits.pipeline, auto_validation stage 8). Only a DTD the user
            # points Auto Tag at explicitly is used here.
            dtd_dirs = [d for d in [settings.get("auto_tag_dtd_dir")] if d]
        for path in settings.get("auto_tag_dtd_paths", []) or []:
            try:
                dtds.append(DTDModel.from_file(path))
            except Exception as e:  # noqa: BLE001
                diagnostics.append(f"DTD {path}: {e}")
        for d in dtd_dirs:
            found, errors = DTDModel.discover(d)
            dtds.extend(found)
            diagnostics.extend(errors)

        known = {b["tag"] for b in profile.get("tag_buttons", [])}
        corpus_dirs = [os.path.join(profile_dir, "reference_xml")] if profile_dir else []
        corpus_dirs += list(settings.get("auto_tag_reference_xml_dirs", []) or [])
        corpus_files = []
        for d in corpus_dirs:
            if d and os.path.isdir(d):
                for root, _dirs, files in os.walk(d):
                    corpus_files += [os.path.join(root, f) for f in sorted(files)
                                     if f.lower().endswith((".xml", ".xhtml", ".html", ".htm"))]
        corpus = CorpusModel.from_files(corpus_files, known_elements=known, mapping=mapping)
        diagnostics.extend(corpus.errors)

        zoning_meta = {}

        learned = (settings.get("auto_tag_learning") or {})
        return cls(profile, lexicon, mapping, dtds, corpus, reference_template, learned, zoning_meta, diagnostics)

    @staticmethod
    def profile_dir(profile: dict) -> str:
        mp = profile.get("mapping_xml_path") or ""
        if mp:
            return os.path.dirname(mp)
        name = profile.get("name") or ""
        d = resource_path("profiles", name)
        return d if os.path.isdir(d) else ""

    @staticmethod
    def load_lexicon(profile_dir: str, diagnostics: list) -> dict:
        candidates = [os.path.join(profile_dir, "semantic_roles.json")] if profile_dir else []
        candidates.append(resource_path("profiles", "semantic_roles.json"))
        for path in candidates:
            if path and os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    diagnostics.append(f"semantic_roles.json: {e}")
        diagnostics.append("semantic_roles.json not found - Auto Tag has no role vocabulary")
        return {"roles": {}}

    def _build_options(self) -> list:
        options = []
        for b in self.profile.get("tag_buttons", []):
            attrs = dict(b.get("attrs", {}))
            label = b.get("label", "")
            meta = self.zoning_meta.get(attrs.get("cup_name") or label, {})
            semantic_attr_values = [v for k, v in attrs.items() if k not in _BOOKKEEPING_ATTRS and isinstance(v, str)]
            opt = TagOption(
                label=label, tag=b["tag"], attrs=attrs,
                category=meta.get("category"),
                is_image=bool(attrs.get("asset_kind")) or meta.get("property") == "image",
                lookup_type=meta.get("lookup_type"),
                tokens=tokenize(label, b["tag"], *semantic_attr_values),
                output_tokens=self.mapping.output_tokens(b["tag"]) if self.mapping else set(),
            )
            options.append(opt)
        return options

    @staticmethod
    def _expand_roles(roles: dict) -> dict:
        out = {}
        for name, spec in roles.items():
            levels = spec.get("levels")
            if levels:
                for lv in range(1, int(levels) + 1):
                    groups = [[w.replace("{level}", str(lv)) for w in g] for g in spec.get("all", [])]
                    out[f"{name}_{lv}"] = dict(spec, all=groups, level=lv)
            else:
                out[name] = dict(spec)
        return out

    # ------------------------------------------------------------- roles
    def option_by_label(self, label: str):
        return next((o for o in self.options if o.label == label), None)

    def options_for_tag(self, tag: str) -> list:
        return [o for o in self.options if o.tag == tag]

    def options_for_role(self, role: str) -> list:
        """Every project tag matching `role`, best first."""
        if role in self._role_cache:
            return self._role_cache[role]
        spec = self.roles.get(role)
        matches = []
        if spec:
            groups = spec.get("all", [])
            vocab = {w for g in groups for w in g}
            none = set(spec.get("none", []))
            want_image = bool(spec.get("image", False))
            for opt in self.options:
                if opt.is_image != want_image:
                    continue
                if none & opt.tokens:
                    continue
                weights, reasons = [], []
                for g in groups:
                    if set(g) & opt.tokens:
                        weights.append(1.0)
                        reasons.append(f"name matches {sorted(set(g) & opt.tokens)}")
                    elif set(g) & opt.output_tokens:
                        weights.append(0.6)
                        reasons.append(f"Mapping.xml output matches {sorted(set(g) & opt.output_tokens)}")
                    else:
                        weights = None
                        break
                if not weights:
                    continue
                score = sum(weights) / len(weights)
                # Specificity: a tag whose name carries extra concepts beyond
                # the role (e.g. a "no-indent" variant for plain paragraph)
                # is a weaker match than the plain one.
                own = pieces(opt.label)
                joined = "".join(own)
                if joined in vocab:
                    extra = 0
                else:
                    extra = len([t for t in own if t not in vocab and not t.isdigit()])
                score -= 0.1 * extra
                learned = self.learned.get(role, {}).get(opt.label, 0)
                if learned:
                    score += min(0.3, 0.05 * learned)
                    reasons.append(f"user chose this tag {learned}x for this role")
                if self.corpus is not None and self.corpus.available and opt.tag in self.corpus.text_stats:
                    score += min(0.1, 0.5 * self.corpus.frequency(opt.tag))
                    reasons.append("used in reference corpus")
                if self.reference_template is not None and opt.tag in getattr(self.reference_template,
                                                                             "tag_profiles", {}):
                    score += 0.05
                    reasons.append("used in reference project")
                matches.append(RoleMatch(opt, round(score, 4), reasons))
        matches.sort(key=lambda m: -m.score)
        self._role_cache[role] = matches
        return matches

    def best_for_role(self, role: str):
        m = self.options_for_role(role)
        return m[0] if m else None

    def supported_roles(self) -> list:
        return [r for r in self.roles if self.options_for_role(r)]

    def role_of_tag(self, tag: str, attrs: dict = None):
        """Reverse lookup: which role does an existing zone's tag play."""
        label = (attrs or {}).get("cup_name")
        for role in self.roles:
            for m in self.options_for_role(role):
                if m.option.tag == tag and (label is None or m.option.label == label):
                    return role
        return None

    # --------------------------------------------------------- structure
    def tag_families(self, tag: str) -> list:
        return self.mapping.families_of(tag) if self.mapping else []

    def is_image_tag(self, tag: str) -> bool:
        return any(o.is_image for o in self.options_for_tag(tag))

    def structural_verdict(self, tag: str, prev_tag: str = None, next_tag: str = None,
                           parent_tag: str = None) -> StructureVerdict:
        v = StructureVerdict()
        root = self.profile.get("root_tag")
        # 1. DTD - hard constraints
        for dtd in self.dtds:
            if not dtd.has_element(tag):
                continue
            container = parent_tag or root
            allowed = dtd.allows_child(container, tag) if container else None
            if allowed is False:
                v.ok = False
                v.reasons.append(f"DTD {os.path.basename(dtd.source)}: <{tag}> not allowed in <{container}>")
            seq = dtd.allows_sequence(container, prev_tag, tag) if container and prev_tag else None
            if seq is False:
                v.adjust -= 0.15
                v.reasons.append(f"DTD: <{tag}> may not follow <{prev_tag}>")
        # 2. Mapping.xml families - a text member of a family that exists to
        #    group text with an image (figure / table parts) needs a family
        #    neighbour, otherwise it would generate a detached wrapper.
        fams = self.tag_families(tag)
        if fams and not self.is_image_tag(tag):
            neighbour = any(self.mapping.same_family(tag, t) for t in (prev_tag, next_tag) if t)
            image_family = any(any(self.is_image_tag(m) for m in f.members if m != tag) for f in fams)
            if neighbour:
                v.adjust += 0.08
                v.reasons.append("adjacent to a member of its Mapping.xml family")
            elif image_family:
                v.adjust -= 0.2
                v.reasons.append("Mapping.xml groups this tag with an image, but no image/family member is adjacent")
        # 3. Reference corpus succession
        if self.corpus is not None and self.corpus.available and prev_tag:
            p = self.corpus.succession_probability(prev_tag, tag)
            if p is not None:
                if p == 0.0:
                    v.adjust -= 0.08
                    v.reasons.append(f"reference corpus never has <{tag}> after <{prev_tag}>")
                elif p > 0.2:
                    v.adjust += 0.05
                    v.reasons.append(f"reference corpus: <{prev_tag}> -> <{tag}> is common ({p:.0%})")
        return v

    def required_attributes(self, tag: str) -> list:
        out = []
        for dtd in self.dtds:
            out.extend(a.name for a in dtd.required_attributes(tag))
        return sorted(set(out))

    def allowed_attribute_values(self, tag: str, attr: str) -> list:
        for dtd in self.dtds:
            vals = dtd.attribute_values(tag, attr)
            if vals:
                return vals
        return []

    # ------------------------------------------------------------ report
    def report(self) -> str:
        lines = [f"TAG KNOWLEDGE MODEL - profile {self.profile_name}", ""]
        lines.append(f"Tag options: {len(self.options)}")
        lines.append(f"Mapping.xml: {self.mapping.path if self.mapping else '-'} "
                     f"({self.mapping.rule_count if self.mapping else 0} rules, "
                     f"{len(self.mapping.families) if self.mapping else 0} families)")
        lines.append(f"DTDs: {', '.join(d.source for d in self.dtds) or 'none found'}")
        if self.corpus is not None:
            lines.append(f"Reference XML corpus: {len(self.corpus.files)} file(s)")
        lines.append(f"Reference projects: {'yes' if self.reference_template is not None else 'none loaded'}")
        lines.append(f"Learned corrections: {sum(sum(v.values()) for v in self.learned.values())}")
        lines.append("")
        lines.append("ROLE -> TAG resolution (best first):")
        for role in self.roles:
            matches = self.options_for_role(role)
            if matches:
                lines.append(f"  {role:24s} " + ", ".join(f"{m.option.label} ({m.score:.2f})" for m in matches[:4]))
            else:
                lines.append(f"  {role:24s} (no project tag - role is never applied)")
        if self.mapping and self.mapping.families:
            lines.append("")
            lines.append("Mapping.xml families:")
            for f in self.mapping.families:
                lines.append(f"  {' | '.join(f.members)}  ->  <{f.wrapper_tag} {f.wrapper_attrs}>")
        if self.diagnostics:
            lines.append("")
            lines.append("Diagnostics:")
            lines.extend(f"  {d}" for d in self.diagnostics)
        return "\n".join(lines)
