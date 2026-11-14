"""Invoice lifecycle state machine (§19i).

received → extracted → validated → matched → approved | flagged
→ posted → scheduled → paid, with duplicate / on_hold / rejected as
side states reachable from the early states.
"""

from app.domain.models import InvoiceStatus

# event -> (from, to)
_TRANSITIONS: dict[str, tuple[set[InvoiceStatus], InvoiceStatus]] = {
    "extract": ({InvoiceStatus.RECEIVED}, InvoiceStatus.EXTRACTED),
    "validate": ({InvoiceStatus.EXTRACTED}, InvoiceStatus.VALIDATED),
    "match": ({InvoiceStatus.VALIDATED}, InvoiceStatus.MATCHED),
    "auto_approve": ({InvoiceStatus.MATCHED}, InvoiceStatus.APPROVED),
    "flag": (
        {InvoiceStatus.MATCHED, InvoiceStatus.VALIDATED, InvoiceStatus.EXTRACTED},
        InvoiceStatus.FLAGGED,
    ),
    "approve": ({InvoiceStatus.FLAGGED, InvoiceStatus.MATCHED}, InvoiceStatus.APPROVED),
    "reject": (
        {InvoiceStatus.FLAGGED, InvoiceStatus.MATCHED, InvoiceStatus.VALIDATED},
        InvoiceStatus.REJECTED,
    ),
    "post": ({InvoiceStatus.APPROVED}, InvoiceStatus.POSTED),
    "schedule": ({InvoiceStatus.POSTED}, InvoiceStatus.SCHEDULED),
    "pay": ({InvoiceStatus.SCHEDULED}, InvoiceStatus.PAID),
    "mark_duplicate": (
        {
            InvoiceStatus.RECEIVED,
            InvoiceStatus.EXTRACTED,
            InvoiceStatus.VALIDATED,
            InvoiceStatus.MATCHED,
        },
        InvoiceStatus.DUPLICATE,
    ),
    "hold": (
        {
            InvoiceStatus.RECEIVED,
            InvoiceStatus.EXTRACTED,
            InvoiceStatus.VALIDATED,
            InvoiceStatus.MATCHED,
            InvoiceStatus.FLAGGED,
        },
        InvoiceStatus.ON_HOLD,
    ),
    "resume": ({InvoiceStatus.ON_HOLD}, InvoiceStatus.RECEIVED),
}

TERMINAL_STATES = {
    InvoiceStatus.PAID,
    InvoiceStatus.REJECTED,
    InvoiceStatus.DUPLICATE,
}


def transition(current: InvoiceStatus, event: str) -> InvoiceStatus:
    """Apply an event to a status. Raises ValueError on an illegal move."""
    if event not in _TRANSITIONS:
        raise ValueError(f"unknown lifecycle event: {event!r}")
    allowed_from, target = _TRANSITIONS[event]
    if current not in allowed_from:
        raise ValueError(
            f"illegal lifecycle transition: {current.value} --{event}--> "
            f"{target.value} (allowed from: {sorted(s.value for s in allowed_from)})"
        )
    return target


def allowed_events(current: InvoiceStatus) -> list[str]:
    return [e for e, (src, _) in _TRANSITIONS.items() if current in src]
