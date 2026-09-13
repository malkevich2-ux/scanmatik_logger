#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка фикса пропускной способности моста НА РЕАЛЬНОМ адаптере:
подключаемся, несколько раз подряд читаем иденты (это как раз генерирует
многокадровые ISO-TP обмены — VIN, номера ПО/железа), одновременно пишем
монитор-лог, затем считаем через dtc_report.analyze_csv_file, сколько
многокадровых обменов повреждено. Сравниваем с тем, что было ДО фикса
(monitor_20260904_182544.csv: 26/43 = 60%, monitor_20260904_191752.csv:
20/44 = 45%)."""
import sys
import time
from pathlib import Path

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
import can_dtc_reader_gui as gui
from dtc_report import analyze_csv_file

root = tk.Tk()
root.withdraw()

live = gui.LiveTab(root)
monitor = gui.MonitorTab(root, live)
root.update()

out_path = Path(__file__).resolve().parent / "logs" / "verify_throughput_fix.csv"
monitor.log_path_var.set(str(out_path))

READ_ROUNDS = 6  # несколько раз подряд прочитать иденты — генерируем много многокадровых обменов


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
    globals()["_round"] = 0
    root.after(500, do_round)


def do_round():
    # ВАЖНО: раньше здесь был фиксированный интервал 4с между раундами, но
    # один раунд чтения идентов может занимать НАМНОГО дольше: 8 DID'ов *
    # (до SEND_ATTEMPTS=4 повторов * RESPONSE_TIMEOUT_S=1.5с) = до ~48с в
    # худшем случае, если ЭБУ не отвечает. Запуск следующего раунда по
    # таймеру поверх ещё выполняющегося приводил к тому, что _run_op видел
    # self._op_lock.locked() и показывал МОДАЛЬНОЕ окно messagebox.showinfo
    # ("Занято", ...) — а поскольку тест безголовый (никто не может нажать
    # OK), mainloop зависал навсегда. Поэтому теперь ждём освобождения
    # _op_lock перед каждым следующим раундом вместо таймера.
    n = globals()["_round"]
    if n >= READ_ROUNDS:
        root.after(1000, stop_and_check)
        return
    if live._op_lock.locked():
        root.after(300, do_round)
        return
    print(f"--- Раунд чтения идентов {n + 1}/{READ_ROUNDS} ---")
    live._on_read_idents_click()
    globals()["_round"] = n + 1
    # даём потоку время захватить _op_lock, прежде чем начнём ждать его освобождения
    root.after(300, wait_round_done)


def wait_round_done():
    if live._op_lock.locked():
        root.after(300, wait_round_done)
    else:
        root.after(200, do_round)


def watchdog():
    # Аварийный останов на случай, если раунды идут дольше отведённого
    # времени (например, ЭБУ шлёт много responsePending подряд). Раньше тут
    # был прямой вызов finish() — а он НЕ закрывает CSV-файл (закрытие только
    # в _stop_recording(), это и есть единственное место, где буфер реально
    # сбрасывается на диск помимо каждой 200-й строки). В результате первый
    # прогон с сорванным watchdog'ом дал ПУСТОЙ файл (0 байт, даже заголовок
    # не попал на диск) — данные потерялись впустую. Теперь идём через
    # stop_and_check(), как при штатном завершении, чтобы то, что успело
    # записаться, было проанализировано.
    print("\nWATCHDOG: превышено максимальное время ожидания — останавливаю запись и анализирую, что успело записаться.")
    root.after(200, stop_and_check)


def stop_and_check():
    monitor._stop_recording()
    live._disconnect()
    print(f"\nЗаписано кадров: {monitor.recorded_count}")
    analyzer, rows, events = analyze_csv_file(out_path)
    stats = analyzer.reassembler.stats
    corrupted = stats["corrupted_gap"] + stats["abandoned"]
    print(f"Строк: {rows}, UDS-событий: {events}")
    print(f"Многокадровых обменов: {stats['started']}, "
          f"собралось целиком: {stats['completed_ok']}, "
          f"повреждено: {corrupted}"
          + (f" ({100*corrupted/stats['started']:.0f}%)" if stats["started"] else " (обменов не было)"))
    print("\nДЛЯ СРАВНЕНИЯ (до фикса): 182544 -> 26/43 (60%), 191752 -> 20/44 (45%)")
    root.after(200, finish)


def finish():
    print("\nDONE")
    root.destroy()


root.after(200, step_connect)
root.after(400_000, watchdog)  # ~6.7 мин — заведомо больше худшего случая (6 раундов * ~48с)
root.mainloop()
