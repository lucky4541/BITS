"""EPUB Structure / Package Builder - a standalone module that analyzes an
already-generated EPUB project directory (the final XHTML/OPF/CSS/images
the existing Zoning engine produces) and generates/repairs its navigation
document (NAV: toc/landmarks/page-list), OPF manifest/spine, and NCX,
resolving every link to a real, existing target.

Completely independent of core.zone_manager / core.ocr / auto_zoning /
core.epub_xml_generator / core.hierarchy / core.mapping_engine / gui.
main_window's generation pipeline - this package only ever READS the
XHTML files those already produced and writes new/updated NAV/OPF/NCX
documents next to them. It never touches Zoning, OCR, Reading Order,
Split/Merge, Table/Index detection, or the existing core.epub/ (EPUB-ZIP-
based validation/repair) subsystem.

See core/epub_structure/orchestrator.py for the single entry point."""
