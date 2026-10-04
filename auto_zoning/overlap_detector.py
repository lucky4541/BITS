"""Thin re-export of the project's existing, unmodified overlap-detection
algorithm (core.validation) for the auto-zoning summary count - never a
second/competing implementation."""
from core import validation


def check(zone_manager) -> set:
    return validation.find_overlapping_zone_ids(zone_manager)
