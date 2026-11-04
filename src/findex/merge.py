"""Злиття відсортованих списків doc_id: AND / OR / NOT (лаба 2).

Два рушії з однаковим інтерфейсом:
    merge — двовказівникове злиття, O(len(a) + len(b)), без хешування;
    set   — set(a) & set(b) тощо з подальшим сортуванням.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

type DocIds = Sequence[int]
type EngineName = Literal["merge", "set"]


def intersect(a: DocIds, b: DocIds) -> list[int]:
    """AND: обидва списки відсортовані; рухаємо вказівник на меншому елементі."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def union(a: DocIds, b: DocIds) -> list[int]:
    """OR: те саме злиття, але віддаємо кожен doc_id один раз."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: DocIds, b: DocIds) -> list[int]:
    """a NOT b: усе з a, чого немає в b."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na:
        if j >= nb:
            out.extend(a[i:])
            break
        x, y = a[i], b[j]
        if x == y:
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            j += 1
    return out


def set_intersect(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) & set(b))


def set_union(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) | set(b))


def set_difference(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) - set(b))


type MergeFn = Callable[[DocIds, DocIds], list[int]]


@dataclass(frozen=True, slots=True)
class Engine:
    and_: MergeFn
    or_: MergeFn
    not_: MergeFn


ENGINES: dict[EngineName, Engine] = {
    "merge": Engine(intersect, union, difference),
    "set": Engine(set_intersect, set_union, set_difference),
}
