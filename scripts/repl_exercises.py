"""П'ять дослідів «Prove it to yourself» з лаби 1 — одним скриптом.

    uv run python scripts/repl_exercises.py

Перед кожним блоком спробуйте сказати вголос, що буде надруковано.
"""

import tempfile
import tracemalloc
import unicodedata
from pathlib import Path


def section(title: str) -> None:
    print(f"\n=== {title} ===")


section("1. Ітератор вичерпується, список — ні")
xs = [1, 2, 3]
it = iter(xs)
print("next(it) =", next(it), "| xs =", xs)
print("list(it) =", list(it))  # [2, 3] — ітератор пам'ятає позицію
print("list(it) =", list(it))  # [] — вичерпаний назавжди
print("xs       =", xs)  # список цілий: кожен iter(xs) — новий ітератор


section("2. Список vs генераторний вираз під tracemalloc")
for label, make in [
    ("sum([x*x ...])", lambda: sum([x * x for x in range(10**7)])),
    ("sum(x*x ...)  ", lambda: sum(x * x for x in range(10**7))),
]:
    tracemalloc.start()
    make()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    print(f"{label}: пік {peak / 1024 / 1024:8.2f} MiB")
# У лінивій версії в RAM: сам range (3 числа: start/stop/step), один x і сума.


section("3. Звичайна функція vs генератор")


def ordinary(n):
    print("  start", n)
    return n


def countdown(n):
    print("  start", n)
    while n > 0:
        yield n
        n -= 1


print("ordinary(3):")
ordinary(3)  # тіло виконується одразу
print("countdown(3):")
gen = countdown(3)  # нічого не друкує: лише створено generator object
print(" ", type(gen))
print("next(gen):")
print("  ->", next(gen))  # тепер тіло дійшло до першого yield


section("4. Два «café»")
a, b = "café", "café"
print(f"{a!r} == {b!r}:", a == b, "| довжини:", len(a), len(b))
print("після NFC:", unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b))


section("5. Файл — теж ітератор")
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "f.txt"
    p.write_text("рядок 1\nрядок 2\n", encoding="utf-8")
    with p.open(encoding="utf-8") as f:
        print("перший прохід:", [line.rstrip() for line in f])
        print("другий прохід:", [line.rstrip() for line in f])  # [] — як у п.1
        print("iter(f) is f:", iter(f) is f)  # файл сам собі ітератор
