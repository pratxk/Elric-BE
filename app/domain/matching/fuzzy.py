"""Fuzzy vendor matching — Jaro-Winkler, pure Python.

Vendor names differ across docs ("Acme Corp" vs "ACME Corporation Pvt Ltd").
Names are normalized (case, punctuation, legal suffixes) then scored with
Jaro-Winkler against the canonical vendor registry.
"""

from __future__ import annotations

import re

_SUFFIXES = {
    "pvt", "ltd", "limited", "inc", "corp", "corporation", "llp", "llc",
    "co", "company", "private", "india", "technologies", "technology",
    "solutions", "services", "labs", "industries",
}


def normalize_vendor_name(name: str) -> str:
    """Lowercase, strip punctuation, drop generic legal/industry suffixes."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    kept = [t for t in tokens if t not in _SUFFIXES]
    return " ".join(kept)


def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    match_distance = max(len1, len2) // 2 - 1
    match_distance = max(match_distance, 0)
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    t = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    t //= 2
    return (matches / len1 + matches / len2 + (matches - t) / matches) / 3.0


def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Jaro-Winkler: Jaro plus a bonus for a shared prefix (max 4 chars)."""
    j = jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2):
        if a == b and prefix < 4:
            prefix += 1
        else:
            break
    return j + prefix * prefix_weight * (1.0 - j)


def vendor_similarity(name_a: str, name_b: str) -> float:
    """Similarity between two raw vendor names (0..1)."""
    na, nb = normalize_vendor_name(name_a), normalize_vendor_name(name_b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return jaro_winkler(na, nb)


def best_vendor_match(
    name: str, registry: list[str], threshold: float = 0.85
) -> tuple[str | None, float]:
    """Best registry match at or above threshold, else (None, best score)."""
    best_name, best_score = None, 0.0
    for candidate in registry:
        score = vendor_similarity(name, candidate)
        if score > best_score:
            best_name, best_score = candidate, score
    if best_score >= threshold:
        return best_name, best_score
    return None, best_score
