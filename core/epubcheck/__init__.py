"""Real W3C EPUBCheck integration (spec: "EPUBForge / ZoneTool - Unified
EPUB Editor + Validator + Universal Auto-Fix Engine", sections 9/46 -
"REAL EPUBCHECK... DO NOT simulate EPUBCheck"). This package is validation-
only: it runs the actual bundled epubcheck.jar and parses its own real
JSON output - never a custom/fake validator standing in for it.
"""
