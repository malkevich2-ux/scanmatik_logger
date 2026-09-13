#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless-тест новой очереди запросов и копирования в буфер — через
настоящие методы виджетов, root.mainloop()/after(), как и предыдущие
test_request_tab.py / test_multiframe.py.

Если устройство занято другим процессом (например, у пользователя открыто
окно программы) — часть с реальной отправкой аккуратно пропускается,
но UI/очередь/буфер обмена проверяются в любом случае (эти операции
устройства не требуют).
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

print("--- 1. Проверка копирования в буфер (без устройства) ---")
req._log("тестовая строка журнала 1")
req._log("тестовая строка журнала 2 — VIN пример YV2RG10A4HA810812")
root.update()
req._copy_log()
clip = root.clipboard_get()
print("Буфер после _copy_log() содержит обе строки:", "строка журнала 1" in clip and "строка журнала 2" in clip)

live.tables.add_ident(0x23, 0xF190, "VIN", "YV2RG10A4HA810812")
live.tables.add_ident(0x23, 0xF191, "HW number", "1234567890")
root.update()
gui.EcuTables._copy_tree_all(live.tables.ident_tree)
clip2 = root.clipboard_get()
print("Буфер после копирования таблицы идентов содержит VIN:", "YV2RG10A4HA810812" in clip2)
print("Буфер содержит заголовки колонок:", "Параметр" in clip2 and "Значение" in clip2)

print("\n--- 2. Проверка очереди (без устройства) ---")
for i, d in enumerate(gui.SERVICE_DEFS):
    if d["key"] == "3E":
        req.service_combo.current(i)
        break
req._on_service_change()
root.update()
req._on_add_to_queue()

for i, d in enumerate(gui.SERVICE_DEFS):
    if d["key"] == "22":
        req.service_combo.current(i)
        break
req._on_service_change()
req._param_vars["did"].set("F190")
root.update()
req._on_add_to_queue()

for i, d in enumerate(gui.SERVICE_DEFS):
    if d["key"] == "19":
        req.service_combo.current(i)
        break
req._on_service_change()
req._param_vars["subfn"].set("02")
root.update()
req._on_add_to_queue()

print("Размер очереди после 3 добавлений:", len(req.request_queue))
print("Очередь (описания):", [it["desc"] for it in req.request_queue])

# проверка перемещения и удаления
req.queue_tree.selection_set(req.queue_tree.get_children()[0])
req._move_queue_item(1)
print("После перемещения первого элемента вниз:", [it["desc"] for it in req.request_queue])

req.queue_tree.selection_set(req.queue_tree.get_children()[-1])
req._on_remove_from_queue()
print("После удаления последнего элемента, размер очереди:", len(req.request_queue))


def step_connect():
    found = False
    for i, (label, dll) in enumerate(live.devices):
        if dll.lower().endswith("smj2534.dll"):
            live.device_combo.current(i)
            found = True
            print(f"\nУстройство: {label} -> {dll}")
            break
    if not found:
        print("SM2 dll не найден в реестре — пропускаю проверку реальной отправки очереди.")
        root.after(200, finish)
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
        print("Не удалось подключиться (возможно, устройство занято другим процессом) — "
              "пропускаю проверку реальной отправки очереди. Остальное уже проверено выше.")
        root.after(200, finish)
        return
    root.after(300, step_send_queue)


def step_send_queue():
    print(f"\n--- 3. Отправка очереди из {len(req.request_queue)} запрос(ов) на реальном ЭБУ ---")
    req._on_send_queue_click()
    _wait_queue_done()


def _wait_queue_done(_deadline=None):
    if _deadline is None:
        _deadline = time.monotonic() + 30
    if str(req.send_queue_btn["state"]) != "disabled" or time.monotonic() > _deadline:
        root.after(300, step_done)
        return
    root.after(150, lambda: _wait_queue_done(_deadline))


def step_done():
    print("\n--- ЖУРНАЛ ВКЛАДКИ ---")
    print(req.log_text.get("1.0", "end"))
    live._disconnect()
    root.after(300, finish)


def finish():
    print("\nDONE")
    root.destroy()


root.after(200, step_connect)
root.mainloop()
