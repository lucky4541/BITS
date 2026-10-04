"""Patch EPUBForge source tree with Auto Split Based on Numbers.
Run from the EPUBForge project root: python apply_auto_split_numbers.py
Creates .bak files before editing.
"""
from pathlib import Path
import shutil

ROOT=Path.cwd()
ZM=ROOT/'core'/'zone_manager.py'
MW=ROOT/'gui'/'main_window.py'
HELP=ROOT/'core'/'auto_split_numbered_notes.py'

helper_src=Path(__file__).with_name('core_auto_split_numbered_notes.py')
if not ZM.exists() or not MW.exists():
    raise SystemExit('Run this from the EPUBForge project root containing core/ and gui/.')
HELP.write_text(helper_src.read_text(encoding='utf-8'),encoding='utf-8')

zm=ZM.read_text(encoding='utf-8')
if 'def auto_split_numbered_notes(' not in zm:
    marker='    # ---------- defensive hierarchy validation ----------\n'
    method='''    # ---------- auto split based on numbers ----------\n    def auto_split_numbered_notes(self, parent_zone_id: str, heading_tag="h2", note_tag="fn", page_marker_tag=None):\n        """Analyze ONLY the selected zone and split it using the existing Horizontal Split primitive."""\n        parent = self.zones.get(parent_zone_id)\n        if not parent or not self.pdf_document:\n            return {"ok": False, "message": "Select a zone with an open PDF first."}\n        from core.auto_split_numbered_notes import detect\n        page = self.pdf_document.get_page(parent.page)\n        result = detect(page, parent.bbox, text_extractor)\n        if not result.boundaries:\n            return {"ok": False, "message": "\\n".join(result.warnings or ["No safe numbered-note boundaries found."])}\n        self.begin_batch()\n        try:\n            created = self.split_zone(parent_zone_id, result.boundaries, child_tag=note_tag, axis="h")\n            if not created:\n                return {"ok": False, "message": "Split created no child zones."}\n            hb, pb = result.heading_boundary, result.page_boundary\n            for child in created:\n                cy0, cy1 = child.bbox[1], child.bbox[3]\n                if result.heading_detected and hb is not None and cy0 < hb and cy1 <= hb + 1:\n                    child.tag = heading_tag\n                    child.level = self._compute_level(heading_tag, parent_zone_id)\n                    child.attributes.update({"auto_split": "numbered_notes", "role": "notes_heading"})\n                elif pb is not None and cy0 >= pb - 1:\n                    if page_marker_tag:\n                        child.tag = page_marker_tag\n                        child.level = self._compute_level(page_marker_tag, parent_zone_id)\n                        child.attributes.update({"auto_split": "numbered_notes", "role": "printed_page_number"})\n                    else:\n                        child.attributes.update({"auto_split": "numbered_notes", "role": "printed_page_number_unmapped"})\n                else:\n                    child.tag = note_tag\n                    child.level = self._compute_level(note_tag, parent_zone_id)\n                    child.attributes.update({"auto_split": "numbered_notes", "role": "note"})\n                self._refresh_text(child)\n            self.normalize_reading_order(parent.page)\n            return {"ok": True, "created": created, "notes": result.note_count,\n                    "heading": result.heading_detected, "page_number": result.page_detected,\n                    "first_note_number": result.first_note_number, "warnings": result.warnings}\n        finally:\n            self.end_batch()\n\n'''
    if marker not in zm:
        raise SystemExit('ZoneManager marker not found')
    shutil.copy2(ZM, str(ZM)+'.bak')
    zm=zm.replace(marker,method+marker,1)
    ZM.write_text(zm,encoding='utf-8')

mw=MW.read_text(encoding='utf-8')
if 'def auto_split_based_on_numbers(' not in mw:
    marker='    # ---------------- horizontal split ----------------\n'
    method='''    def auto_split_based_on_numbers(self, zone_id=None):\n        """Auto Split Based on Numbers: selected complete notes/endnotes block only."""\n        zone_id = zone_id or self.selected_zone_id\n        if not zone_id:\n            messagebox.showinfo("Auto Split Based on Numbers", "Select the complete notes/endnotes zone first.")\n            return\n        zone = self.zone_manager.zones.get(zone_id)\n        if not zone:\n            return\n        foot_tags = set(self.active_profile.get("footnote_flow_tags") or ["fn", "en"])\n        note_tag = zone.tag if zone.tag in foot_tags else ("fn" if "fn" in foot_tags else next(iter(foot_tags), "fn"))\n        marker_tags = set(self.active_profile.get("page_marker_tags") or [])\n        page_marker_tag = next(iter(marker_tags), None)\n        try:\n            result = self.zone_manager.auto_split_numbered_notes(zone_id, heading_tag="h2", note_tag=note_tag, page_marker_tag=page_marker_tag)\n        except Exception as exc:\n            debug_log.log("AUTO_SPLIT_NUMBERS", f"failed zone={zone_id}: {exc}")\n            messagebox.showerror("Auto Split Based on Numbers", f"Auto split failed:\\n\\n{exc}")\n            return\n        if not result.get("ok"):\n            messagebox.showinfo("Auto Split Based on Numbers", result.get("message", "No split performed."))\n            return\n        self.on_zones_changed()\n        msg=(f"Split complete.\\n\\nNotes created: {result.get('notes',0)}\\n"\n             f"Heading detected: {'Yes' if result.get('heading') else 'No'}\\n"\n             f"Page number detected: {'Yes' if result.get('page_number') else 'No'}\\n"\n             f"First note number: {result.get('first_note_number','—')}")\n        if result.get("page_number") and not page_marker_tag:\n            msg += "\\n\\nWARNING: the active profile has no Page Number tag configured, so the detected folio was left unmapped rather than inventing a tag."\n        self.set_status(f"Auto Split: {result.get('notes',0)} notes")\n        messagebox.showinfo("Auto Split Based on Numbers", msg)\n\n'''
    if marker not in mw:
        raise SystemExit('MainWindow marker not found')
    shutil.copy2(MW, str(MW)+'.bak')
    mw=mw.replace(marker,method+marker,1)

old='self.context_menu.add_command(label="Horizontal Split", command=lambda: self.start_horizontal_split(zone_id))'
if 'label="Auto Split Based on Numbers"' not in mw:
    if old not in mw:
        raise SystemExit('Context-menu split command not found')
    mw=mw.replace(old,old+'\n        self.context_menu.add_command(label="Auto Split Based on Numbers", command=lambda: self.auto_split_based_on_numbers(zone_id))',1)

needle='        self.root.bind("<Return>", lambda e: self._confirm_split() if self.viewer.mode == "split" else None)'
if '<Control-Shift-N>' not in mw:
    if needle not in mw:
        raise SystemExit('Keyboard binding marker not found')
    mw=mw.replace(needle,needle+'\n        for seq in ("<Control-Shift-N>", "<Control-Shift-n>"):\n            self.root.bind(seq, lambda e: self.auto_split_based_on_numbers())',1)
MW.write_text(mw,encoding='utf-8')
print('PATCH COMPLETE')
print('Added context menu: Auto Split Based on Numbers')
print('Added shortcut: Ctrl+Shift+N')
print('Backups: core/zone_manager.py.bak and gui/main_window.py.bak')
