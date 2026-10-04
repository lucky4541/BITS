"""RepairEngine + RepairReport - the CLOSED-LOOP, data-safe EPUB auto-fix
engine ("VALIDATE & AUTO-FIX EPUB").

    INPUT EPUB -> BACKUP (immutable, hashed) -> INVENTORY (content integrity
    baseline) -> EPUBCHECK + Quick Validator -> ERROR ANALYZER -> ROOT-CAUSE
    REPAIR PLAN (levels) -> per-repair TRANSACTION:
        apply ONE strategy to a working copy -> package -> CONTENT INTEGRITY
        (vs the original) -> structure check -> [LEVEL 2: EPUBCheck now]
        -> COMMIT or ROLLBACK (recorded in the repair history)
    -> EPUBCHECK AGAIN -> COMPARE -> next pass ... until clean or only
    manual-review issues remain -> [PDF-aware repairs + comparison when a
    PDF is given] -> FINAL PACKAGE (clean build) -> FINAL EPUBCHECK ON THE
    PACKAGED FILE -> FINAL INTEGRITY CHECK -> DASHBOARD + REPORTS

Everything that decides WHAT is wrong is reused from the existing pipeline:
core.epub.validation_runner (Quick Validator + real EPUBCheck +
error_analyzer), core.epub.repair_planner (root causes, levels, priority),
the strategies of core.epub.repair_strategies / xhtml_repair_strategy /
autofix_strategies, and core.epub.integrity_snapshot for ZERO DATA LOSS.

Safety rules enforced here:
  * the original file is only ever READ (its sha256 is verified again at
    the end); every write goes to the session's working/ folder
  * every repair is its own transaction; a repair that loses ANY content
    (text, heading, paragraph, image, link, id, page marker, nav entry,
    metadata, CSS rule, binary asset...) is rolled back automatically
  * LEVEL 1 (safe) repairs are batched and then validated with EPUBCheck;
    if a batch makes EPUBCheck worse it is replayed one repair at a time
    ("careful mode") and the culprit is rejected
  * LEVEL 2 (high confidence) repairs each get EPUBCheck immediately
  * LEVEL 3/4 are never applied - they are reported with the recommended
    manual action
  * a strategy that keeps changing files without reducing its errors is
    stopped (REPAIR LOOP) and its issues marked NEEDS MANUAL REVIEW
  * success = EPUBCheck clean on the FINAL PACKAGED file AND content
    integrity PASS - never just "the error disappeared"
"""
import datetime
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field

from core.epub import integrity_snapshot, package_builder, package_reader, quick_validator, repair_planner
from core.epub import autofix_strategies as A
from core.epub.repair_strategies import DEFAULT_REGISTRY
from core.epub.validation_runner import run_validation

MAX_PASSES = 8
LOOP_STRIKES = 2

# the new categories are also available to the legacy registry path
for _cat, (_lvl, _prio, _fn, _lbl, _man) in repair_planner.CATALOG.items():
    if _fn is not None and _lvl <= 2:
        DEFAULT_REGISTRY._by_category.setdefault(_cat, _fn)


@dataclass
class RepairReport:
    initial_errors: int = 0
    fixed_automatically: int = 0
    review_required: int = 0
    remaining: int = 0
    files_modified: list = field(default_factory=list)
    files_added: list = field(default_factory=list)
    files_removed: list = field(default_factory=list)
    quick_validator_before: str = ""
    quick_validator_after: str = ""
    epubcheck_before: str = ""
    epubcheck_after: str = ""
    content_integrity: str = ""
    content_integrity_verified: bool = False
    final_status: str = ""
    modifications: list = field(default_factory=list)
    passes_run: int = 0
    output_path: str = ""
    backup_path: str = ""
    rolled_back: bool = False
    original_characters: int = 0
    final_characters: int = 0
    original_words: int = 0
    final_words: int = 0
    original_images: int = 0
    final_images: int = 0
    original_xhtml_files: int = 0
    final_xhtml_files: int = 0
    # closed-loop engine
    overall: str = ""                  # PASS | NEEDS REVIEW | FAIL
    session_dir: str = ""
    epubcheck_available: bool = False
    initial_plan: list = field(default_factory=list)     # PlanGroup.to_dict()
    history: list = field(default_factory=list)          # repair records
    dashboard: dict = field(default_factory=dict)        # check -> {"status", "detail"}
    remaining_issues: list = field(default_factory=list)
    integrity_findings: list = field(default_factory=list)
    totals_before: dict = field(default_factory=dict)
    totals_after: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)          # name -> path
    pdf_summary: dict = field(default_factory=dict)
    original_unchanged: bool = True
    # client validation (e.g. CUPEPUB - the publisher's own delivery rules)
    client_profile: str = ""
    client_before: dict = field(default_factory=dict)     # {"counts", "by_code"} on the original
    client_after: dict = field(default_factory=dict)      # on the delivered package
    client_findings: list = field(default_factory=list)   # remaining client findings (dicts)
    client_tool_log: dict = field(default_factory=dict)   # the client tool's own log, compared
    delivery_path: str = ""                                # <ISBN>.epub when the client names files by ISBN

    @property
    def committed(self):
        return sum(1 for h in self.history if h["status"] == "COMMITTED")

    @property
    def rolled_back_repairs(self):
        return sum(1 for h in self.history if h["status"] == "ROLLED BACK")


