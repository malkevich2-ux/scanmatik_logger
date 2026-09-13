#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless-тест новой вкладки "Монитор шины" (MonitorTab) — БЕЗ реального
устройства: подаём синтетические кадры напрямую через
monitor._on_raw_frame(...), как это делал бы колбэк LiveTab (реальное
подключение к J2534 отдельно уже проверено раньше — здесь тестируется
только логика самой вкладки: разбор адресов J1939 SA / OBD-II ID, таблица
блоков, запись в CSV, копирование в буфер, автостоп записи при разрыве
соединения).
"""
import csv
import sys
import tempfile
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

root = tk.Tk()
root.withdraw()

live = gui.LiveTab(root)
monitor = gui.MonitorTab(root, live)
root.update()

print("--- 1. Кнопка записи заблокирована, пока не 'подключено' ---")
print("record_btn state (не подключено):", str(monitor.record_btn["state"]))

# Симулируем "подключение" без реального устройства — тестируем только
# логику MonitorTab, а не сам J2534-адаптер (это отдельно уже проверялось
# на реальном ЭБУ раньше).
live.connected = True


def j1939_id(sa: int, pf: int = 0xF0, ps: int = 0x04, prio: int = 6, dp: int = 0) -> str:
    v = (prio << 26) | (dp << 24) | (pf << 16) | (ps << 8) | sa
    return f"{v:08X}"


print("\n--- 2. Подача синтетических кадров (J1939 + обычный CAN) ---")
pc_ms = str(int(time.time() * 1000))
dev_ts = "1000"  # dev_ts_us — таймstamp адаптера, для теста не важен, просто передаём как есть
frames = [
    (pc_ms, dev_ts, j1939_id(0x00), "1", "01 02 03 04 05 06 07", "0"),  # Двигатель №1
    (pc_ms, dev_ts, j1939_id(0x0B), "1", "10 20 30", "0"),                # Тормозная система
    (pc_ms, dev_ts, j1939_id(0x0B), "1", "11 21 31", "0"),                # тот же блок ещё раз — счётчик
    (pc_ms, dev_ts, j1939_id(0x17), "1", "AA BB", "0"),                    # Приборная панель
    (pc_ms, dev_ts, "7E8", "0", "03 41 00 00", "0"),                       # обычный CAN/OBD, ответ ЭБУ №1
]
for f in frames:
    monitor._on_raw_frame(*f)

monitor._poll()
root.update()

print("Кадров всего:", monitor.frame_count, "(ожидалось 5)")
print("Уникальных адресов:", len(monitor._addresses), "(ожидалось 4)")
print("SA 0x0B встречен раз:", monitor._addresses.get("SA 0x0B", {}).get("count"), "(ожидалось 2)")
print("Название SA 0x00:", monitor._addresses.get("SA 0x00", {}).get("name"))
print("Название SA 0x0B:", monitor._addresses.get("SA 0x0B", {}).get("name"))
print("Название SA 0x17:", monitor._addresses.get("SA 0x17", {}).get("name"))
print("Название ID 0x7E8:", monitor._addresses.get("ID 0x7E8", {}).get("name"))
print("Строк в таблице адресов:", len(monitor.addr_tree.get_children()), "(ожидалось 4)")

print("\n--- 3. Журнал кадров (показ включён по умолчанию) ---")
log_content = monitor.log_text.get("1.0", "end")
print("В журнале есть строка про SA 0x0B:", "SA 0x0B" in log_content)

print("\n--- 4. Копирование таблицы адресов в буфер ---")
monitor._copy_addr_table()
clip = root.clipboard_get()
print("В буфере есть 'Тормозная система':", "Тормозная система" in clip)
print("В буфере есть заголовки колонок:", "Адрес" in clip and "Блок (расшифровка)" in clip)

print("\n--- 5. Запись в CSV ---")
tmp_dir = Path(tempfile.mkdtemp(prefix="monitor_test_"))
csv_path = tmp_dir / "test_monitor.csv"
monitor.log_path_var.set(str(csv_path))
monitor._start_recording()
print("recording:", monitor.recording)
print("record_btn текст:", monitor.record_btn["text"])

more_frames = [
    (pc_ms, dev_ts, j1939_id(0x00), "1", "AA", "0"),
    (pc_ms, dev_ts, j1939_id(0x23), "1", "BB", "0"),  # адрес нашего ЭБУ в проекте (DA=0x23)
]
for f in more_frames:
    monitor._on_raw_frame(*f)
monitor._poll()
root.update()

monitor._stop_recording()
print("recording после остановки:", monitor.recording)
print("recorded_count:", monitor.recorded_count, "(ожидалось 2)")

with open(csv_path, encoding="utf-8") as fh:
    rows = list(csv.reader(fh))
print("Строк в CSV (с заголовком):", len(rows), "(ожидалось 3)")
print("Заголовок CSV:", rows[0])
print("Пример строки:", rows[-1])
found_hitch = any(len(r) > 9 and "Управление сцепным устройством" in r[9] for r in rows[1:])
print("В CSV есть расшифровка блока с адресом нашего ЭБУ 0x23:", found_hitch)

print("\n--- 6. Разрыв соединения автоматически останавливает запись ---")
monitor.log_path_var.set(str(tmp_dir / "test_monitor2.csv"))
monitor._start_recording()
print("recording до отключения:", monitor.recording)
live.connected = False
monitor._poll()
root.update()
print("recording после 'отключения' на вкладке Живое подключение:", monitor.recording, "(ожидалось False)")
print("record_btn заблокирована:", str(monitor.record_btn["state"]))

print("\n--- 7. Выделение в таблице адресов переживает обновление (регресс старого бага) ---")
# Раньше _refresh_addr_tree пересоздавала строки с авто-сгенерированными id
# при каждом кадре, и восстановление выделения было сломано (сравнивало
# "SA 0x0B" с "I001"). Теперь iid строки — это сам ключ адреса, поэтому
# выделение должно сохраняться само по себе.
live.connected = True
monitor.addr_tree.selection_set("SA 0x0B")
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x17), "1", "CC", "0")  # другой уже известный адрес
monitor._poll()
root.update()
print("Выделение после нового кадра:", monitor.addr_tree.selection(), "(ожидалось ('SA 0x0B',))")

print("\n--- 8. Фильтр по блокам: просмотр и запись переключаются раздельно ---")
monitor.addr_tree.selection_set("SA 0x0B")
monitor._clear_log()
monitor.filter_view_var.set(True)
monitor.filter_record_var.set(False)
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x0B), "1", "DD", "0")
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x17), "1", "EE", "0")
monitor._poll()
root.update()
log_after_filter = monitor.log_text.get("1.0", "end")
print("Журнал содержит SA 0x0B (выделен):", "SA 0x0B" in log_after_filter, "(ожидалось True)")
print("Журнал НЕ содержит SA 0x17 (отфильтрован):", "SA 0x17" not in log_after_filter, "(ожидалось True)")

csv_path2 = tmp_dir / "test_monitor_filter_off.csv"
monitor.log_path_var.set(str(csv_path2))
monitor._start_recording()
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x0B), "1", "FF", "0")
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x17), "1", "11", "0")
monitor._poll()
root.update()
monitor._stop_recording()
with open(csv_path2, encoding="utf-8") as fh:
    rows_off = list(csv.reader(fh))
print(
    "Фильтр записи ВЫКЛЮЧЕН -> в CSV попали оба адреса:",
    len(rows_off) - 1 == 2, "(ожидалось True, т.к. filter_record_var=False)",
)

monitor.filter_record_var.set(True)
csv_path3 = tmp_dir / "test_monitor_filter_on.csv"
monitor.log_path_var.set(str(csv_path3))
monitor._start_recording()
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x0B), "1", "22", "0")
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x17), "1", "33", "0")
monitor._poll()
root.update()
monitor._stop_recording()
with open(csv_path3, encoding="utf-8") as fh:
    rows_on = list(csv.reader(fh))
only_0b = len(rows_on) - 1 == 1 and rows_on[1][8] == "SA 0x0B"
print(
    "Фильтр записи ВКЛЮЧЁН -> в CSV попал только выделенный SA 0x0B:",
    only_0b, "(ожидалось True)",
)

print("\n--- 9. Пустое выделение = фильтр не действует ---")
monitor.addr_tree.selection_remove(*monitor.addr_tree.selection())
csv_path4 = tmp_dir / "test_monitor_filter_empty.csv"
monitor.log_path_var.set(str(csv_path4))
monitor._start_recording()  # filter_record_var всё ещё True, но выделения нет
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x0B), "1", "44", "0")
monitor._on_raw_frame(pc_ms, dev_ts, j1939_id(0x17), "1", "55", "0")
monitor._poll()
root.update()
monitor._stop_recording()
with open(csv_path4, encoding="utf-8") as fh:
    rows_empty = list(csv.reader(fh))
print(
    "Пустое выделение -> фильтр не режет ничего, оба адреса в CSV:",
    len(rows_empty) - 1 == 2, "(ожидалось True)",
)

print("\n--- 10. Обновление списка J2534-устройств (LiveTab._refresh_devices) ---")
live.connected = False
devices_before = list(live.devices)
live._refresh_devices()
print("Список устройств после обновления не пуст или корректно пуст:", isinstance(live.devices, list))
print("Комбобокс синхронизирован со списком устройств:", list(live.device_combo["values"]) == [d[0] for d in live.devices] or (not live.devices and list(live.device_combo["values"]) == ["(не найдено)"]))

print("\nDONE")
root.destroy()
