"""Tests for Hungarian alignment and Jaro-Winkler vendor matching."""

from decimal import Decimal

from app.domain.matching.aligner import align_lines, description_similarity
from app.domain.matching.fuzzy import (
    best_vendor_match,
    jaro_winkler,
    normalize_vendor_name,
    vendor_similarity,
)


def test_description_similarity_identical():
    assert description_similarity("Dell Latitude 5540", "Dell Latitude 5540") == 1.0


def test_align_lines_reorders():
    # Invoice lines arrive in the opposite order to the PO lines.
    inv = [
        {"description": "Wireless Mouse Logitech MX", "qty": Decimal("5"), "unit_price": Decimal("2500")},
        {"description": "Laptop Dell Latitude 5540", "qty": Decimal("2"), "unit_price": Decimal("65000")},
    ]
    po = [
        {"description": "Laptop Dell Latitude 5540", "qty": Decimal("2"), "unit_price": Decimal("65000")},
        {"description": "Wireless Mouse Logitech MX", "qty": Decimal("5"), "unit_price": Decimal("2500")},
    ]
    pairs = align_lines(inv, po)
    assert len(pairs) == 2
    by_inv = {p["invoice_idx"]: p["po_idx"] for p in pairs}
    assert by_inv[0] == 1  # mouse -> mouse
    assert by_inv[1] == 0  # laptop -> laptop
    assert all(p["similarity"] > 0.9 for p in pairs)


def test_align_lines_unmatched_below_threshold():
    inv = [{"description": "Unrelated thing", "qty": Decimal("1"), "unit_price": Decimal("10")}]
    po = [{"description": "Laptop Dell Latitude", "qty": Decimal("2"), "unit_price": Decimal("65000")}]
    assert align_lines(inv, po) == []


def test_jaro_winkler_identical():
    assert jaro_winkler("acme", "acme") == 1.0


def test_normalize_vendor_name_strips_suffixes():
    assert normalize_vendor_name("ACME Corporation Pvt Ltd") == "acme"


def test_vendor_similarity_variants():
    assert vendor_similarity("Acme Corp", "ACME Corporation Pvt Ltd") > 0.9
    assert vendor_similarity("Acme Corp", "Totally Different Ltd") < 0.7


def test_best_vendor_match_threshold():
    registry = ["Acme Corporation Pvt Ltd", "Globex Industries"]
    name, score = best_vendor_match("Acme Corp", registry)
    assert name == "Acme Corporation Pvt Ltd"
    assert score >= 0.85
    name, score = best_vendor_match("Unknown Vendor XYZ", registry)
    assert name is None