def _qv_summary(result) -> str:
    if result is None:
        return "not run"
    return f"{result.n_errors} error(s), {result.n_warnings} warning(s)"


def _ec_summary(result) -> str:
    if result is None:
        return "unavailable"
    if not result.ran:
        return f"could not run ({result.error})"
    return f"{result.n_fatal} fatal, {result.n_error} error(s), {result.n_warning} warning(s)"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _signatures(snapshot, categories=None):
    out = set()
    for a in snapshot.all_analyses:
        if (a.error.severity or "").upper() not in repair_planner.BLOCKING:
            continue
        if categories is None or a.root_cause.category in categories:
            out.add((a.error.code, os.path.basename(a.error.file or ""), a.error.message))
    return out


def _blocking(snapshot):
    """Blocking findings counted per LOCATION - EPUBCheck's own nError counts
    a message once even when it lists many locations (e.g. one RSC-012 for
    five broken fragments), which would hide both progress and damage."""
    return sum(1 for a in snapshot.all_analyses if (a.error.severity or "").upper() in repair_planner.BLOCKING)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class _State:
    def __init__(self, path, snapshot, inventory):
        self.path = path
        self.snapshot = snapshot
        self.inventory = inventory
        self.allowed = integrity_snapshot.Allowed()

    def copy_from(self, other):
        self.path, self.snapshot, self.inventory = other.path, other.snapshot, other.inventory
        self.allowed = integrity_snapshot.Allowed()
        self.allowed.merge(other.allowed)

    def clone(self):
        c = _State(self.path, self.snapshot, self.inventory)
        c.allowed.merge(self.allowed)
        return c


