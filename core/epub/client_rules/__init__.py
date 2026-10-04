"""Client validation profiles - the publisher's own delivery rules, run as a
separate validation next to EPUBCheck and repaired by the same closed-loop,
data-safe auto-fix engine (core.epub.repair_engine).

PROFILES maps a client name to (validate(files, epub_name) -> ClientReport,
FIXES [(label, rule codes, fn(mp, package))]).

parse_log(text) reads a log written by the client's tool (SPiXVali) so the
findings it reported can be listed - and checked against the native
re-implementation - inside the application."""
import re
from dataclasses import dataclass, field

from core.epub.client_rules import cupepub, cupepub_fixes

PROFILES = {
    "CUPEPUB": (cupepub.validate, cupepub_fixes.FIXES),
}
DEFAULT_PROFILE = "CUPEPUB"

_LINE_RE = re.compile(r"^\s*\d+\.\s*(Error|Warning|Exception)\[([A-Z]+-\d+)\]:(\d+):(\d+)\s+(.*)$")


@dataclass
class ToolLog:
    client: str = ""
    profile: str = ""
    input_path: str = ""
    totals: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)       # cupepub.ClientFinding

    def by_code(self):
        out = {}
        for f in self.findings:
            out[f.code] = out.get(f.code, 0) + 1
        return dict(sorted(out.items()))


def parse_log(text: str) -> ToolLog:
    log = ToolLog()
    for line in text.splitlines():
        m = re.match(r"#\s*Client Name\s*:\s*(.+)", line)
        if m:
            log.client = m.group(1).strip()
        m = re.match(r"#\s*Profile\s*:\s*(.+)", line)
        if m:
            log.profile = m.group(1).strip()
        m = re.match(r"#\s*Input \w+ File Path\s*:\s*(.+)", line)
        if m:
            log.input_path = m.group(1).strip()
        m = re.match(r"#Total (\w+) count:\s*(\d+)", line)
        if m:
            log.totals[m.group(1)] = int(m.group(2))
        m = _LINE_RE.match(line)
        if m:
            msg = m.group(5).strip()
            fm = re.search(r'"([^"]+\.x?html)"|file: ([^\s,]+\.x?html)|check ([^\s]+\.x?html)', msg, re.I)
            file = next((g for g in (fm.groups() if fm else ()) if g), "")
            log.findings.append(cupepub.ClientFinding(m.group(2), m.group(1), file, int(m.group(3)), int(m.group(4)),
                                                      msg))
    return log


def compare(tool_log: ToolLog, report) -> list:
    """[(code, tool count, native before count)] - how the native rules
    reproduce what the client's tool reported."""
    native = report.by_code()
    codes = sorted(set(tool_log.by_code()) | set(native))
    return [(c, tool_log.by_code().get(c, 0), native.get(c, 0)) for c in codes]
