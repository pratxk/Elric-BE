"""Line-item alignment via the Hungarian algorithm (Kuhn–Munkres), pure Python.

Invoice lines and PO lines rarely arrive in the same order or with identical
descriptions. We build a similarity matrix (description similarity + qty +
price proximity) and solve the optimal assignment, then match aligned pairs.
No scipy — keeps the Docker image small.
"""

from __future__ import annotations

import re
from decimal import Decimal


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def description_similarity(a: str, b: str) -> float:
    """Jaccard similarity over normalized word tokens."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _closeness(a: Decimal, b: Decimal) -> float:
    """1.0 when equal, decaying to 0 as relative difference grows."""
    if a == b:
        return 1.0
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 1.0
    return max(0.0, 1.0 - float(abs(a - b) / denom))


def pair_similarity(
    inv_desc: str,
    inv_qty: Decimal,
    inv_price: Decimal,
    po_desc: str,
    po_qty: Decimal,
    po_price: Decimal,
) -> float:
    """Weighted similarity: description 0.5, qty 0.25, price 0.25."""
    return (
        0.5 * description_similarity(inv_desc, po_desc)
        + 0.25 * _closeness(inv_qty, po_qty)
        + 0.25 * _closeness(inv_price, po_price)
    )


def _hungarian_max(cost: list[list[float]]) -> list[int]:
    """Maximum-weight assignment for a square matrix.

    Classic O(n^3) Kuhn–Munkres on potentials, implemented for maximisation
    by negating the matrix. Returns assignment[row] = column (or -1).
    """
    n = len(cost)
    if n == 0:
        return []
    # Convert to minimisation with 1-indexed arrays (standard formulation).
    a = [[0.0] * (n + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(n):
            a[i + 1][j + 1] = -cost[i][j]
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row matched to column j
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = a[i0][j] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assignment = [-1] * n
    for j in range(1, n + 1):
        if p[j]:
            assignment[p[j] - 1] = j - 1
    return assignment


def align_lines(
    invoice_lines: list[dict],
    po_lines: list[dict],
    min_similarity: float = 0.30,
) -> list[dict]:
    """Align invoice lines to PO lines.

    Each line dict needs keys: description, qty (Decimal), unit_price (Decimal).
    Returns a list of {invoice_idx, po_idx, similarity} for pairs at or above
    the threshold. Unaligned lines are simply absent.
    """
    if not invoice_lines or not po_lines:
        return []
    n = max(len(invoice_lines), len(po_lines))
    matrix = [[0.0] * n for _ in range(n)]
    for i, inv in enumerate(invoice_lines):
        for j, po in enumerate(po_lines):
            matrix[i][j] = pair_similarity(
                inv["description"], inv["qty"], inv["unit_price"],
                po["description"], po["qty"], po["unit_price"],
            )
    assignment = _hungarian_max(matrix)
    pairs = []
    for i, j in enumerate(assignment):
        if j != -1 and j < len(po_lines) and matrix[i][j] >= min_similarity:
            pairs.append(
                {"invoice_idx": i, "po_idx": j, "similarity": round(matrix[i][j], 4)}
            )
    return pairs
