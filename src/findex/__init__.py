"""findex — навчальна пошукова система.

Лаба 1: потокове читання корпусу (corpus), токенізація (tokenize) і статистика (stats).
"""

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize

__all__ = ["Document", "iter_documents", "tokenize"]