class AutoFixSession:
    """One closed-loop run. Use run_full_auto_repair() for the default."""

    def __init__(self, epub_path, session_dir=None, pdf_path=None, progress=None, max_passes=MAX_PASSES,
                 cancel=None, client_profile=None, client_log=None):
        self.epub_path = os.path.abspath(epub_path)
        base = os.path.splitext(self.epub_path)[0]
        self.session_dir = session_dir or base + "_autofix"
        self.backup_dir = os.path.join(self.session_dir, "repair_backup")
        self.working_dir = os.path.join(self.session_dir, "working")
        self.history_dir = os.path.join(self.session_dir, "repair_history")
        self.pdf_path = pdf_path
        self.progress = progress
        self.cancel = cancel
        self.max_passes = max_passes
        self.report = RepairReport(output_path=base + ".repaired.epub", session_dir=self.session_dir)
        self.step = 0
        self.blacklist = {}              # strategy label -> reason
        self.strikes = {}
        self.review_items = []           # from strategies: things they could not resolve
        self.attempted = {}              # strategy label -> last outcome text
        self.inv0 = None
        self.client_profile = client_profile
        self.client_log = client_log
        self.report.client_profile = client_profile or ""

    # ----------------------------------------------------------- utils
    def _say(self, text):
        if self.progress:
            self.progress(text)

    def _cancelled(self):
        return bool(self.cancel and self.cancel())

    def _next_path(self, tag):
        self.step += 1
        return os.path.join(self.working_dir, f"step_{self.step:03d}_{tag}.epub")

    def _record(self, pass_num, level, label, category, res, status, reason, integ=None, ec=None, codes=()):
        changes = list(getattr(res, "changes", []) or [])
        if not changes and res is not None and res.description:
            changes = [{"file": f, "location": "", "before": "", "after": "", "reason": d.strip(),
                        "confidence": 1.0} for f, d in zip(res.files or [""] * 99, res.description.split("; "))]
        conf = min([c.get("confidence", 1.0) for c in changes] or [1.0])
        rec = {"n": len(self.report.history) + 1, "timestamp": _now(), "pass": pass_num, "level": level,
               "strategy": label, "category": category, "codes": sorted(set(codes)),
               "files": sorted({c["file"] for c in changes}), "changes": changes, "confidence": conf,
               "integrity": integ.summary() if integ is not None else "", "epubcheck": ec or "",
               "status": status, "reason": reason}
        self.report.history.append(rec)
        return rec

    # ------------------------------------------------------ transaction
    def _transaction(self, state, pass_num, level, label, category, producer, validate_with_epubcheck, codes=()):
        """producer(state) -> (candidate_path, result) or (None, result).
        Returns True when committed."""
        try:
            cand, res = producer(state)
        except Exception as e:  # noqa: BLE001 - a crashing repair is simply rejected
            self._record(pass_num, level, label, category, None, "ROLLED BACK", f"repair raised {type(e).__name__}: {e}")
            return False
        if res is not None:
            for item in getattr(res, "review", []) or []:
                self.review_items.append(dict(item, strategy=label))
        if cand is None:
            return False
        allowed = integrity_snapshot.Allowed()
        allowed.merge(state.allowed)
        if res is not None and getattr(res, "allowed", None) is not None:
            allowed.merge(res.allowed)
        inv = integrity_snapshot.build_inventory(cand)
        integ = integrity_snapshot.diff(self.inv0, inv, allowed)
        if not integ.ok:
            self._record(pass_num, level, label, category, res, "ROLLED BACK",
                         "content integrity check failed - " + "; ".join(str(f) for f in integ.losses[:4]), integ,
                         codes=codes)
            self.attempted[label] = "rolled back (would lose content)"
            return False
        pkg = package_reader.read_package(cand)
        qv = quick_validator.validate(pkg)
        before_qv = state.snapshot.quick_result.n_errors if state.snapshot.quick_result else 0
        if qv.n_errors > before_qv:
            self._record(pass_num, level, label, category, res, "ROLLED BACK",
                         f"package structure got worse (Quick Validator {before_qv} -> {qv.n_errors} errors)", integ,
                         codes=codes)
            self.attempted[label] = "rolled back (package structure got worse)"
            return False
        ec_text = ""
        snap = None
        if validate_with_epubcheck:
            self._say(f"Pass {pass_num}: EPUBCheck after '{label}'")
            snap = run_validation(cand)
            before_n, after_n = _blocking(state.snapshot), _blocking(snap)
            new_sigs = _signatures(snap) - _signatures(state.snapshot)
            ec_text = f"errors {before_n} -> {after_n}"
            if after_n > before_n or (after_n == before_n and new_sigs):
                self._record(pass_num, level, label, category, res, "ROLLED BACK",
                             f"EPUBCheck got worse ({ec_text}" + (f", {len(new_sigs)} new" if new_sigs else "") + ")",
                             integ, ec_text, codes)
                self.attempted[label] = "rolled back (EPUBCheck got worse)"
                return False
        state.path, state.inventory, state.allowed = cand, inv, allowed
        if snap is not None:
            state.snapshot = snap
        self._record(pass_num, level, label, category, res, "COMMITTED", "", integ, ec_text, codes)
        self.attempted[label] = "applied"
        return True

    def _strategy_producer(self, fn, tag):
        def produce(state):
            mp = package_builder.MutablePackage.load(state.path)
            pkg = package_reader.read_package(state.path)
            res = fn(mp, pkg)
            if not res.applied:
                return None, res
            cand = self._next_path(tag)
            mp.write_epub(cand)
            return cand, res
        return produce

    # -------------------------------------------------------------- run
    def run(self) -> RepairReport:
        rep = self.report
        for d in (self.backup_dir, self.working_dir, self.history_dir):
            os.makedirs(d, exist_ok=True)
        # 1. immutable backup, verified
        self._say("Backing up the original EPUB")
        original_sha = _sha256(self.epub_path)
        backup = os.path.join(self.backup_dir, "original.epub")
        if not os.path.exists(backup) or _sha256(backup) != original_sha:
            shutil.copyfile(self.epub_path, backup)
        if _sha256(backup) != original_sha:
            raise IOError("backup verification failed - nothing was changed")
        with open(os.path.join(self.backup_dir, "original.sha256"), "w") as f:
            f.write(f"{original_sha}  {os.path.basename(self.epub_path)}\n")
        rep.backup_path = backup
        # 2. baseline
        self._say("Recording the content-integrity baseline")
        self.inv0 = integrity_snapshot.build_inventory(backup)
        with open(os.path.join(self.backup_dir, "inventory_before.json"), "w", encoding="utf-8") as f:
            json.dump(self.inv0.to_dict(), f, indent=1, ensure_ascii=False)
        self._say("Running EPUBCheck on the original")
        work0 = self._next_path("original")
        shutil.copyfile(backup, work0)
        initial = run_validation(work0)
        rep.epubcheck_available = bool(initial.epubcheck_result and initial.epubcheck_result.ran)
        rep.initial_errors = _blocking(initial)
        rep.quick_validator_before = _qv_summary(initial.quick_result)
        rep.epubcheck_before = _ec_summary(initial.epubcheck_result)
        plan = repair_planner.build_plan(initial)
        rep.initial_plan = [g.to_dict() for g in plan]
        with open(os.path.join(self.history_dir, "repair_plan.txt"), "w", encoding="utf-8") as f:
            f.write(repair_planner.plan_text(plan))
        state = _State(work0, initial, self.inv0)
        # 3. package hygiene first (junk never reaches the loop or the final package)
        self._transaction(state, 0, 1, "package hygiene", "PACKAGE_HYGIENE",
                          self._strategy_producer(A.fix_package_hygiene, "hygiene"), False)
        # 4. closed loop
        status = ""
        for pass_num in range(1, self.max_passes + 1):
            if self._cancelled():
                status = "CANCELLED - the last committed state was packaged."
                break
            plan = repair_planner.build_plan(state.snapshot)
            auto = [g for g in plan if g.automatic and g.strategy_label not in self.blacklist]
            if not auto:
                status = "COMPLETE - no automatic repair applies to the remaining issues."
                break
            rep.passes_run = pass_num
            pass_start = state.clone()
            errors_start = _blocking(state.snapshot)
            sigs_start = _signatures(state.snapshot)
            committed_any = False
            # LEVEL 1 batch
            level1, seen = [], set()
            for g in auto:
                if g.level == 1 and g.strategy_label not in seen:
                    seen.add(g.strategy_label)
                    level1.append(g)
            ran_l1 = False
            for g in level1:
                self._say(f"Pass {pass_num}: {g.strategy_label}")
                codes = [c for x in plan if x.strategy_label == g.strategy_label for c in x.codes]
                if self._transaction(state, pass_num, 1, g.strategy_label, g.category,
                                     self._strategy_producer(g.strategy, g.category.lower()), False, codes):
                    ran_l1 = committed_any = True
            if ran_l1:
                self._say(f"Pass {pass_num}: EPUBCheck after the safe repairs")
                snap = run_validation(state.path)
                if _blocking(snap) > errors_start:
                    # careful mode: replay one at a time with EPUBCheck after each
                    self._say(f"Pass {pass_num}: errors increased - replaying repairs one at a time")
                    for h in rep.history:
                        if h["pass"] == pass_num and h["status"] == "COMMITTED":
                            h["status"] = "ROLLED BACK"
                            h["reason"] = "batch made EPUBCheck worse - replayed individually"
                    state.copy_from(pass_start)
                    committed_any = False
                    for g in level1:
                        if self._transaction(state, pass_num, 1, g.strategy_label, g.category,
                                             self._strategy_producer(g.strategy, g.category.lower()), True):
                            committed_any = True
                        elif self.attempted.get(g.strategy_label, "").startswith("rolled back (EPUBCheck"):
                            self.blacklist[g.strategy_label] = "made EPUBCheck worse"
                else:
                    state.snapshot = snap
            # LEVEL 2 - one transaction each, EPUBCheck immediately
            seen2 = set()
            for g in repair_planner.build_plan(state.snapshot):
                if g.level != 2 or not g.automatic or g.strategy_label in self.blacklist or \
                        g.strategy_label in seen2:
                    continue
                seen2.add(g.strategy_label)
                self._say(f"Pass {pass_num}: {g.strategy_label}")
                codes = [c for x in plan if x.strategy_label == g.strategy_label for c in x.codes]
                if self._transaction(state, pass_num, 2, g.strategy_label, g.category,
                                     self._strategy_producer(g.strategy, g.category.lower()), True, codes):
                    committed_any = True
                elif self.attempted.get(g.strategy_label, "").startswith("rolled back"):
                    self.blacklist[g.strategy_label] = self.attempted[g.strategy_label]
            # loop detection: a strategy that changed files but its errors did not go down
            sigs_now = _signatures(state.snapshot)
            for g in auto:
                if self.attempted.get(g.strategy_label) != "applied":
                    continue
                cat_sigs_before = {s for s in sigs_start if any(a.error.message == s[2] for a in g.errors)}
                if cat_sigs_before and cat_sigs_before <= sigs_now:
                    self.strikes[g.strategy_label] = self.strikes.get(g.strategy_label, 0) + 1
                    if self.strikes[g.strategy_label] >= LOOP_STRIKES:
                        self.blacklist[g.strategy_label] = "REPAIR LOOP - the same errors return after repair"
            if not committed_any:
                status = "COMPLETE - no further safe repair could be applied."
                break
            if _blocking(state.snapshot) == 0:
                status = "COMPLETE - EPUBCheck reports no errors."
                break
        else:
            status = f"STOPPED - reached the {self.max_passes}-pass limit."
        # 5. PDF-aware repairs (page markers / image mapping) - LEVEL 2
        if self.pdf_path and os.path.isfile(self.pdf_path) and not self._cancelled():
            self._pdf_stage(state)
        # 5b. client validation + client-rule repairs (each its own transaction)
        if self.client_profile and not self._cancelled():
            self._client_stage(state)
        # 6. final package + final EPUBCheck on the packaged file
        self._say("Packaging the final EPUB")
        mp = package_builder.MutablePackage.load(state.path)
        for n in list(mp.order):
            if A.JUNK_RE.search(n) and n not in self._declared(state.path):
                mp.remove_entry(n)
                state.allowed.removed_files.add(n)
        mp.write_epub(rep.output_path)
        self._say("Final EPUBCheck on the packaged EPUB")
        final = run_validation(rep.output_path)
        inv_final = integrity_snapshot.build_inventory(rep.output_path)
        integ = integrity_snapshot.diff(self.inv0, inv_final, state.allowed)
        if not integ.ok:
            # never ship a package that lost content: fall back to the original
            shutil.copyfile(backup, rep.output_path)
            rep.rolled_back = True
            status = "ROLLED BACK - the final package failed the content-integrity check; original kept."
            final = run_validation(rep.output_path)
            inv_final = integrity_snapshot.build_inventory(rep.output_path)
            integ = integrity_snapshot.diff(self.inv0, inv_final, state.allowed)
        rep.original_unchanged = _sha256(self.epub_path) == original_sha
        if self.client_profile:
            self._client_final(integ.ok and not rep.rolled_back)
        self._finish(initial, final, inv_final, integ, status)
        self._write_reports(initial, final)
        return rep

    @staticmethod
    def _declared(path):
        pkg = package_reader.read_package(path)
        return {pkg.resolve_href(i.href) for i in pkg.manifest if i.href}

    # --------------------------------------------------------------- PDF
    def _pdf_stage(self, state):
        from core.qc.engine import QCSession
        self._say("PDF comparison: mapping page markers and images")
        cache = os.path.join(self.session_dir, "qc_cache")
        try:
            s = QCSession.open(self.pdf_path, state.path, cache_dir=cache)
            s.analyse()
        except Exception as e:  # noqa: BLE001
            self.report.pdf_summary = {"error": f"PDF comparison failed: {e}"}
            return

        def produce(_state):
            n = s.auto_correct()
            if not n:
                return None, None
            cand = self._next_path("pdf")
            s.mgr.export_epub(cand)
            from core.epub.repair_strategies import RepairActionResult
            res = RepairActionResult(applied=True, description=f"{n} PDF-verified page-marker / image correction(s)",
                                     files=[])
            res.changes = [{"file": h.split(":")[0] if ":" in h else "", "location": "", "before": "", "after": "",
                            "reason": h, "confidence": 0.9} for h in s.mgr.sidecar.get("history", [])[-n:]]
            return cand, res
        self._transaction(state, "PDF", 2, "PDF page markers / images", "PDF_MAPPING", produce, True)

    # ------------------------------------------------------------ client
    def _client_validate(self, path, name=None):
        import zipfile
        from core.epub import client_rules
        validate, _fixes = client_rules.PROFILES[self.client_profile]
        with zipfile.ZipFile(path) as zf:
            files = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.filename.endswith("/")}
        return validate(files, name or os.path.basename(self.epub_path))

    def _client_stage(self, state):
        from core.epub import client_rules
        _validate, fixes = client_rules.PROFILES[self.client_profile]
        self._say(f"Client validation ({self.client_profile})")
        before = self._client_validate(state.path)
        self.report.client_before = {"counts": before.counts(), "by_code": before.by_code()}
        self._client_before_log = before.log_text()
        if self.client_log:
            try:
                with open(self.client_log, encoding="utf-8", errors="replace") as f:
                    tool = client_rules.parse_log(f.read())
                self.report.client_tool_log = {
                    "path": self.client_log, "client": tool.client, "input": tool.input_path, "totals": tool.totals,
                    "by_code": tool.by_code(), "compare": client_rules.compare(tool, before)}
            except OSError as e:
                self.report.client_tool_log = {"path": self.client_log, "error": str(e)}
        current = before
        for label, codes, fn in fixes:
            if self._cancelled():
                break
            if not any(f.code in codes and f.fix for f in current.findings):
                continue
            self._say(f"Client rules: {label}")
            if self._transaction(state, "CLIENT", 2, label, "CLIENT_RULES", self._strategy_producer(fn, "client"),
                                 True, [c for c in codes if any(f.code == c for f in current.findings)]):
                current = self._client_validate(state.path)

    def _client_final(self, ok):
        """Validates the delivered package; when the client names packages by
        ISBN a correctly named copy is written to client_delivery/."""
        from core.epub.client_rules import cupepub
        rep = self.report
        target = rep.output_path
        name = os.path.basename(self.epub_path)
        if ok:
            res = self._client_validate(rep.output_path)
            isbn = ""
            try:
                import zipfile
                with zipfile.ZipFile(rep.output_path) as zf:
                    files = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.filename.endswith("/")}
                isbn = cupepub.Book(files).isbn().replace("urn:isbn:", "")
            except Exception:  # noqa: BLE001
                pass
            if re.fullmatch(r"\d{13}", isbn or ""):
                folder = os.path.join(self.session_dir, "client_delivery")
                os.makedirs(folder, exist_ok=True)
                target = os.path.join(folder, f"{isbn}.epub")
                shutil.copyfile(rep.output_path, target)
                rep.delivery_path = target
                name = os.path.basename(target)
        after = self._client_validate(target, name)
        rep.client_after = {"counts": after.counts(), "by_code": after.by_code(), "package": target}
        why = {}
        for item in self.review_items:
            if item.get("strategy", "").startswith(self.client_profile):
                why.setdefault(os.path.basename(item.get("file", "")), []).append(
                    f"{item.get('reference', '')}: {item.get('reason', '')}")
        rep.client_findings = [dict(f.to_dict(), recommended=cupepub.manual_action(f.code),
                                    why_not_fixed=why.get(f.file, [])[:4]) for f in after.findings]
        self._client_after_log = after.log_text()

    # ------------------------------------------------------------ finish
    def _finish(self, initial, final, inv_final, integ, status):
        rep = self.report
        rep.quick_validator_after = _qv_summary(final.quick_result)
        rep.epubcheck_after = _ec_summary(final.epubcheck_result)
        rep.remaining = _blocking(final)
        rep.fixed_automatically = max(0, rep.initial_errors - rep.remaining)
        rep.integrity_findings = [f.__dict__ for f in integ.findings]
        rep.content_integrity = integ.summary()
        rep.content_integrity_verified = integ.ok
        rep.totals_before = self.inv0.totals()
        rep.totals_after = inv_final.totals()
        tb, ta = rep.totals_before, rep.totals_after
        rep.original_characters, rep.final_characters = tb["characters"], ta["characters"]
        rep.original_words, rep.final_words = tb["words"], ta["words"]
        rep.original_images, rep.final_images = tb["image_files"], ta["image_files"]
        rep.original_xhtml_files, rep.final_xhtml_files = tb["xhtml_files"], ta["xhtml_files"]
        committed = [h for h in rep.history if h["status"] == "COMMITTED"]
        rep.modifications = [f"[Pass {h['pass']}] {c['file']}: {c['reason']}"
                             + (f" ('{c['before']}' -> '{c['after']}')" if c.get("before") or c.get("after") else "")
                             for h in committed for c in h["changes"]]
        before_files, after_files = set(self.inv0.files), set(inv_final.files)
        rep.files_added = sorted(after_files - before_files)
        rep.files_removed = sorted(before_files - after_files)
        rep.files_modified = sorted(n for n in before_files & after_files
                                    if self.inv0.files[n][0] != inv_final.files[n][0])
        rep.remaining_issues = self._remaining(final) + self._client_remaining()
        rep.review_required = len(rep.remaining_issues)
        rep.dashboard = self._dashboard(final, inv_final, integ)
        if rep.rolled_back or not integ.ok:
            rep.overall = "FAIL"
        elif rep.dashboard["Final package"]["status"] == "PASS" and \
                all(v["status"] in ("PASS", "SKIP") for v in rep.dashboard.values()):
            rep.overall = "PASS"
        else:
            rep.overall = "NEEDS REVIEW"
        if rep.overall == "PASS":
            rep.final_status = "VALIDATED - the packaged EPUB passes EPUBCheck and no content was lost."
        elif not rep.epubcheck_available:
            rep.final_status = ("NEEDS REVIEW - EPUBCheck is not available, so the package cannot be declared "
                                "valid. " + (status or ""))
        else:
            rep.final_status = (status or "") + (f" {rep.remaining} blocking issue(s) need manual review."
                                                 if rep.remaining else "")

    def _client_remaining(self):
        out = []
        for f in self.report.client_findings:
            attempted = next((h["strategy"] + ": " + h["status"].lower() for h in reversed(self.report.history)
                              if h["pass"] == "CLIENT" and f["code"] in h["codes"]), "none")
            why = list(f.get("why_not_fixed") or []) or (
                ["the client rule needs a decision or content that is not in the package"] if f.get("fix") == ""
                else ["no single certain fix"])
            out.append({"code": f["code"], "severity": f["severity"], "file": f["file"], "line": f["line"],
                        "message": f["message"], "root_cause": f"CLIENT RULES ({self.client_profile})",
                        "explanation": f["message"], "level": 3, "attempted": attempted, "why_not_fixed": why,
                        "recommended": f["recommended"]})
        return out

    def _remaining(self, final):
        out = []
        groups = repair_planner.build_plan(final)
        for g in groups:
            attempted = self.attempted.get(g.strategy_label, "") if g.strategy is not None else ""
            why = []
            for item in self.review_items:
                if item.get("strategy") == g.strategy_label and (not g.files or any(
                        item.get("file", "").endswith(os.path.basename(f)) for f in g.files)):
                    why.append(f"{item.get('reference', '')}: {item.get('reason', '')}")
            if g.strategy_label in self.blacklist:
                why.append(self.blacklist[g.strategy_label])
            if g.level >= 3:
                why.append(f"LEVEL {g.level} ({repair_planner.LEVEL_NAMES[g.level]}) - never changed automatically")
            if not why:
                why.append("the automatic repair found no single certain fix")
            for a in g.errors:
                out.append({"code": a.error.code, "severity": a.error.severity, "file": a.error.file,
                            "line": a.error.line, "message": a.error.message, "root_cause": g.category,
                            "explanation": g.explanation, "level": g.level,
                            "attempted": f"{g.strategy_label}: {attempted}" if attempted else
                            (g.strategy_label or "none"),
                            "why_not_fixed": sorted(set(why))[:6], "recommended": g.manual_action})
        return out

    def _dashboard(self, final, inv, integ):
        ec = final.epubcheck_result
        d = {}

        def row(name, ok, detail, skip=False):
            d[name] = {"status": "SKIP" if skip else ("PASS" if ok else "REVIEW"), "detail": detail}
        if ec is not None and ec.ran:
            row("EPUBCheck", ec.n_fatal + ec.n_error == 0,
                f"{ec.n_fatal} fatal, {ec.n_error} error(s), {ec.n_warning} warning(s) ({ec.version})")
        else:
            d["EPUBCheck"] = {"status": "REVIEW", "detail": "EPUBCheck not available (Java + tools/epubcheck/"
                                                             "epubcheck.jar) - cannot be declared valid"}
        bad_xml = [n for n, x in inv.docs.items() if not x.well_formed]
        row("XHTML", not bad_xml, "all documents well-formed" if not bad_xml else f"not well-formed: {bad_xml}")
        opf_err = [a for a in final.all_analyses if (a.error.severity or "").upper() in repair_planner.BLOCKING
                   and ((a.error.code or "").startswith(("OPF", "PKG")) or a.context.is_opf)]
        row("OPF / spine / manifest", not opf_err, f"{len(opf_err)} package error(s)" if opf_err else "valid")
        nav = [n for n in inv.docs if n.endswith(os.path.basename(final.package.nav_path or "\0"))]
        nav_bad = [h for n in nav for h, ok in inv.docs[n].links if not ok]
        row("Navigation", not nav_bad, f"{len(inv.nav_entries)} entries, all targets valid" if not nav_bad
            else f"{len(nav_bad)} broken: {nav_bad[:4]}")
        from collections import Counter
        dups = {n: [i for i, c in Counter(x.ids).items() if c > 1] for n, x in inv.docs.items()}
        dups = {n: v for n, v in dups.items() if v}
        row("IDs", not dups, "unique in every document" if not dups else f"duplicates: {dups}")
        frag_bad = [(n, h) for n, x in inv.docs.items() for h, ok in x.links if "#" in (h or "") and not ok]
        row("Fragments", not frag_bad, "all #fragment targets exist" if not frag_bad else f"{len(frag_bad)} broken: "
            + ", ".join(f"{os.path.basename(n)} -> {h}" for n, h in frag_bad[:4]))
        link_bad = [(n, h) for n, x in inv.docs.items() for h, ok in x.links if not ok]
        row("Links", not link_bad, f"{sum(len(x.links) for x in inv.docs.values())} links, all resolve"
            if not link_bad else f"{len(link_bad)} broken")
        img_bad = [(n, s) for n, x in inv.docs.items() for s, ok in x.images if not ok]
        img_lost = [f for f in integ.losses if f.category in ("image reference", "binary")]
        hdr = sum(1 for f in integ.findings if f.category == "binary" and f.severity == "CHANGE")
        row("Images", not img_bad and not img_lost,
            f"{sum(len(x.images) for x in inv.docs.values())} image reference(s), all resolve; "
            f"{len(inv.binaries) - hdr} asset(s) byte-identical"
            + (f", {hdr} changed as declared (DPI header / cover resized for the client)" if hdr else "")
            if not img_bad and not img_lost
            else f"{len(img_bad)} unresolved reference(s): " + ", ".join(s for _n, s in img_bad[:4]))
        pm_before = sum(len(x.page_markers) for x in self.inv0.docs.values())
        pm_after = [p for x in inv.docs.values() for p in x.page_markers]
        pm_dup = [p for p, c in Counter(pm_after).items() if c > 1 and p]
        row("Page markers", len(pm_after) >= pm_before and not pm_dup,
            f"{len(pm_after)} marker(s) ({pm_before} before)" + (f", duplicates: {pm_dup[:5]}" if pm_dup else ""))
        row("Content integrity", integ.ok, integ.summary())
        d["Data loss"] = {"status": "PASS" if integ.ok else "FAIL",
                          "detail": "NONE" if integ.ok else "; ".join(str(f) for f in integ.losses[:5])}
        rep = self.report
        d["Repairs"] = {"status": "PASS" if not rep.remaining_issues else "REVIEW",
                        "detail": f"{rep.committed} applied, {rep.rolled_back_repairs} rolled back, "
                                  f"{len(rep.remaining_issues)} manual review"}
        d["Original file"] = {"status": "PASS" if rep.original_unchanged else "FAIL",
                              "detail": "untouched (sha256 verified)" if rep.original_unchanged
                              else "MODIFIED - this must never happen"}
        clean = ec is not None and ec.ran and ec.n_fatal + ec.n_error == 0 and integ.ok
        d["Final package"] = {"status": "PASS" if clean else "REVIEW",
                              "detail": "VALIDATED (EPUBCheck run on the packaged .epub)" if clean
                              else "not validated - see remaining issues"}
        if self.client_profile and rep.client_after:
            ca, cb = rep.client_after["counts"], rep.client_before.get("counts", {})
            d[f"Client rules ({self.client_profile})"] = {
                "status": "PASS" if ca["Error"] + ca["Warning"] == 0 else "REVIEW",
                "detail": f"{ca['Error']} error(s), {ca['Warning']} warning(s) "
                          f"(before: {cb.get('Error', '?')} / {cb.get('Warning', '?')})"
                          + (f"; delivered as {os.path.basename(rep.delivery_path)}" if rep.delivery_path else "")}
        if self.report.pdf_summary:
            ps = self.report.pdf_summary
            d["PDF comparison"] = {"status": "PASS" if ps.get("open_differences", 1) == 0 else "REVIEW",
                                   "detail": ps.get("text", ps.get("error", ""))}
        return d

    # ----------------------------------------------------------- reports
    def _write_reports(self, initial, final):
        from core.epub import autofix_report
        rep = self.report
        if self.pdf_path and os.path.isfile(self.pdf_path):
            try:
                from core.qc.engine import QCSession
                s = QCSession.open(self.pdf_path, rep.output_path, cache_dir=os.path.join(self.session_dir, "qc_cache"))
                s.analyse()
                path = os.path.join(self.history_dir, "pdf_comparison.html")
                s.export_report(path)
                rep.reports["PDF <-> XHTML comparison"] = path
                open_d = [d for d in s.differences if d.status == "open"]
                rep.pdf_summary = {"scores": s.mapping.scores, "open_differences": len(open_d),
                                   "text": f"Overall {s.mapping.scores.get('Overall')}%, {len(open_d)} open "
                                           f"difference(s)"}
                rep.dashboard["PDF comparison"] = {"status": "PASS" if not open_d else "REVIEW",
                                                   "detail": rep.pdf_summary["text"]}
            except Exception as e:  # noqa: BLE001
                rep.pdf_summary = {"error": f"PDF comparison failed: {e}"}
        for attr, fname, title in (("_client_before_log", "client_validation_before.log", "Client validation - original"),
                                   ("_client_after_log", "client_validation_after.log", "Client validation - final")):
            text = getattr(self, attr, None)
            if text:
                path = os.path.join(self.history_dir, fname)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                rep.reports[title] = path
        rep.reports.update(autofix_report.write_all(rep, initial, final, self.inv0, self.history_dir))


def run_full_auto_repair(epub_path: str, progress=None, pdf_path: str = None, session_dir: str = None,
                         max_passes: int = MAX_PASSES, cancel=None, client_profile: str = None,
                         client_log: str = None) -> RepairReport:
    """VALIDATE & AUTO-FIX EPUB. Leaves the original untouched and writes
    `<name>.repaired.epub` next to it, plus the session folder
    `<name>_autofix/` (repair_backup/, working/, repair_history/ with the
    reports). See the module docstring for the workflow.

    client_profile ("CUPEPUB"): also run the client's own validation rules
    (core.epub.client_rules) and their repairs, each as a transaction;
    client_log: the client tool's log (SPiXVali) to compare against."""
    return AutoFixSession(epub_path, session_dir=session_dir, pdf_path=pdf_path, progress=progress,
                          max_passes=max_passes, cancel=cancel, client_profile=client_profile,
                          client_log=client_log).run()

