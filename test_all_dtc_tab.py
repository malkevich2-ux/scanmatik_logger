#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless-тест новой вкладки "Ошибки всех блоков" (AllDtcTab) — БЕЗ реального
устройства: обнаружение адресов подаём через _on_raw_frame (как это делает
LiveTab на реальной шине), а сам обмен по UDS подменяем фейковой
live._send_and_wait, чтобы не трогать реальный bridge/адаптер.

Окно управляется НАСТОЯЩИМ root.mainloop() + root.after() (как в
test_request_tab.py), а НЕ ручным update() в цикле — вкладка запускает
фоновый поток (_start_scan), который трогает Tk-переменные (ecu_da_var), а
это даёт ложный "main thread is not in main loop" при ручном update()
(проверено на этой же машине: Python 3.14 тут строже к этому, чем раньше).
"""

import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
import can_dtc_reader_gui as gui
from uds_decoder import UdsEvent

root = tk.Tk()
root.withdraw()

live = gui.LiveTab(root)
tab = gui.AllDtcTab(root, live)

FAILED = []


def fail(msg):
    print("FAIL:", msg)
    FAILED.append(msg)
    root.destroy()


def check(name, condition, detail=""):
    if condition:
        print(f"[OK] {name}")
    else:
        fail(f"{name} {detail}")


def j1939_id(sa: int, pf: int = 0xF0, ps: int = 0x04, prio: int = 6, dp: int = 0) -> str:
    v = (prio << 26) | (dp << 24) | (pf << 16) | (ps << 8) | sa
    return f"{v:08X}"


def wait_until_idle(on_done, _deadline=None):
    """Ждёт, пока фоновая операция вкладки (_busy) завершится, через
    root.after — как _wait_send_done в test_request_tab.py, а не через
    ручной update() в цикле (см. docstring файла)."""
    if _deadline is None:
        _deadline = time.monotonic() + 10
    if not tab._busy:
        root.after(50, on_done)
        return
    if time.monotonic() > _deadline:
        fail("операция не завершилась за отведённое время (похоже, зависла)")
        return
    root.after(30, lambda: wait_until_idle(on_done, _deadline))


def step1_check_disabled():
    print("--- 1. Кнопки опроса заблокированы, пока не 'подключено' ---")
    check("scan_all_btn выключена без подключения", str(tab.scan_all_btn["state"]) == "disabled")
    live.connected = True
    root.after(50, step2_discover)


def step2_discover():
    print("\n--- 2. Обнаружение блоков по сырым кадрам шины ---")
    pc_ms = str(int(time.time() * 1000))
    dev_ts = "1000"
    for sa in (0x0B, 0x0B, 0x17, 0x40):  # 0x0B дважды — проверка счётчика
        tab._on_raw_frame(pc_ms, dev_ts, j1939_id(sa), "1", "01 02", "0")
    tab._poll()

    check("обнаружены нужные адреса", set(tab._blocks.keys()) == {0x0B, 0x17, 0x40},
          f"получено {sorted(tab._blocks.keys())}")
    check("SA 0x0B встречен 2 раза", tab._blocks.get(0x0B, {}).get("seen") == 2)
    check("источник 0x0B — 'шина'", tab._blocks.get(0x0B, {}).get("source") == "шина")
    check("статус до опроса — заглушка", tab._blocks.get(0x0B, {}).get("status") == "— не опрошено —")
    check("строк в таблице — 3", len(tab.tree.get_children()) == 3, f"{len(tab.tree.get_children())}")

    root.after(50, step3_manual_add)


def step3_manual_add():
    print("\n--- 3. Ручное добавление адреса ---")
    tab.manual_addr_var.set("41")
    tab._on_add_manual()
    check("0x41 добавлен вручную", 0x41 in tab._blocks)
    check("источник 0x41 — 'вручную'", tab._blocks.get(0x41, {}).get("source") == "вручную")
    check("seen у ручного адреса — 0", tab._blocks.get(0x41, {}).get("seen") == 0)
    check("строк в таблице — 4", len(tab.tree.get_children()) == 4, f"{len(tab.tree.get_children())}")
    root.after(50, step4_start_scan)


FAKE_DTC_BY_ADDR = {
    0x0B: [("500001", 236, "C1000", ["confirmedDTC"])],
    0x17: [],  # ошибок нет
    0x40: None,  # None => имитируем таймаут (нет ответа)
    0x41: "negative",  # имитируем отрицательный ответ
}


def fake_send_and_wait(payload: bytes, label: str):
    da = int(live.ecu_da_var.get(), 16)
    entry = FAKE_DTC_BY_ADDR.get(da, [])
    if entry is None:
        return None
    if entry == "negative":
        return UdsEvent(
            ts=0, can_id="18DAF223", sa=da, da=0xF2, kind="negative",
            service=0x19, service_name="ReadDTCInformation",
            summary="отрицательный ответ", raw_hex="7F1931", nrc=0x31,
        )
    return UdsEvent(
        ts=0, can_id="18DAF223", sa=da, da=0xF2, kind="positive",
        service=0x19, service_name="ReadDTCInformation",
        summary="ok", raw_hex="590AFF", dtcs=entry,
    )


ORIG_DA = None


def step4_start_scan():
    global ORIG_DA
    print("\n--- 4. Опрос всех обнаруженных блоков (фейковый обмен, без реального ЭБУ) ---")
    live._send_and_wait = fake_send_and_wait
    ORIG_DA = live.ecu_da_var.get()
    print("Адрес ЭБУ на 'Живом подключении' до опроса:", ORIG_DA, "(ожидалось 23)")
    tab._on_scan_click(only_selected=False)
    wait_until_idle(step5_check_scan_result)


def step5_check_scan_result():
    print("Опрос завершён (не завис): True")
    check("адрес ЭБУ восстановлен после опроса", live.ecu_da_var.get() == ORIG_DA,
          f"{live.ecu_da_var.get()} != {ORIG_DA}")
    check("результат 0x0B содержит найденный DTC", "C1000" in tab._blocks[0x0B]["status"] and "1 DTC" in tab._blocks[0x0B]["status"],
          tab._blocks[0x0B]["status"])
    check("результат 0x17 — «ошибок нет»", tab._blocks[0x17]["status"] == "ошибок нет", tab._blocks[0x17]["status"])
    check("результат 0x40 — таймаут", "таймаут" in tab._blocks[0x40]["status"], tab._blocks[0x40]["status"])
    check("результат 0x41 — отрицательный ответ", "отрицательный ответ" in tab._blocks[0x41]["status"], tab._blocks[0x41]["status"])
    log_content = tab.log_text.get("1.0", "end")
    check("в журнале есть код C1000", "C1000" in log_content)
    root.after(50, step6_abort)


def step6_abort():
    print("\n--- 5. Остановка опроса на середине ---")
    for extra_sa in (0x50, 0x60, 0x70):
        tab._blocks[extra_sa] = {
            "name": "тест", "seen": 0, "source": "вручную",
            "status": "— не опрошено —", "dtcs": [], "last_poll": "",
        }
    tab._refresh_tree()
    tab._on_scan_click(only_selected=False)
    tab._on_abort_click()
    wait_until_idle(step7_check_abort)


def step7_check_abort():
    print("После остановки не завис: True")
    check("адрес ЭБУ восстановлен и после прерванного опроса", live.ecu_da_var.get() == ORIG_DA)
    log_after_abort = tab.log_text.get("1.0", "end")
    check("в журнале есть отметка об остановке", "Остановлено пользователем" in log_after_abort)
    root.after(50, step7b_clear_dtc)


def step7b_clear_dtc():
    print("\n--- 5b. Очистка DTC у выделенных блоков (успех и отрицательный ответ) ---")
    check("clear_selected_btn включена (есть подключение и не занято)", str(tab.clear_selected_btn["state"]) == "normal")
    for iid in tab.tree.selection():
        tab.tree.selection_remove(iid)
    tab.tree.selection_set("0B", "41")  # 0x0B — успех, 0x41 — отрицательный ответ (см. FAKE_DTC_BY_ADDR)
    # _on_clear_click показывает реальный messagebox.askyesno с подтверждением
    # (это НЕОБРАТИМАЯ операция) — в headless-тесте подменяем на "Да", иначе
    # это блокирующее модальное окно повиснет и весь mainloop встанет колом
    # (root скрыт через withdraw(), поэтому диалог не виден, но всё равно
    # блокирует событийный цикл, ожидая клика, которого не будет).
    orig_askyesno = gui.messagebox.askyesno
    gui.messagebox.askyesno = lambda *a, **k: True
    try:
        tab._on_clear_click(only_selected=True)
    finally:
        gui.messagebox.askyesno = orig_askyesno
    wait_until_idle(step7c_check_clear)


def step7c_check_clear():
    print("Очистка завершена (не зависла): True")
    check("адрес ЭБУ восстановлен после очистки", live.ecu_da_var.get() == ORIG_DA)
    check("0x0B: DTC очищены", tab._blocks[0x0B]["status"] == "DTC очищены", tab._blocks[0x0B]["status"])
    check("0x41: очистка не выполнена (отрицательный ответ)", "не выполнена" in tab._blocks[0x41]["status"],
          tab._blocks[0x41]["status"])
    log_content = tab.log_text.get("1.0", "end")
    check("в журнале есть 'Очистка DTC'", "Очистка DTC" in log_content)
    check("в журнале есть 'DTC успешно очищены'", "DTC успешно очищены" in log_content)
    root.after(50, step8_forget_all)


def step8_forget_all():
    print("\n--- 6. «Забыть все адреса» (подтверждение подменено, чтобы не блокировать тест) ---")
    orig_askyesno = gui.messagebox.askyesno
    gui.messagebox.askyesno = lambda *a, **k: True
    try:
        tab._on_forget_all()
    finally:
        gui.messagebox.askyesno = orig_askyesno
    check("таблица очищена (_blocks)", len(tab._blocks) == 0)
    check("таблица очищена (tree)", len(tab.tree.get_children()) == 0)
    root.after(50, step9_on_close)


def step9_on_close():
    print("\n--- 7. on_close отписывает слушатель кадров ---")
    listeners_before = len(live._frame_listeners)
    tab.on_close()
    check("слушатель кадров отписан", len(live._frame_listeners) == listeners_before - 1,
          f"{listeners_before} -> {len(live._frame_listeners)}")
    print("\nDONE" if not FAILED else "\nFAILED")
    root.destroy()


root.after(50, step1_check_disabled)
root.mainloop()

if FAILED:
    sys.exit(1)
