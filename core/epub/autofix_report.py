"""Reports of a VALIDATE & AUTO-FIX run (core.epub.repair_engine), written
to the session's repair_history/ folder:

  report.html            dashboard + every section below, one page
  report.json            everything, machine readable
  repair_history.json    every repair: time, level, codes, files, before /
                         after, reason, confidence, integrity, EPUBCheck,
                         COMMITTED / ROLLED BACK + why
  epubcheck_before.json  EPUBCheck messages of the original
  epubcheck_after.json   EPUBCheck messages of the FINAL packaged EPUB
  integrity.json         content-integrity baseline / final totals and every
                         finding (losses and expected changes)
Sections of the HTML: dashboard, EPUBCheck before/after, repair plan,
repair history, remaining issues (root cause, attempted repair, why it was
not fixed, recommended action), data integrity, image mapping, page
markers, links / fragments, split documents, final package.
"""
import html
import json
import os

STATUS_COLORS = {"PASS": "#1b7f3b", "REVIEW": "#b7791f", "FAIL": "#c53030", "SKIP": "#718096",
                 "NEEDS REVIEW": "#b7791f", "COMMITTED": "#1b7f3b", "ROLLED BACK": "#c53030"}


def _ec_rows(snapshot):
    ec = snapshot.epubcheck_result
    if ec is None or not ec.ran:
        return []
    return [{"code": m.code, "severity": m.severity, "file": m.file, "line": m.line, "column": m.column,
             "message": m.message} for m in ec.messages]


