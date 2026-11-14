"""Tests for the invoice lifecycle state machine (§19i)."""

import pytest

from app.domain.lifecycle import allowed_events, transition
from app.domain.models import InvoiceStatus as S


def test_happy_path():
    assert transition(S.RECEIVED, "extract") == S.EXTRACTED
    assert transition(S.EXTRACTED, "validate") == S.VALIDATED
    assert transition(S.VALIDATED, "match") == S.MATCHED
    assert transition(S.MATCHED, "auto_approve") == S.APPROVED
    assert transition(S.APPROVED, "post") == S.POSTED
    assert transition(S.POSTED, "schedule") == S.SCHEDULED
    assert transition(S.SCHEDULED, "pay") == S.PAID


def test_flagged_path():
    assert transition(S.MATCHED, "flag") == S.FLAGGED
    assert transition(S.FLAGGED, "approve") == S.APPROVED
    assert transition(S.FLAGGED, "reject") == S.REJECTED


def test_side_states():
    assert transition(S.RECEIVED, "mark_duplicate") == S.DUPLICATE
    assert transition(S.MATCHED, "hold") == S.ON_HOLD
    assert transition(S.ON_HOLD, "resume") == S.RECEIVED


def test_illegal_transition_raises():
    with pytest.raises(ValueError):
        transition(S.RECEIVED, "pay")
    with pytest.raises(ValueError):
        transition(S.PAID, "approve")


def test_allowed_events_lists_legal_moves():
    assert "extract" in allowed_events(S.RECEIVED)
    assert "pay" not in allowed_events(S.RECEIVED)
