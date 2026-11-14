"""Vendor service — registry lookup with fuzzy matching (§16a.2).

Canonical vendor registry; fuzzy match by Jaro-Winkler with a confidence
threshold; new vendors go through a human-confirm path (never auto-created
from a single document).
"""

from __future__ import annotations

from app.domain.matching.fuzzy import best_vendor_match
from app.domain.models import Vendor

MATCH_THRESHOLD = 0.85


class VendorService:
    def __init__(self, registry: list[Vendor] | None = None):
        self._registry: list[Vendor] = registry or []

    def register(self, vendor: Vendor) -> None:
        self._registry.append(vendor)

    def find(self, name: str) -> tuple[Vendor | None, float, bool]:
        """Returns (vendor, score, needs_human_confirm).

        needs_human_confirm is True when no registry entry clears the
        threshold — a new vendor must be confirmed by a human.
        """
        names = [v.name for v in self._registry]
        match_name, score = best_vendor_match(name, names, MATCH_THRESHOLD)
        if match_name is None:
            return None, score, True
        vendor = next(v for v in self._registry if v.name == match_name)
        return vendor, score, False
