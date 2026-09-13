#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless-тест МНОГОКАДРОВОЙ отправки через вкладку «Свои UDS-запросы» на
реальном ЭБУ — через настоящие методы виджетов (service_combo, _param_vars,
_on_send_click), с настоящим root.mainloop()/after(), как и предыдущий
test_request_tab.py.

Тест безопасный (read-only): просим ReadDataByIdentifier сразу НЕСКОЛЬКО
DID одним запросом (SID 0x22 + 5 DID по 2 байта = 11 байт payload) — это
больше 7 байт, поэтому НАШ код обязан отправить его как First Frame +
Consecutive Frame и дождаться Flow Control от ЭБУ между ними. Сам запрос
ничего не меняет на ЭБУ (может вернуть и отрицательный ответ, если ЭБУ не
поддерживает мульти-DID чтение — это тоже валидный результат, подтверждающий,
что запрос дошёл и был разобран).
"""

import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
import can_dtc_reader_gui as gui

root = tk.Tk()
root.withdraw()

live = gui.LiveTab(root)
req = gui.RequestTab(root, live)


def fail(msg):
    print("FAIL:", msg)
    try:
        live._disconnect()
    except Exception:
        pass
    root.destroy()
    sys.exit(1)


def step_connect():
    found = False
    for i, (label, dll) in enumerate(live.devices):
        if dll.lower().endswith("smj2534.dll"):
            live.device_combo.current(i)
            found = True
            print(f"Устройство: {label} -> {dll}")
            break
    if not found:
        fail(f"SM2 dll не найден в реестре: {live.devices}")
        return
    live.mode_var.set("J1939")
    live._on_mode_change()
    live.baud_var.set("500000")
    live.ecu_da_var.set("23")
    live.tester_sa_var.set("F2")
    live._connect()
    root.after(2000, check_connected)


def check_connected():
    print("connected:", live.connected)
    if not live.connected:
        fail("не удалось подключиться")
        return
    root.after(300, step_multiframe)


def step_multiframe():
    # Свой запрос: 0x22 + 5 DID подряд (VIN, HW ECU number, System Supplier
    # HW number, Spare Part Number, ECU Software Number) = 11 байт -> точно
    # многокадровый (First Frame 6 байт + 1 Consecutive Frame 5 байт).
    raw_hex = "22 F1 90 F1 91 F1 92 F1 87 F1 88"
    for i, d in enumerate(gui.SERVICE_DEFS):
        if d["key"] == "raw":
            req.service_combo.current(i)
            break
    req._on_service_change()
    req._param_vars["raw"].set(raw_hex)
    print("preview (multi-DID raw):", req.preview_var.get())
    req._on_send_click()
    _wait_send_done(step_done)


def _wait_send_done(on_done, _deadline=None):
    if _deadline is None:
        _deadline = time.monotonic() + 30
    if str(req.send_btn["state"]) == "normal" or time.monotonic() > _deadline:
        root.after(300, on_done)
        return
    root.after(150, lambda: _wait_send_done(on_done, _deadline))


def step_done():
    print("\n--- ЖУРНАЛ ВКЛАДКИ ---")
    print(req.log_text.get("1.0", "end"))
    live._disconnect()
    root.after(300, finish)


def finish():
    print("DONE")
    root.destroy()


root.after(200, step_connect)
root.mainloop()
