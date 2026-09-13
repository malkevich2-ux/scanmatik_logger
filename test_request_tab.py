#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless-тест реальной вкладки «Свои UDS-запросы» (RequestTab) — без ручных
кликов мышью, но через ТЕ ЖЕ методы, что дёргают виджеты (Combobox.current,
_on_service_change, _param_vars, _on_send_click). Окно скрыто
(root.withdraw()), но управляется НАСТОЯЩИМ root.mainloop() + root.after(),
как и реальное приложение (а не ручным update() в цикле — это не то же
самое для многопоточности Tkinter и даёт ложные "main thread is not in
main loop"). Проверяем на реальном ЭБУ: несколько запросов подряд через
конструктор.
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

STEPS_DONE = {"connect": False}


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
    root.after(300, lambda: send_service("22", {"did": "F190"}, step2))


def send_service(key, params, on_done):
    for i, d in enumerate(gui.SERVICE_DEFS):
        if d["key"] == key:
            req.service_combo.current(i)
            break
    req._on_service_change()
    for k, v in params.items():
        req._param_vars[k].set(v)
    print(f"preview [{key}]:", req.preview_var.get())
    req._on_send_click()
    _wait_send_done(on_done)


def _wait_send_done(on_done, _deadline=None):
    if _deadline is None:
        _deadline = time.monotonic() + 20
    if str(req.send_btn["state"]) == "normal" or time.monotonic() > _deadline:
        root.after(300, on_done)
        return
    root.after(150, lambda: _wait_send_done(on_done, _deadline))


def step2():
    send_service("3E", {}, step3)


def step3():
    send_service("19", {"subfn": "0A"}, step4)


def step4():
    print("\n--- ЖУРНАЛ ВКЛАДКИ ---")
    print(req.log_text.get("1.0", "end"))
    live._disconnect()
    root.after(300, finish)


def finish():
    print("DONE")
    root.destroy()


root.after(200, step_connect)
root.mainloop()
