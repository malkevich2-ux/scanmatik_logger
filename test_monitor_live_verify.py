#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Короткая проверка на РЕАЛЬНОМ адаптере: подключаемся, 8 секунд пишем
монитор-лог, затем смотрим — стали ли таймstamp'ы (pc_time_ms и dev_ts_us)
у близких кадров различаться (после исправления NowMs() на Stopwatch),
вместо того чтобы задваиваться, как было видно в monitor_20260904_182544.csv."""
import csv
import sys
import time
from collections import Counter
from pathlib import Path

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
import can_dtc_reader_gui as gui

root = tk.Tk()
root.withdraw()

live = gui.LiveTab(root)
monitor = gui.MonitorTab(root, live)
root.update()

out_path = Path(__file__).resolve().parent / "logs" / "verify_stopwatch_fix.csv"
monitor.log_path_var.set(str(out_path))


def step_connect():
    found = False
    for i, (label, dll) in enumerate(live.devices):
        if dll.lower().endswith("smj2534.dll"):
            live.device_combo.current(i)
            found = True
            print(f"Устройство: {label} -> {dll}")
            break
    if not found:
        print("SM2 dll не найден — пропускаю проверку на реальном адаптере.")
        root.after(200, finish)
        return
    live.mode_var.set("J1939")
    live._on_mode_change()
    live.baud_var.set("500000")
    live._connect()
    root.after(2000, check_connected)


def check_connected():
    print("connected:", live.connected)
    if not live.connected:
        print("Не удалось подключиться — пропускаю проверку.")
        root.after(200, finish)
        return
    monitor._start_recording()
    print("recording:", monitor.recording, "-> файл:", out_path.name)
    root.after(8000, stop_and_check)


def stop_and_check():
    monitor._stop_recording()
    live._disconnect()
    print(f"\nЗаписано кадров: {monitor.recorded_count}")
    analyze()
    root.after(200, finish)


def analyze():
    with open(out_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"Строк в CSV: {len(rows)}")
    if len(rows) < 5:
        print("Слишком мало кадров для содержательной проверки (шина тихая?).")
        return

    pc_ms_counts = Counter(r["pc_time_ms"] for r in rows)
    dev_ts_counts = Counter(r["dev_ts_us"] for r in rows)
    max_pc_dupes = max(pc_ms_counts.values())
    max_dev_dupes = max(dev_ts_counts.values())
    print(f"Максимум кадров с ОДИНАКОВЫМ pc_time_ms: {max_pc_dupes} (раньше, до фикса, доходило до 5+ подряд)")
    print(f"Максимум кадров с ОДИНАКОВЫМ dev_ts_us: {max_dev_dupes}")

    unique_pc_ms = len(pc_ms_counts)
    print(f"Уникальных значений pc_time_ms: {unique_pc_ms} из {len(rows)} строк "
          f"({100*unique_pc_ms/len(rows):.0f}% уникальны)")

    # dev_ts_us должен быть монотонно неубывающим (это счётчик адаптера)
    dev_vals = [int(r["dev_ts_us"]) for r in rows]
    non_monotonic = sum(1 for a, b in zip(dev_vals, dev_vals[1:]) if b < a)
    print(f"Случаев, когда dev_ts_us пошёл НАЗAД (переполнение счётчика адаптера — нормально раз в ~71 мин): {non_monotonic}")


def finish():
    print("\nDONE")
    root.destroy()


root.after(200, step_connect)
root.mainloop()
