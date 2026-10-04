"""ValidationRunner (spec: "EPUBForge - PHASE 3 - Universal Auto-Fix
Engine" section 1's required component). The ONE implementation (spec:
"ONE implementation only") of "read the package, run Quick Validator, run
real EPUBCheck if available, wrap everything into core.epub.error_analyzer.
AnalyzableError, run the Error Analyzer" - used by BOTH app.validation.
validation_window's manual 'Validate Only' button and core.epub.
repair_engine's own before/after passes, so there is exactly one place
this sequence is coded, never two copies that could quietly drift apart."""
from dataclasses import dataclass, field

from core.epub import error_analyzer, package_reader, quick_validator
from core.epubcheck import runner as epubcheck_runner


@dataclass
class ValidationSnapshot:
    epub_path: str
    package: object                  # core.epub.package_reader.EpubPackage
    quick_result: object              # core.epub.quick_validator.QuickValidationResult
    epubcheck_result: object           # core.epubcheck.parser.EpubCheckResult | None (None: EPUBCheck unavailable)
    qv_analyses: list = field(default_factory=list)   # [ErrorAnalysis, ...] - same order/length as quick_result.issues
    ec_analyses: list = field(default_factory=list)   # [ErrorAnalysis, ...] - same order/length as epubcheck_result.messages

    @property
    def all_analyses(self) -> list:
        return self.qv_analyses + self.ec_analyses

    @property
    def n_errors(self) -> int:
        """Errors severe enough to fail validation: Quick Validator ERROR
        rows + EPUBCheck FATAL/ERROR rows - never WARNING/USAGE/INFO."""
        n = self.quick_result.n_errors if self.quick_result else 0
        if self.epubcheck_result and self.epubcheck_result.ran:
            n += self.epubcheck_result.n_fatal + self.epubcheck_result.n_error
        return n

    @property
    def is_valid(self) -> bool:
        qv_ok = bool(self.quick_result and self.quick_result.is_valid)
        ec_ok = bool(self.epubcheck_result and self.epubcheck_result.ran and self.epubcheck_result.is_valid)
        return qv_ok and ec_ok


def run_validation(epub_path: str) -> ValidationSnapshot:
    package = package_reader.read_package(epub_path)
    qv_result = quick_validator.validate(package)
    qv_errors = [error_analyzer.from_quick_issue(i) for i in qv_result.issues]

    ec_result = None
    ok_avail, _ = epubcheck_runner.check_availability()
    if ok_avail:
        ec_result = epubcheck_runner.run_epubcheck(epub_path)
    ec_errors = ([error_analyzer.from_epubcheck_message(m) for m in ec_result.messages]
                 if ec_result and ec_result.ran else [])

    combined = qv_errors + ec_errors
    analyses = error_analyzer.analyze_errors(epub_path, package, combined) if combined else []
    qv_analyses = analyses[:len(qv_errors)]
    ec_analyses = analyses[len(qv_errors):]

    return ValidationSnapshot(epub_path=epub_path, package=package, quick_result=qv_result,
                               epubcheck_result=ec_result, qv_analyses=qv_analyses, ec_analyses=ec_analyses)
