"""Tests for money helpers (Decimal-only, GST split, discount NPV)."""

import pytest
from decimal import Decimal

from app.domain.money import (
    early_payment_discount_value,
    split_gst,
    tax_amount,
    to_money,
)


def test_to_money_rejects_float():
    with pytest.raises(TypeError):
        to_money(1.5)


def test_to_money_rounds_half_up():
    assert to_money("10.005") == Decimal("10.01")
    assert to_money(10) == Decimal("10.00")


def test_tax_amount():
    assert tax_amount(Decimal("1000"), Decimal("18")) == Decimal("180.00")


def test_split_gst_intra_state_sums_to_tax():
    split = split_gst(Decimal("180.00"), intra_state=True)
    assert split["cgst"] + split["sgst"] == Decimal("180.00")
    assert split["igst"] == Decimal("0.00")
    assert split["cgst"] == Decimal("90.00")


def test_split_gst_inter_state_all_igst():
    split = split_gst(Decimal("180.00"), intra_state=False)
    assert split["igst"] == Decimal("180.00")
    assert split["cgst"] == Decimal("0.00")


def test_early_payment_discount_pays_when_discount_beats_cost():
    # 2% discount for paying 20 days early at 12% cost of capital:
    # discount ₹2,000 vs opportunity cost ₹1,000*20/365*12% ≈ ₹657.
    discount, cost, pay_early = early_payment_discount_value(
        Decimal("100000"), Decimal("2"), 20, Decimal("12")
    )
    assert discount == Decimal("2000.00")
    assert pay_early is True


def test_early_payment_discount_skips_when_cost_beats_discount():
    discount, cost, pay_early = early_payment_discount_value(
        Decimal("100000"), Decimal("0.5"), 20, Decimal("12")
    )
    assert pay_early is False