def write_all(rep, initial, final, inv0, folder) -> dict:
    os.makedirs(folder, exist_ok=True)
    paths = {}

    def dump(name, obj):
        p = os.path.join(folder, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
        return p
    from core.epub import integrity_snapshot
    inv_final = integrity_snapshot.build_inventory(rep.output_path)
    paths["EPUBCheck - original"] = dump("epubcheck_before.json", _ec_rows(initial))
    paths["EPUBCheck - final package"] = dump("epubcheck_after.json", _ec_rows(final))
    paths["Repair history"] = dump("repair_history.json", rep.history)
    paths["Data integrity"] = dump("integrity.json", {"before": inv0.to_dict(), "after": inv_final.to_dict(),
                                                      "findings": rep.integrity_findings})
    data = {k: v for k, v in rep.__dict__.items() if k not in ("reports",)}
    paths["Full report (JSON)"] = dump("report.json", data)
    p = os.path.join(folder, "report.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_html(rep, initial, final, inv0, inv_final))
    paths["Report (HTML)"] = p
    return paths


def _badge(status):
    col = STATUS_COLORS.get(status, "#4a5568")
    return f'<span class="b" style="background:{col}">{html.escape(status)}</span>'


def _table(headers, rows):
    e = html.escape
    out = ["<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c if str(c).startswith('<span') else e(str(c))}</td>" for c in r)
                   + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def _html(rep, initial, final, inv0, inv_final):
    e = html.escape
    tiles = "".join(f'<div class="tile"><div class="tn">{e(k)}</div>{_badge(v["status"])}'
                    f'<div class="td">{e(v["detail"])}</div></div>' for k, v in rep.dashboard.items())
    plan_rows = [(i + 1, g["category"], ", ".join(g["codes"]), g["errors"], ", ".join(g["files"])[:80],
                  f'{g["level"]} - {g["level_name"]}', g["action"][:120],
                  ("symptom of " + g["cascade_of"]) if g["cascade_of"] else "") for i, g in enumerate(rep.initial_plan)]
    hist_rows = []
    for h in rep.history:
        ch = h["changes"][:6]
        detail = "<br>".join(e(f"{c['file']}: {c['reason']}" + (f" | '{c['before']}' -> '{c['after']}'"
                                                                 if c.get("before") or c.get("after") else ""))
                             for c in ch) + (f"<br>... {len(h['changes']) - 6} more" if len(h["changes"]) > 6 else "")
        hist_rows.append((h["n"], h["pass"], h["level"], e(h["strategy"]), e(", ".join(h["codes"])),
                          f'<span>{detail}</span>' if detail else "", f"{h['confidence']:.0%}",
                          e(h["integrity"][:80]), e(h["epubcheck"]), _badge(h["status"]), e(h["reason"][:160])))
    remaining = [(r["code"], r["severity"], r["file"], r["line"], r["message"][:140], r["root_cause"],
                  r["attempted"], "; ".join(r["why_not_fixed"])[:200], r["recommended"]) for r in rep.remaining_issues]
    tb, ta = rep.totals_before, rep.totals_after
    totals = [(k, tb.get(k), ta.get(k), "" if ta.get(k) == tb.get(k) else (
        "+" + str(ta.get(k) - tb.get(k)) if (ta.get(k) or 0) > (tb.get(k) or 0) else str(ta.get(k) - tb.get(k))))
              for k in tb]
    findings = [(f["severity"], f["category"], f["file"], f["detail"][:200]) for f in rep.integrity_findings]
    images = []
    for name, info in sorted(inv_final.binaries.items()):
        if info.get("dims") is None:
            continue
        b = inv0.binaries.get(name, {})
        refs = sum(1 for x in inv_final.docs.values() for s, ok in x.images if ok and os.path.basename(
            s.replace("\\", "/")) == os.path.basename(name))
        images.append((name, "x".join(map(str, info["dims"])), info["sha256"][:12],
                       "unchanged" if b.get("sha256") == info["sha256"] else ("NEW" if not b else "CHANGED"), refs))
    pm = [(os.path.basename(n), ", ".join(x.page_markers[:40]) + (" ..." if len(x.page_markers) > 40 else ""),
           len(inv0.docs[n].page_markers) if n in inv0.docs else "-", len(x.page_markers))
          for n, x in sorted(inv_final.docs.items()) if x.page_markers or (n in inv0.docs and inv0.docs[n].page_markers)]
    links = [(os.path.basename(n), h) for n, x in sorted(inv_final.docs.items()) for h, ok in x.links if not ok]
    splits = [(i + 1, os.path.basename(p), len(inv_final.docs[p].words) if p in inv_final.docs else "-",
               len(inv_final.docs[p].ids) if p in inv_final.docs else "-") for i, p in enumerate(inv_final.spine)]
    files = [(n, s, h[:16]) for n, (h, s) in sorted(inv_final.files.items())]
    client = ""
    if rep.client_profile and rep.client_after:
        cb, ca = rep.client_before.get("by_code", {}), rep.client_after.get("by_code", {})
        tool = rep.client_tool_log.get("by_code", {}) if rep.client_tool_log else {}
        codes = sorted(set(cb) | set(ca) | set(tool))
        rows = [(c, tool.get(c, "") if tool else "", cb.get(c, 0), ca.get(c, 0)) for c in codes]
        heads = ["Rule", "Client tool log", "Before", "After"] if tool else ["Rule", "", "Before", "After"]
        left = [(f["code"], f["severity"], f["file"], f"{f['line']}:{f['col']}", f["message"][:160],
                 f["recommended"]) for f in rep.client_findings]
        client = (f"<h2>Client rules ({e(rep.client_profile)})</h2><p>Before: {e(str(rep.client_before.get('counts')))}"
                  f" &nbsp; After: {e(str(rep.client_after.get('counts')))}"
                  + (f"<br><b>Delivery copy:</b> {e(rep.delivery_path)}" if rep.delivery_path else "") + "</p>"
                  + _table(heads, rows)
                  + (_table(["Rule", "Severity", "File", "Line", "Message", "Recommended action"], left)
                     if left else "<p>No client findings remain.</p>"))
    ec_before = [(m["code"], m["severity"], m["file"], m["line"], m["message"][:160]) for m in _ec_rows(initial)]
    ec_after = [(m["code"], m["severity"], m["file"], m["line"], m["message"][:160]) for m in _ec_rows(final)]
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>EPUB Validation &amp; Auto-Fix Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1a202c}}h1{{margin-bottom:4px}}
h2{{margin-top:28px;border-bottom:2px solid #e2e8f0;padding-bottom:4px}}.b{{color:#fff;border-radius:4px;
padding:2px 8px;font-weight:700;font-size:12px}}.tile{{display:inline-block;vertical-align:top;width:230px;
border:1px solid #e2e8f0;border-radius:6px;padding:8px;margin:4px}}.tn{{font-weight:700;margin-bottom:4px}}
.td{{font-size:12px;color:#4a5568;margin-top:4px}}table{{border-collapse:collapse;width:100%;font-size:12px}}
th,td{{border:1px solid #e2e8f0;padding:4px;vertical-align:top;text-align:left}}th{{background:#f7fafc}}
.big{{font-size:20px}}</style></head><body>
<h1>EPUB Validation &amp; Auto-Fix</h1>
<p class="big">Overall: {_badge(rep.overall)} &nbsp; {e(rep.final_status)}</p>
<p><b>Input:</b> {e(rep.backup_path)} (original, untouched)<br><b>Output:</b> {e(rep.output_path)}<br>
<b>Session:</b> {e(rep.session_dir)}<br><b>EPUBCheck before:</b> {e(rep.epubcheck_before)} &nbsp;
<b>after (packaged file):</b> {e(rep.epubcheck_after)}<br><b>Passes:</b> {rep.passes_run} &nbsp;
<b>Repairs:</b> {rep.committed} applied, {rep.rolled_back_repairs} rolled back, {len(rep.remaining_issues)} manual review</p>
<h2>Validation dashboard</h2>{tiles}
<h2>Remaining issues ({len(remaining)})</h2>
{_table(["Code", "Severity", "File", "Line", "Message", "Root cause", "Attempted repair", "Why not fixed",
         "Recommended manual action"], remaining) if remaining else "<p>None.</p>"}
{client}
<h2>Repair plan (before any change)</h2>
{_table(["#", "Root cause", "Codes", "Errors", "Files", "Level", "Action", "Cascade"], plan_rows)}
<h2>Repair history</h2>
{_table(["#", "Pass", "Level", "Repair", "Codes", "Changes (before -> after)", "Confidence", "Integrity",
         "EPUBCheck", "Status", "Reason"], hist_rows)}
<h2>Data integrity</h2><p>{e(rep.content_integrity)}</p>
{_table(["Measure", "Original", "Final", "Difference"], totals)}
{_table(["Severity", "Category", "File", "Detail"], findings) if findings else ""}
<h2>Image mapping</h2>{_table(["Image", "Size", "SHA-256", "Bytes", "References"], images)}
<h2>Page markers</h2>{_table(["Document", "Labels", "Before", "After"], pm) if pm else "<p>No page markers.</p>"}
<h2>Links and fragments</h2>{_table(["Document", "Unresolved reference"], links) if links else
"<p>Every internal link and #fragment resolves.</p>"}
<h2>Split documents (reading order)</h2>{_table(["#", "Document", "Words", "IDs"], splits)}
<h2>EPUBCheck - original ({len(ec_before)})</h2>{_table(["Code", "Severity", "File", "Line", "Message"], ec_before)}
<h2>EPUBCheck - final packaged EPUB ({len(ec_after)})</h2>{_table(["Code", "Severity", "File", "Line", "Message"],
                                                                     ec_after) if ec_after else "<p>No messages.</p>"}
<h2>Final package</h2>{_table(["File", "Bytes", "SHA-256"], files)}
</body></html>"""
