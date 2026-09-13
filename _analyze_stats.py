#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Одноразовый мини-скрипт: печатает stats IsoTpReassembler и число UDS-событий
для каждого CSV, без форматирования полного отчёта. Используется для сравнения
«до фикса» vs «после фикса».
"""
import sys
from pathlib import Path

from dtc_report import analyze_csv_file

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

paths = sys.argv[1:] or [str(p) for p in Path("logs").glob("monitor_*.csv")]

print(f"{'файл':<48} {'строк':>8} {'UDS':>5} {'started':>8} {'OK':>5} {'gap':>4} {'aban':>4} {'corrupt%':>9}")
print("-" * 100)

for p in paths:
    pp = Path(p)
    if not pp.exists():
        print(f"  (нет файла: {p})")
        continue
    try:
        an, total_rows, total_events = analyze_csv_file(pp)
    except Exception as e:
        print(f"{pp.name:<48} ERROR: {e}")
        continue
    s = an.reassembler.stats
    started = s["started"]
    ok = s["completed_ok"]
    gap = s["corrupted_gap"]
    aban = s["abandoned"]
    corrupt = gap + aban
    pct = (100.0 * corrupt / started) if started else 0.0
    print(f"{pp.name:<48} {total_rows:>8} {total_events:>5} {started:>8} {ok:>5} {gap:>4} {aban:>4} {pct:>8.1f}%")
