"""Auto-Zoning / Auto-Tagging engine: proposes zones for a new PDF from one
or more already-manually-zoned reference projects, using deterministic
layout/semantic scoring (no ML). Fully isolated from core/gui - only calls
their existing public functions (zone_manager.add_zone, project_manager
save/load, validation.find_overlapping_zone_ids, ...); never modifies
manual zoning, XML generation, or the project file format."""
