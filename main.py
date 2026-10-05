"""BITS Tool entry point. Run with: python main.py"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _check_dependencies():
    missing = []
    for module, package in (("fitz", "PyMuPDF"), ("PIL", "Pillow"), ("lxml", "lxml"),
                             ("spellchecker", "pyspellchecker")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Tkinter is not available in this Python installation. "
              "Install a Python build with Tk support (standard on python.org installers).")
        sys.exit(1)
    if missing:
        print("Missing required dependencies: " + ", ".join(missing))
        print("Install them with:")
        print(f"    pip install {' '.join(missing)}")
        print("or:")
        print("    pip install -r requirements.txt")
        sys.exit(1)


def _selfcheck_resources():
    """`BITSTool.exe --selfcheck-resources` - runs the REAL, built EXE
    (onedir or onefile) and confirms every bundled resource EPUBForge
    needs at runtime actually resolves via core.resource_path, without
    needing a display/GUI at all. This is what packaging/check_package.py
    launches to verify a onefile build (see its verify_onefile()): a
    onefile EXE has no _internal/ folder to inspect from outside, so the
    only genuine way to confirm its bundle is complete is to ask the
    running EXE itself. Prints one line per resource and exits 0 only if
    every required resource is present."""
    from core.resource_path import selfcheck_missing, required_resource_list
    missing = set(selfcheck_missing())
    for rel in required_resource_list():
        print(("RESOURCE_MISSING: " if rel in missing else "RESOURCE_OK: ") + rel)

    # pyspellchecker's own dictionary is loaded via pkgutil.get_data against
    # the package's OWN on-disk location, not core.resource_path - a onedir/
    # onefile-relative file check (like the loop above) doesn't cover it, so
    # the one genuinely reliable check is constructing the real object and
    # confirming it actually recognizes an ordinary English word.
    spellchecker_ok = False
    try:
        from spellchecker import SpellChecker
        spellchecker_ok = bool(SpellChecker().known(["the"]))
    except Exception as exc:  # noqa: BLE001 - reported below, never crashes the self-check itself
        print(f"RESOURCE_MISSING: spellchecker dictionary ({exc})")
    if spellchecker_ok:
        print("RESOURCE_OK: spellchecker dictionary")
    else:
        missing.add("spellchecker dictionary")

    print("SELFCHECK: " + ("FAIL" if missing else "PASS"))
    sys.exit(1 if missing else 0)


def _selfcheck_full():
    """`BITSTool.exe --selfcheck-full` - a GUI-less, real end-to-end
    functional test run INSIDE the actual packaged build (onedir or
    onefile): open a real PDF, run PaddleOCR against it, zone the result,
    generate XHTML, and save/reload a project - the same stack a real
    user session exercises, without needing UI automation against a
    Tkinter window (which exposes no real accessibility tree to drive
    from outside). Prints one PASS/FAIL line per stage and exits non-zero
    if anything fails - "the build finished without error" is never
    treated as proof the packaged app actually works (packaging spec:
    "test the actual packaged application")."""
    import tempfile
    import shutil
    results = []

    def _check(name, fn):
        try:
            fn()
            print(f"[PASS] {name}")
            results.append(True)
        except Exception as exc:  # noqa: BLE001 - every failure is reported, never crashes the run
            import traceback
            print(f"[FAIL] {name} - {exc}")
            print("--- full traceback (including any chained __cause__) ---")
            traceback.print_exc()
            print("--- end traceback ---")
            results.append(False)

    tmp_dir = tempfile.mkdtemp(prefix="bitstool_selfcheck_")
    state = {}

    def _make_pdf():
        import fitz
        path = os.path.join(tmp_dir, "selfcheck.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=200)
        page.insert_text((50, 60), "BITS Tool packaging self-check.", fontsize=14)
        doc.save(path)
        doc.close()
        state["pdf_path"] = path

    def _open_pdf():
        from core.pdf_loader import PDFDocument
        pdf = PDFDocument(state["pdf_path"])
        assert pdf.page_count == 1
        state["pdf"] = pdf

    def _extract_text():
        from core.text_extractor import extract_plain_text
        page = state["pdf"].get_page(1)
        text = extract_plain_text(page, [0, 0, 400, 200])
        assert "packaging self-check" in text, f"unexpected text: {text!r}"

    def _zone_and_generate_xml():
        from core.zone_manager import ZoneManager
        from core.bits import pipeline
        zm = ZoneManager(state["pdf"])
        zm.add_zone(1, "p", [40, 45, 360, 85])
        out = os.path.join(tmp_dir, "output", "selfcheck.xml")
        res = pipeline.generate(zm, state["pdf"], "JATS", out, os.path.join(tmp_dir, "output", "images"),
                                prefix="selfcheck")
        with open(out, encoding="utf-8") as f:
            xml = f.read()
        assert "packaging self-check" in xml, f"generated XML missing expected text: {xml}"
        assert res.text_preserved, res.summary()
        state["zone_manager"] = zm

    def _save_and_reload_project():
        from core import project_manager
        path = os.path.join(tmp_dir, "selfcheck_project.json")
        project_manager.save_project(path, state["pdf_path"], 150, 1.0, state["zone_manager"])
        data = project_manager.load_project(path)
        from core.zone_manager import ZoneManager
        zm2 = ZoneManager(state["pdf"])
        project_manager.load_into_zone_manager(data, zm2)
        assert len(zm2.zones) == 1, f"expected 1 zone after reload, got {len(zm2.zones)}"

    def _paddleocr_initializes():
        from core.ocr.ocr_engine import get_engine
        engine = get_engine("PaddleOCR")
        assert engine.is_available(), "PaddleOCR reports not available in this build"
        ok, message = engine.test_initialization()
        assert ok, f"PaddleOCR failed to initialize: {message}"

    def _ocr_recognizes_real_text():
        from PIL import Image, ImageDraw
        from core.ocr.ocr_engine import get_engine
        img = Image.new("RGB", (400, 100), color="white")
        draw = ImageDraw.Draw(img)
        draw.text((10, 30), "Packaging Selfcheck", fill="black")
        engine = get_engine("PaddleOCR")
        result = engine.recognize(img, language="en", options={})
        assert result is not None and not getattr(result, "error", None), \
            f"OCR returned an error: {getattr(result, 'error', None)}"
        text = " ".join(b.text for b in (result.blocks or []))
        assert text.strip(), "OCR returned no recognized text at all"

    def _spellchecker_works():
        from spellchecker import SpellChecker
        checker = SpellChecker()
        assert checker.known(["the"]), "SpellChecker did not recognize an ordinary English word"

    _check("make a real synthetic PDF (proves PyMuPDF write path)", _make_pdf)
    _check("open the PDF (core.pdf_loader.PDFDocument)", _open_pdf)
    _check("extract text from the PDF (core.text_extractor)", _extract_text)
    _check("zone it and generate JATS XML (core.bits.pipeline)", _zone_and_generate_xml)
    _check("save and reload a project (core.project_manager)", _save_and_reload_project)
    _check("PaddleOCR engine initializes (bundled models load)", _paddleocr_initializes)
    _check("PaddleOCR recognizes real rendered text", _ocr_recognizes_real_text)
    _check("spellchecker dictionary loads and works", _spellchecker_works)

    shutil.rmtree(tmp_dir, ignore_errors=True)
    passed = all(results)
    print("SELFCHECK_FULL: " + ("PASS" if passed else "FAIL"))
    sys.exit(0 if passed else 1)


def main():
    if "--selfcheck-resources" in sys.argv[1:]:
        _selfcheck_resources()
        return
    if "--selfcheck-full" in sys.argv[1:]:
        _selfcheck_full()
        return
    # Always-on startup/crash log (packaging spec 20/36) - installed FIRST,
    # before anything else can fail, so even a missing bundled resource or
    # a broken profile is never silently invisible on a --windowed EXE
    # with no console. Never touches core.debug_log's own separate,
    # opt-in verbose trace.
    from core import app_log
    app_log.install_excepthook()
    app_log.info("BITS Tool starting")
    # A GUI app must not be killed by a stray Ctrl+C reaching the console it
    # was launched from (e.g. "python main.py" in PowerShell: Ctrl+C pressed
    # in that window, or a Ctrl+C event broadcast to the console). That
    # arrived as KeyboardInterrupt inside Tk's mainloop and was shown as
    # "EPUBForge encountered an unexpected error". Close the app with its
    # own window instead. (The packaged EXE has no console, so it never
    # receives this signal anyway.)
    try:
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ImportError, ValueError, OSError):
        pass
    try:
        _check_dependencies()
        from app.launcher.launcher_window import run
        run()
    except Exception as exc:
        app_log.error("Startup failed", exc)
        raise
    app_log.info("BITS Tool exited normally")


if __name__ == "__main__":
    main()
