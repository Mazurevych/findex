"""Сніпети: вікно ±80 символів навколо найкращого збігу з підсвіченими термами.

Позиції в індексі — це номери ТОКЕНІВ, а для сніпета потрібні СИМВОЛИ оригінального
тексту. Тому проходимо тим самим регулярним виразом, що й tokenize, по тексту без
casefold (довжина рядка не змінюється) і нормалізуємо кожен збіг окремо: так
`match.start()` вказує в оригінальний текст, а порівнюємо ми нормалізовані терми.

«Найкраще» вікно — те, де трапляється найбільше РІЗНИХ термів запиту (за рівності —
найраніше).
"""

from __future__ import annotations

import bisect
import logging
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

from findex.index import Index
from findex.tokenize import WORD_RE, normalize

log = logging.getLogger(__name__)

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'"})
_SPACES = re.compile(r"\s+")
_RULES = re.compile(r"[=\-~^*#+`]{4,}")  # підкреслення заголовків rST: ===== ----- ~~~~~


def _clean(text: str) -> str:
    return _SPACES.sub(" ", _RULES.sub(" ", text))


type Segment = tuple[str, bool]  # (текст, чи підсвічувати)


@dataclass(frozen=True, slots=True)
class Snippet:
    segments: tuple[Segment, ...]
    starts_mid: bool  # вікно почалося не з початку документа -> «…» спереду
    ends_mid: bool

    def render(self, open_mark: str = "«", close_mark: str = "»") -> str:
        body = "".join(f"{open_mark}{t}{close_mark}" if hit else t for t, hit in self.segments)
        return ("…" if self.starts_mid else "") + body.strip() + ("…" if self.ends_mid else "")

    def __str__(self) -> str:
        return self.render()


EMPTY = Snippet((), False, False)


def make_snippet(text: str, terms: Collection[str], radius: int = 80) -> Snippet:
    """Вікно ±radius символів навколо місця, де зібралось найбільше різних термів."""
    wanted = set(terms)
    original = unicodedata.normalize("NFC", text)
    # шукаємо по тексту з уніфікованими апострофами (заміна 1:1, позиції не зсуваються),
    # а показуємо оригінал — щоб ``code`` у rST не перетворився на ''code''
    prepared = original.translate(_APOSTROPHES)
    hits: list[tuple[int, int, str]] = [
        (m.start(), m.end(), term)
        for m in WORD_RE.finditer(prepared)
        if (term := normalize(m.group())) in wanted
    ]
    if not hits:
        return Snippet(((_clean(original[: 2 * radius]), False),), False, True)

    starts = [h[0] for h in hits]
    best_i, best_count = 0, 0
    for i, (start, _, _) in enumerate(hits):
        lo = bisect.bisect_left(starts, start - radius)
        hi = bisect.bisect_right(starts, start + radius)
        count = len({h[2] for h in hits[lo:hi]})
        if count > best_count:
            best_i, best_count = i, count

    center_start, center_end, _ = hits[best_i]
    lo = max(0, center_start - radius)
    hi = min(len(prepared), center_end + radius)
    # не різати слова навпіл: зсуваємо межі до найближчих пробілів
    if lo > 0 and (space := prepared.find(" ", lo, center_start)) != -1:
        lo = space + 1
    if hi < len(prepared) and (space := prepared.rfind(" ", center_end, hi)) != -1:
        hi = space

    segments: list[Segment] = []
    cursor = lo
    for start, end, _ in hits:
        if start < lo or end > hi:
            continue
        if start > cursor:
            segments.append((_clean(original[cursor:start]), False))
        segments.append((original[start:end], True))
        cursor = end
    if cursor < hi:
        segments.append((_clean(original[cursor:hi]), False))
    return Snippet(tuple(segments), lo > 0, hi < len(prepared))


def snippet_for(index: Index, doc_id: int, terms: Collection[str], radius: int = 80) -> Snippet:
    """Сніпет для документа індексу: читає файл з корпусу (якщо він доступний)."""
    path = index.document_path(doc_id)
    if path is None or not path.is_file():
        log.debug("no source file for doc %d (%s)", doc_id, path)
        return EMPTY
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("%s: не вдалося прочитати для сніпета (%s)", path, exc)
        return EMPTY
    return make_snippet(text, terms, radius)


def highlight_segments(text: str, terms: Collection[str]) -> tuple[Segment, ...]:
    """Увесь текст на сегменти «звичайний / підсвічений» — для перегляду документа.

    На відміну від сніпета, пробіли й переноси рядків зберігаються.
    """
    wanted = set(terms)
    original = unicodedata.normalize("NFC", text)
    prepared = original.translate(_APOSTROPHES)
    segments: list[Segment] = []
    cursor = 0
    if wanted:
        for m in WORD_RE.finditer(prepared):
            if normalize(m.group()) in wanted:
                if m.start() > cursor:
                    segments.append((original[cursor : m.start()], False))
                segments.append((original[m.start() : m.end()], True))
                cursor = m.end()
    if cursor < len(original):
        segments.append((original[cursor:], False))
    return tuple(segments)


def read_document(index: Index, doc_id: int) -> str | None:
    """Текст документа з корпусу (або None, якщо файлу немає — напр. сторінка з краулера)."""
    path = index.document_path(doc_id)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("%s: не вдалося прочитати (%s)", path, exc)
        return None
