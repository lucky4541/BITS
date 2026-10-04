"""EPUB package reading + quick structural validation (spec: "EPUBForge /
ZoneTool - Unified EPUB Editor + Validator", "make only validation part").
Deliberately NOT the full PackageModel/RepairEngine/PackageBuilder
architecture the master spec describes for a future auto-repair pass -
this package exists purely to read an existing .epub and check it,
never to modify or rebuild one."""
