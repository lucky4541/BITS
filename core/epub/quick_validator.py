"""Quick Validator (spec: "EPUBForge / ZoneTool - Unified EPUB Editor +
Validator" section 9: display "Quick Validator" and "EPUBCheck" SEPARATELY
- two distinct tools, not one). This is a fast, lightweight, LOCAL
structural sanity pass over an already-read core.epub.package_reader.
EpubPackage - deliberately NOT a reimplementation of EPUBCheck's own much
deeper, spec-exhaustive validation (section 8's "universal validation"
list is EPUBCheck's job, run for real via core.epubcheck.runner). Quick
Validator exists to catch the most common structural problems instantly,
without needing the ~seconds-long real EPUBCheck subprocess, and to keep
working even in the (disclosed, checked separately) case where Java/
EPUBCheck aren't available at all."""
import posixpath
from dataclasses import dataclass, field


@dataclass
class QuickValidationIssue:
    severity: str    # "ERROR" | "WARNING"
    code: str         # "QV-xxx" - this module's own codes, never confused with a real EPUBCheck ID
    message: str
    file: str = ""


@dataclass
class QuickValidationResult:
    issues: list = field(default_factory=list)   # [QuickValidationIssue, ...]

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def n_warnings(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARNING")

    @property
    def is_valid(self) -> bool:
        return self.n_errors == 0


def validate(package) -> QuickValidationResult:
    """package: a core.epub.package_reader.EpubPackage. Never raises -
    every check is defensive against the package having failed to parse
    at all (package.error already set) or being only partially populated."""
    result = QuickValidationResult()
    add = result.issues.append

    if package.error:
        add(QuickValidationIssue("ERROR", "QV-000", package.error, file=package.path))
        return result  # nothing else can be meaningfully checked without a readable package

    if "mimetype" not in package.zip_names:
        add(QuickValidationIssue("ERROR", "QV-001", "The 'mimetype' file is missing from the package."))
    else:
        if package.first_entry_name != "mimetype":
            add(QuickValidationIssue(
                "ERROR", "QV-002", "'mimetype' must be the FIRST entry in the zip archive.", file="mimetype"))
        if not package.mimetype_stored:
            add(QuickValidationIssue(
                "ERROR", "QV-003", "'mimetype' must be stored UNCOMPRESSED (no deflate).", file="mimetype"))
        if package.mimetype_content != "application/epub+zip":
            add(QuickValidationIssue(
                "ERROR", "QV-004",
                f"'mimetype' content must be exactly 'application/epub+zip' (found {package.mimetype_content!r}).",
                file="mimetype"))

    if not package.opf_path:
        add(QuickValidationIssue("ERROR", "QV-010", "No OPF (package document) could be located."))
        return result  # every remaining check needs a real OPF to check against

    if not package.manifest:
        add(QuickValidationIssue("WARNING", "QV-011", "The manifest is empty.", file=package.opf_path))
    if not package.spine:
        add(QuickValidationIssue("ERROR", "QV-012", "The spine is empty - the package has no reading order.",
                                  file=package.opf_path))

    manifest_ids = {item.id for item in package.manifest if item.id}
    seen_ids = set()
    for item in package.manifest:
        if item.id in seen_ids:
            add(QuickValidationIssue("ERROR", "QV-020", f"Duplicate manifest id '{item.id}'.",
                                      file=package.opf_path))
        seen_ids.add(item.id)

        resolved = package.resolve_href(item.href)
        if not resolved:
            add(QuickValidationIssue("ERROR", "QV-021", f"Manifest item '{item.id}' has no href.",
                                      file=package.opf_path))
        elif resolved not in package.zip_names:
            add(QuickValidationIssue(
                "ERROR", "QV-022", f"Manifest item '{item.id}' references '{item.href}', "
                                    f"which does not exist in the package.", file=package.opf_path))

    for ref in package.spine:
        if ref.idref not in manifest_ids:
            add(QuickValidationIssue(
                "ERROR", "QV-030", f"Spine itemref '{ref.idref}' does not match any manifest item.",
                file=package.opf_path))

    # QV-023 (spec: "EPUBForge - PHASE 3 - Universal Auto-Fix Engine"
    # example "missing manifest entry when resource exists") - a real file
    # physically present in the package that no manifest item declares.
    # WARNING, not ERROR: an undeclared file does not by itself make the
    # EPUB invalid under this module's own is_valid rule, but real
    # EPUBCheck will flag it - this exists so the Auto-Fix Engine has a
    # 100%-certain (Quick Validator is EPUBForge's own code, never
    # ambiguous about its own codes) trigger to add the missing entry.
    declared_hrefs = {package.resolve_href(item.href) for item in package.manifest if item.href}
    for name in sorted(package.zip_names):
        if name in declared_hrefs or name in ("mimetype", package.opf_path):
            continue
        if name.startswith("META-INF/"):
            continue
        add(QuickValidationIssue(
            "WARNING", "QV-023", f"'{name}' exists in the package but is not declared in the manifest.",
            file=name))

    if package.epub_version.startswith("3"):
        has_nav = any("nav" in (item.properties or "").split() for item in package.manifest)
        if not has_nav:
            add(QuickValidationIssue(
                "ERROR", "QV-040", "EPUB 3 package has no manifest item with properties=\"nav\".",
                file=package.opf_path))

    if not package.title:
        add(QuickValidationIssue("WARNING", "QV-050", "No <dc:title> found in metadata.", file=package.opf_path))
    if not package.identifier:
        add(QuickValidationIssue("WARNING", "QV-051", "No <dc:identifier> found in metadata.",
                                  file=package.opf_path))

    return result
