#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовый разбор monitor_*.csv — ищем признаки потери/задержки кадров:
общая статистика, максимальная нагрузка в секунду, повторы одинаковых
запросов (например "10 03") с маленьким интервалом."""
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

path = sys.argv[1] if len(sys.argv) > 1 else "logs/monitor_20260904_182544.csv"

rows = []
with open(path, encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    for r in reader:
        rows.append(r)

print(f"Файл: {path}")
print(f"Строк данных: {len(rows)}")
if not rows:
    sys.exit(0)

t0 = datetime.strptime(rows[0]["pc_time_iso"], "%Y-%m-%d %H:%M:%S.%f")
t1 = datetime.strptime(rows[-1]["pc_time_iso"], "%Y-%m-%d %H:%M:%S.%f")
dur = (t1 - t0).total_seconds()
print(f"Период: {rows[0]['pc_time_iso']} -> {rows[-1]['pc_time_iso']}  ({dur:.1f} сек)")
print(f"Средняя частота: {len(rows) / dur:.1f} кадров/сек" if dur > 0 else "")

# нагрузка по секундам (целая секунда pc_time)
per_sec = Counter()
for r in rows:
    per_sec[r["pc_time_iso"][:19]] += 1
top_sec = per_sec.most_common(10)
print("\nТоп-10 самых загруженных секунд:")
for sec, cnt in top_sec:
    print(f"  {sec}: {cnt} кадров")

# поиск повторов одинаковых data_hex с маленьким интервалом (подозрение на дубли/ретраи)
by_data = defaultdict(list)
for r in rows:
    key = (r.get("адрес", ""), r.get("data_hex", ""))
    by_data[key].append(r)

print("\nЗапросы/кадры с одинаковыми данными, повторившиеся с интервалом < 1 сек (топ-15 групп):")
dup_groups = []
for key, items in by_data.items():
    if len(items) < 2:
        continue
    times = [datetime.strptime(it["pc_time_iso"], "%Y-%m-%d %H:%M:%S.%f") for it in items]
    times.sort()
    close_pairs = sum(1 for a, b in zip(times, times[1:]) if (b - a).total_seconds() < 1.0)
    if close_pairs:
        dup_groups.append((close_pairs, key, len(items), times[:5]))
dup_groups.sort(reverse=True)
for close_pairs, key, total, sample_times in dup_groups[:15]:
    addr, data = key
    print(f"  адрес={addr!r} данные={data!r}: всего {total} раз, {close_pairs} пар(ы) с интервалом <1с; примеры времени: {[t.strftime('%H:%M:%S.%f')[:-3] for t in sample_times]}")

print(f"\nВсего групп с 'подозрительными' повторами (<1с): {len(dup_groups)}")

# конкретно "10 03" (DiagnosticSessionControl extendedDiagnosticSession) — то, что упомянул пользователь
sess_rows = [r for r in rows if r.get("data_hex", "").strip().upper().startswith("10 03")]
print(f"\nКадров с данными, начинающимися на '10 03': {len(sess_rows)}")
if sess_rows:
    times = [datetime.strptime(r["pc_time_iso"], "%Y-%m-%d %H:%M:%S.%f") for r in sess_rows]
    times.sort()
    deltas = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    print("Временные интервалы между последовательными '10 03' (сек):", [f"{d:.3f}" for d in deltas[:30]])
    addrs = Counter(r.get("адрес", "") for r in sess_rows)
    print("По каким адресам встречается '10 03':", dict(addrs))

# уникальные адреса
addrs_all = Counter(r.get("адрес", "") for r in rows)
print(f"\nУникальных адресов всего: {len(addrs_all)}")
for addr, cnt in addrs_all.most_common(15):
    print(f"  {addr}: {cnt} кадров")
