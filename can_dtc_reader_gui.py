#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чтение идентов (номера ПО/железа, VIN и т.п.) и кодов ошибок (DTC) по
диагностическому обмену UDS (ISO 14229) поверх CAN/J1939 — для прибора
Scanmatik (SM2/SM3), подключённого по USB.

Три вкладки:
  1. "Живое подключение" — подключается к прибору через мост smbridge.exe
     и АКТИВНО опрашивает ЭБУ по UDS: сама отправляет запросы (чтение
     идентов, чтение и очистка DTC) и разбирает ответы на лету. Мост умеет
     и слушать шину, и отправлять кадры (PassThruWriteMsgs); адресация
     (CAN ID, точка-точка тестер<->ЭБУ) воспроизводит схему, реально
     наблюдённую в захваченном логе.
  2. "Свои UDS-запросы" — конструктор произвольного запроса поверх того же
     соединения, с очередью для отправки нескольких запросов подряд.
  3. "Монитор шины" — пассивный просмотр ВСЕГО трафика на шине (не только
     диагностического обмена), не открывает второе соединение — использует
     тот же канал, что и вкладка "Живое подключение" (мониторинг идёт
     всегда, пока то соединение открыто). Отдельная кнопка "Запись"
     включает/выключает запись в CSV поверх уже идущего мониторинга. Ниже
     — отдельная таблица с уникальными адресами блоков, встреченными на
     шине, и попыткой расшифровать, что это за блок (по стандартной
     таблице SAE J1939-71 для режима J1939, либо по диапазонам ID для
     классического CAN/OBD-II).

Офлайн-разбор ранее записанного CSV-лога по-прежнему доступен из
командной строки (без GUI): `py can_dtc_reader_gui.py --analyze лог.csv`.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from bridge_common import (
    APP_DIR,
    CAN_BAUD_OPTIONS,
    J1939_BAUD_OPTIONS,
    BridgeProcess,
    find_bridge_exe,
    find_j2534_devices,
    parse_frame_line,
)
from uds_decoder import (
    Analyzer,
    DID_NAMES,
    J1939_SA_NAMES,
    NRC_NAMES,
    OBD_STD_ID_NAMES,
    READ_DTC_SUBFUNCTIONS,
    RESET_TYPE_NAMES,
    ROUTINE_CONTROL_NAMES,
    SESSION_NAMES,
    decode_29bit_id,
)
from uds_tx import (
    DEFAULT_IDENT_DIDS,
    DTC_SUBFUNCTIONS_WITH_MASK,
    MAX_MULTI_FRAME_PAYLOAD,
    build_can_id,
    build_cf,
    build_ff,
    build_flow_control,
    build_sf,
    is_first_frame,
    parse_flow_control,
    req_clear_dtc,
    req_diag_session_control,
    req_ecu_reset,
    req_read_dtc,
    req_read_did,
    req_read_dtc_by_status_mask,
    req_routine_control,
    req_security_access,
    req_tester_present,
    req_write_did,
    st_min_to_seconds,
)
from dtc_reference import (
    COMPONENT_RU,
    FAILURE_TYPE_RU,
    describe_failure_type,
    is_dtc_active,
    search_components,
)


# --------------------------------------------------------------------- копирование в буфер обмена

def _copy_text_to_clipboard(widget: tk.Widget, text: str):
    """Кладёт текст в системный буфер обмена через сам виджет (у любого
    Tk-виджета есть доступ к общему буферу обмена приложения)."""
    widget.clipboard_clear()
    widget.clipboard_append(text)
    # Windows иногда не подхватывает содержимое буфера без явного owner_set
    # сразу после clipboard_append из фонового вызова — update() досылает.
    widget.update()


def _make_text_copyable(text_widget: tk.Text):
    """В Tkinter Text с state='disabled' нельзя выделить текст мышью и
    скопировать его (Ctrl+C не работает) — именно на это и жаловались.
    Вместо 'disabled' держим виджет в 'normal' (выделение и Ctrl+C работают
    как в любом текстовом поле), но блокируем реальный ввод текста —
    пропускаем только навигацию, выделение и copy/select-all."""
    nav_keysyms = {
        "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Tab",
    }

    def on_key(event):
        ctrl = bool(event.state & 0x4)
        if ctrl and event.keysym.lower() in ("c", "a", "insert"):
            return None
        if event.keysym in nav_keysyms:
            return None
        return "break"  # блокируем печать/удаление символов

    def select_all(_event=None):
        text_widget.tag_add("sel", "1.0", "end")
        return "break"

    def copy_selection_or_all(_event=None):
        try:
            selected = text_widget.get("sel.first", "sel.last")
        except tk.TclError:
            selected = text_widget.get("1.0", "end-1c")
        _copy_text_to_clipboard(text_widget, selected)
        return "break"

    text_widget.bind("<Key>", on_key)
    text_widget.bind("<Control-a>", select_all)
    text_widget.bind("<Control-A>", select_all)
    text_widget.bind("<Control-c>", copy_selection_or_all)
    text_widget.bind("<Control-C>", copy_selection_or_all)

    menu = tk.Menu(text_widget, tearoff=0)
    menu.add_command(label="Копировать выделенное", command=copy_selection_or_all)
    menu.add_command(
        label="Копировать всё",
        command=lambda: _copy_text_to_clipboard(text_widget, text_widget.get("1.0", "end-1c")),
    )

    def on_right_click(event):
        menu.tk_popup(event.x_root, event.y_root)

    text_widget.bind("<Button-3>", on_right_click)


def _bind_treeview_copy(tree: ttk.Treeview):
    """Treeview в Tkinter вообще не поддерживает выделение текста/Ctrl+C
    "из коробки" — добавляем копирование выбранных строк (или всех, если
    ничего не выделено) как текста с табуляцией между колонками, чтобы
    можно было вставить прямо в Excel."""

    def _rows_text(items):
        cols = tree["columns"]
        headers = [tree.heading(c)["text"] for c in cols]
        lines = ["\t".join(headers)]
        for item in items:
            vals = tree.item(item, "values")
            lines.append("\t".join(str(v) for v in vals))
        return "\n".join(lines)

    def copy_selected(_event=None):
        items = tree.selection() or tree.get_children()
        _copy_text_to_clipboard(tree, _rows_text(items))
        return "break"

    def copy_all(_event=None):
        _copy_text_to_clipboard(tree, _rows_text(tree.get_children()))
        return "break"

    tree.bind("<Control-c>", copy_selected)
    tree.bind("<Control-C>", copy_selected)

    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="Копировать выделенные строки", command=copy_selected)
    menu.add_command(label="Копировать всю таблицу", command=copy_all)

    def on_right_click(event):
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Button-3>", on_right_click)


class EcuTables(ttk.Frame):
    """Пара таблиц (Treeview) с идентами и DTC, сгруппированных по ECU —
    общий виджет для обеих вкладок."""

    def __init__(self, master):
        super().__init__(master)

        ident_frame = ttk.LabelFrame(self, text="Иденты (по ЭБУ)")
        ident_frame.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        ttk.Button(ident_frame, text="📋 Копировать таблицу", command=lambda: self._copy_tree_all(self.ident_tree)).pack(anchor="e", padx=2, pady=(2, 0))
        self.ident_tree = ttk.Treeview(
            ident_frame, columns=("ecu", "did", "name", "value"), show="headings", height=8
        )
        for col, text, w in (
            ("ecu", "ЭБУ (SA)", 80),
            ("did", "DID", 70),
            ("name", "Параметр", 260),
            ("value", "Значение", 320),
        ):
            self.ident_tree.heading(col, text=text)
            self.ident_tree.column(col, width=w, anchor="w")
        yscroll1 = ttk.Scrollbar(ident_frame, orient="vertical", command=self.ident_tree.yview)
        self.ident_tree.configure(yscrollcommand=yscroll1.set)
        self.ident_tree.pack(side="left", fill="both", expand=True)
        yscroll1.pack(side="right", fill="y")
        _bind_treeview_copy(self.ident_tree)

        dtc_frame = ttk.LabelFrame(self, text="Коды ошибок (DTC, по ЭБУ)")
        dtc_frame.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        ttk.Button(dtc_frame, text="📋 Копировать таблицу", command=lambda: self._copy_tree_all(self.dtc_tree)).pack(anchor="e", padx=2, pady=(2, 0))
        self.dtc_tree = ttk.Treeview(
            dtc_frame, columns=("ecu", "code", "raw", "status", "active", "ftype", "flags", "time"),
            show="headings", height=8,
        )
        for col, text, w in (
            ("ecu", "ЭБУ (SA)", 80),
            ("code", "Код", 70),
            ("raw", "Raw", 80),
            ("status", "Статус", 60),
            ("active", "Активна", 60),
            ("ftype", "Тип неисправности (предположительно)", 260),
            ("flags", "Флаги", 220),
            ("time", "Время", 120),
        ):
            self.dtc_tree.heading(col, text=text)
            self.dtc_tree.column(col, width=w, anchor="w")
        yscroll2 = ttk.Scrollbar(dtc_frame, orient="vertical", command=self.dtc_tree.yview)
        self.dtc_tree.configure(yscrollcommand=yscroll2.set)
        self.dtc_tree.pack(side="left", fill="both", expand=True)
        yscroll2.pack(side="right", fill="y")
        _bind_treeview_copy(self.dtc_tree)

        events_frame = ttk.LabelFrame(self, text="События (очистка DTC и т.п.)")
        events_frame.pack(fill="both", expand=False, padx=4, pady=(2, 4))
        ttk.Button(events_frame, text="📋 Копировать", command=lambda: _copy_text_to_clipboard(self.events_text, self.events_text.get("1.0", "end-1c"))).pack(anchor="e", padx=2, pady=(2, 0))
        self.events_text = tk.Text(events_frame, height=5, wrap="none", font=("Consolas", 9))
        self.events_text.pack(fill="both", expand=True)
        _make_text_copyable(self.events_text)

    @staticmethod
    def _copy_tree_all(tree: ttk.Treeview):
        cols = tree["columns"]
        headers = [tree.heading(c)["text"] for c in cols]
        lines = ["\t".join(headers)]
        for item in tree.get_children():
            vals = tree.item(item, "values")
            lines.append("\t".join(str(v) for v in vals))
        _copy_text_to_clipboard(tree, "\n".join(lines))

    def clear(self):
        self.ident_tree.delete(*self.ident_tree.get_children())
        self.dtc_tree.delete(*self.dtc_tree.get_children())
        self.events_text.delete("1.0", "end")

    def add_ident(self, sa: int, did: int, name: str, value: str):
        # Обновляем существующую строку (по ecu+did), либо добавляем новую.
        sa_txt = f"0x{sa:02X}"
        did_txt = f"0x{did:04X}"
        for item in self.ident_tree.get_children():
            vals = self.ident_tree.item(item, "values")
            if vals[0] == sa_txt and vals[1] == did_txt:
                self.ident_tree.item(item, values=(sa_txt, did_txt, name, value))
                return
        self.ident_tree.insert("", "end", values=(sa_txt, did_txt, name, value))

    def add_dtc(self, sa: int, code3: str, status: int, code: str, flags: list[str], ts):
        sa_txt = f"0x{sa:02X}"
        active_txt = "Да" if is_dtc_active(status) else "Нет"
        ftype_txt = describe_failure_type(code3) or "—"
        values = (sa_txt, code, code3, f"0x{status:02X}", active_txt, ftype_txt, ", ".join(flags), ts)
        for item in self.dtc_tree.get_children():
            vals = self.dtc_tree.item(item, "values")
            if vals[0] == sa_txt and vals[2] == code3:
                self.dtc_tree.item(item, values=values)
                return
        self.dtc_tree.insert("", "end", values=values)

    def add_event(self, sa: int, ts, text: str):
        self.events_text.insert("end", f"ЭБУ 0x{sa:02X}  {ts}: {text}\n")
        self.events_text.see("end")


class LiveTab(ttk.Frame):
    """Вкладка живого подключения: активный опрос ЭБУ по UDS (иденты, DTC,
    очистка DTC) + декодирование ответов на лету."""

    # Таймауты ожидания ответа ЭБУ. requestCorrectlyReceived-ResponsePending
    # (NRC 0x78) продлевает ожидание на PENDING_EXTRA_S ещё раз — так вела
    # себя реальная ECU в захваченном логе (см. чтение 0xF191 и очистку DTC).
    RESPONSE_TIMEOUT_S = 1.5
    PENDING_EXTRA_S = 5.0
    SEND_ATTEMPTS = 4  # реальная ЭБУ иногда не отвечает с первого раза (см. README) — повторяем запрос

    # Ожидание Flow Control от ЭБУ при ОТПРАВКЕ многокадрового запроса
    # (First Frame -> ждём FC -> Consecutive Frame(ы)) — не путать с
    # RESPONSE_TIMEOUT_S/PENDING_EXTRA_S, это ожидание самого UDS-ответа.
    FC_WAIT_TIMEOUT_S = 2.0
    FC_WAIT_MAX_RETRIES = 10  # на случай FlowStatus=WAIT (1) — не ждём бесконечно

    def __init__(self, master):
        super().__init__(master)
        self.bridge: BridgeProcess | None = None
        self.connected = False
        self.analyzer = Analyzer()
        self.display_queue: "queue.Queue[tuple]" = queue.Queue()

        # Синхронизация между потоком-читателем моста (кладёт сюда ответы
        # ЭБУ, адресованные тестеру) и рабочим потоком, который шлёт запрос
        # и ждёт ответ. Одновременно может идти только одна "операция"
        # (иденты/DTC/очистка) — это гарантирует _op_lock.
        self._resp_queue: "queue.Queue" = queue.Queue()
        # Flow Control кадры от ЭБУ в ответ на НАШИ многокадровые запросы
        # (First Frame -> FC -> Consecutive Frame) — отдельная очередь, т.к.
        # это не UDS-события и в _resp_queue не попадают.
        self._fc_queue: "queue.Queue" = queue.Queue()
        self._op_lock = threading.Lock()

        # Слушатели "сырых" кадров — например, вкладка "Монитор шины"
        # подписывается сюда, чтобы видеть АБСОЛЮТНО ВСЕ кадры на шине, а не
        # только те, что участвуют в UDS-обмене. Своего соединения вкладка
        # монитора не открывает (адаптер J2534 отдаёт эксклюзивный доступ
        # только одному каналу) — вместо этого она получает копию каждого
        # кадра отсюда. Вызывается из потока-читателя моста — колбэки должны
        # быть быстрыми и не трогать виджеты Tk напрямую.
        self._frame_listeners: list = []

        self.devices = find_j2534_devices()
        self.bridge_exe = find_bridge_exe()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text="Устройство J2534:").grid(row=0, column=0, sticky="w")
        device_frame = ttk.Frame(top)
        device_frame.grid(row=0, column=1, sticky="we", padx=4)
        device_frame.grid_columnconfigure(0, weight=1)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(device_frame, textvariable=self.device_var, width=40, state="readonly")
        self.device_combo["values"] = [d[0] for d in self.devices] or ["(не найдено)"]
        if self.devices:
            self.device_combo.current(0)
        self.device_combo.grid(row=0, column=0, sticky="we")
        self.refresh_devices_btn = ttk.Button(device_frame, text="🔄", width=3, command=self._refresh_devices)
        self.refresh_devices_btn.grid(row=0, column=1, sticky="e", padx=(4, 0))

        ttk.Label(top, text="Режим:").grid(row=0, column=2, sticky="e")
        self.mode_var = tk.StringVar(value="J1939")
        mode_frame = ttk.Frame(top)
        mode_frame.grid(row=0, column=3, sticky="w")
        self.mode_can_rb = ttk.Radiobutton(mode_frame, text="CAN", variable=self.mode_var, value="CAN", command=self._on_mode_change)
        self.mode_can_rb.pack(side="left")
        self.mode_j1939_rb = ttk.Radiobutton(mode_frame, text="J1939", variable=self.mode_var, value="J1939", command=self._on_mode_change)
        self.mode_j1939_rb.pack(side="left", padx=(8, 0))

        ttk.Label(top, text="Скорость:").grid(row=0, column=4, sticky="e")
        self.baud_var = tk.StringVar(value=J1939_BAUD_OPTIONS[0])
        self.baud_combo = ttk.Combobox(top, textvariable=self.baud_var, width=10, values=J1939_BAUD_OPTIONS)
        self.baud_combo.grid(row=0, column=5, sticky="w", padx=4)

        ttk.Label(top, text="Адрес ЭБУ (DA, hex):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.ecu_da_var = tk.StringVar(value="23")
        ttk.Entry(top, textvariable=self.ecu_da_var, width=8).grid(row=1, column=1, sticky="w", pady=(4, 0))

        ttk.Label(top, text="Адрес тестера (SA, hex):").grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.tester_sa_var = tk.StringVar(value="F2")
        ttk.Entry(top, textvariable=self.tester_sa_var, width=8).grid(row=1, column=3, sticky="w", pady=(4, 0))

        top.grid_columnconfigure(1, weight=1)

        device_note = (
            f"В списке выше — ВСЕ J2534-адаптеры, зарегистрированные в Windows (не только "
            f"Scanmatik): NEXIQ, Cummins, Kvaser и другие, если их драйверы установлены. "
            f"Сейчас на этом компьютере найдено: {len(self.devices)}. Кнопка 🔄 перечитывает "
            f"список заново — пригодится, если адаптер подключили уже после запуска программы."
        )
        ttk.Label(self, text=device_note, foreground="#555", wraplength=900, justify="left").pack(fill="x", padx=6)

        note = ("Программа сама отправляет запросы ЭБУ (адресация — как в реальном "
                "захваченном логе: точка-точка по CAN ID). Адреса ЭБУ/тестера выше "
                "можно поменять, если у вас другой блок или другая шина.")
        ttk.Label(self, text=note, foreground="#555", wraplength=900, justify="left").pack(fill="x", padx=6)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=4)
        self.connect_btn = tk.Button(
            ctrl, text="🔌  ПОДКЛЮЧИТЬСЯ", width=18, height=2, bg="#1565c0", fg="white",
            command=self._toggle_connection, font=("Segoe UI", 10, "bold"),
        )
        self.connect_btn.pack(side="left", padx=(0, 10))

        self.read_idents_btn = tk.Button(
            ctrl, text="Прочитать иденты", command=self._on_read_idents_click, state="disabled",
        )
        self.read_idents_btn.pack(side="left", padx=4)

        self.read_dtc_btn = tk.Button(
            ctrl, text="Прочитать DTC", command=self._on_read_dtc_click, state="disabled",
        )
        self.read_dtc_btn.pack(side="left", padx=4)

        self.clear_dtc_btn = tk.Button(
            ctrl, text="Очистить DTC", command=self._on_clear_dtc_click, state="disabled", bg="#b71c1c", fg="white",
        )
        self.clear_dtc_btn.pack(side="left", padx=4)

        did_frame = ttk.Frame(self)
        did_frame.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(did_frame, text="Свой DID (hex):").pack(side="left")
        self.custom_did_var = tk.StringVar(value="F190")
        ttk.Entry(did_frame, textvariable=self.custom_did_var, width=8).pack(side="left", padx=4)
        self.read_did_btn = tk.Button(
            did_frame, text="Прочитать", command=self._on_read_custom_did_click, state="disabled",
        )
        self.read_did_btn.pack(side="left")

        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=6)
        self.status_var = tk.StringVar(value="Не подключено")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="#555").pack(side="left")
        self.stats_var = tk.StringVar(value="Кадров: 0   UDS-событий: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side="right")

        self.op_status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.op_status_var, foreground="#1565c0").pack(fill="x", padx=6, pady=(2, 0))

        self.tables = EcuTables(self)
        self.tables.pack(fill="both", expand=True)

        if self.bridge_exe is None:
            self.status_var.set("Не найден smbridge.exe (см. README.md)")
        if not self.devices:
            self.status_var.set("В реестре не найдено J2534-устройств")

        self.frame_count = 0
        self.event_count = 0
        self._poll_display_queue()

    def _on_mode_change(self):
        if self.mode_var.get() == "J1939":
            self.baud_combo["values"] = J1939_BAUD_OPTIONS
            self.baud_var.set(J1939_BAUD_OPTIONS[0])
        else:
            self.baud_combo["values"] = CAN_BAUD_OPTIONS
            self.baud_var.set(CAN_BAUD_OPTIONS[2])

    def _refresh_devices(self):
        """Перечитывает список J2534-адаптеров из реестра Windows заново —
        нужно, например, если адаптер (Scanmatik, NEXIQ, Cummins, Kvaser и
        т.п.) подключили уже после запуска программы: сам список читается
        только один раз при старте (см. __init__), автоматически не
        обновляется."""
        if self.connected:
            messagebox.showinfo(
                "Подключено", "Сначала отключитесь («Живое подключение»), чтобы обновить список устройств."
            )
            return
        self.devices = find_j2534_devices()
        self.device_combo["values"] = [d[0] for d in self.devices] or ["(не найдено)"]
        if self.devices:
            self.device_combo.current(0)
        else:
            self.device_var.set("(не найдено)")
        self._set_status(f"Список устройств обновлён: найдено {len(self.devices)}.")

    def _selected_dll_path(self) -> str | None:
        idx = self.device_combo.current()
        if idx < 0 or idx >= len(self.devices):
            return None
        return self.devices[idx][1]

    # ---------------------------------------------------------- адреса
    def _addrs(self) -> tuple[int, int] | None:
        """Возвращает (tester_sa, ecu_da) или None, если поля некорректны."""
        try:
            sa = int(self.tester_sa_var.get(), 16) & 0xFF
            da = int(self.ecu_da_var.get(), 16) & 0xFF
            return sa, da
        except ValueError:
            messagebox.showerror("Ошибка", "Адрес ЭБУ и тестера — hex-число (например, 23 или F2).")
            return None

    # ------------------------------------------------------- подключение
    def _toggle_connection(self):
        if not self.connected:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        if self.bridge_exe is None:
            messagebox.showerror("Ошибка", "smbridge.exe не найден. Соберите мост (см. README.md).")
            return
        dll_path = self._selected_dll_path()
        if not dll_path:
            messagebox.showerror("Ошибка", "Не выбрано J2534-устройство.")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("Ошибка", "Некорректная скорость CAN.")
            return
        if self._addrs() is None:
            return

        self.analyzer = Analyzer()
        self.tables.clear()
        self.frame_count = 0
        self.event_count = 0

        self.bridge = BridgeProcess(self.bridge_exe, on_frame=self._on_frame, on_info=self._on_info, on_error=self._on_error)
        try:
            self.bridge.start_process()
            self.bridge.send(f"OPEN {dll_path}")
            self.bridge.send(f"CONNECT {self.mode_var.get()} {baud}")
            self.bridge.send("START")
        except Exception as e:
            self.status_var.set(f"Ошибка запуска моста: {e}")
            self.bridge = None
            return

        self.connected = True
        self.connect_btn.configure(text="⏻  ОТКЛЮЧИТЬСЯ", bg="#c62828")
        self.status_var.set(f"Подключено: {self.mode_var.get()} @ {baud} бод")
        self.device_combo.configure(state="disabled")
        self.baud_combo.configure(state="disabled")
        self.mode_can_rb.configure(state="disabled")
        self.mode_j1939_rb.configure(state="disabled")
        for btn in (self.read_idents_btn, self.read_dtc_btn, self.clear_dtc_btn, self.read_did_btn):
            btn.configure(state="normal")

    def _disconnect(self):
        if self.bridge is not None:
            try:
                self.bridge.send("STOP")
                self.bridge.send("CLOSE")
            except Exception:
                pass
            self.bridge.terminate()
            self.bridge = None
        self.connected = False
        self.connect_btn.configure(text="🔌  ПОДКЛЮЧИТЬСЯ", bg="#1565c0")
        self.status_var.set("Не подключено")
        self.device_combo.configure(state="readonly")
        self.baud_combo.configure(state="normal")
        self.mode_can_rb.configure(state="normal")
        self.mode_j1939_rb.configure(state="normal")
        for btn in (self.read_idents_btn, self.read_dtc_btn, self.clear_dtc_btn, self.read_did_btn):
            btn.configure(state="disabled")

    # ------------------------------------------------------ приём кадров
    def add_frame_listener(self, callback):
        """Регистрирует callback(pc_ms, dev_ts_us, can_id_hex, ide, data_hex,
        rx_status_hex), который будет получать КАЖДЫЙ кадр с шины
        (используется вкладкой "Монитор шины"). dev_ts_us — таймstamp самого
        адаптера (моста smbridge.exe), не ПК — он точнее pc_ms, см.
        bridge_common.parse_frame_line. Список слушателей не сбрасывается
        при переподключении — достаточно подписаться один раз при создании
        вкладки."""
        self._frame_listeners.append(callback)

    def remove_frame_listener(self, callback):
        try:
            self._frame_listeners.remove(callback)
        except ValueError:
            pass

    def _on_frame(self, line: str):
        parsed = parse_frame_line(line)
        if parsed is None:
            return
        pc_ms, dev_ts, can_id_hex, ide, data_hex, rx_status_hex = parsed

        for cb in self._frame_listeners:
            try:
                cb(pc_ms, dev_ts, can_id_hex, ide, data_hex, rx_status_hex)
            except Exception:
                pass  # слушатель не должен ронять обработку основного UDS-обмена

        if ide != "1":
            return  # диагностика точка-точка только на 29-битных ID
        try:
            data_bytes = bytes.fromhex(data_hex.replace(" ", ""))
        except ValueError:
            return

        addrs = self._addrs()

        if addrs is not None:
            tester_sa, ecu_da = addrs
            addr = decode_29bit_id(can_id_hex)

            # Flow Control от ЭБУ, адресованный нам — это ответ на НАШ
            # многокадровый ЗАПРОС (мы отправили First Frame, ждём разрешения
            # слать Consecutive Frame). Отдаём рабочему потоку, который сейчас
            # шлёт многокадровый запрос и ждёт именно этот кадр.
            fc = parse_flow_control(data_bytes)
            if fc is not None and addr.da == tester_sa and addr.sa == ecu_da:
                self._fc_queue.put(fc)

            # Автоматический Flow Control: если это First Frame многокадрового
            # ОТВЕТА ЭБУ, адресованного НАШЕМУ тестеру — сразу отвечаем
            # "продолжайте слать", иначе ЭБУ зависнет в ожидании (см.
            # ISO 15765-2). Без этого длинные ответы (например, VIN) никогда
            # не соберутся целиком.
            if self.bridge is not None and addr.da == tester_sa and is_first_frame(data_bytes):
                fc_id = build_can_id(tester_sa, addr.sa)
                fc_frame = build_flow_control()
                try:
                    self.bridge.send(f"SEND {fc_id:X} " + " ".join(f"{b:02X}" for b in fc_frame))
                except Exception:
                    pass

        pc_time_iso = dt.datetime.fromtimestamp(int(pc_ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.frame_count += 1
        ev = self.analyzer.feed_frame(can_id_hex, data_hex, pc_time_iso)
        if ev is not None:
            self.event_count += 1
            self.display_queue.put(("event", ev))
            # Ответ, адресованный нашему тестеру — отдаём рабочему потоку,
            # который сейчас ждёт результат отправленного запроса.
            if addrs is not None and ev.kind in ("positive", "negative"):
                tester_sa, ecu_da = addrs
                if ev.da == tester_sa and ev.sa == ecu_da:
                    self._resp_queue.put(ev)

    def _on_info(self, line: str):
        pass

    def _on_error(self, line: str):
        self.display_queue.put(("error", line))

    def _poll_display_queue(self):
        try:
            while True:
                kind, payload = self.display_queue.get_nowait()
                if kind == "event":
                    ev = payload
                    if ev.kind == "positive" and ev.service == 0x22 and ev.did is not None:
                        name, val = self.analyzer.ecus[ev.sa]["idents"].get(ev.did, (DID_NAMES.get(ev.did, ""), ""))
                        self.tables.add_ident(ev.sa, ev.did, name, val)
                    if ev.kind == "positive" and ev.service == 0x19 and ev.dtcs:
                        for code3, status, code, flags in ev.dtcs:
                            self.tables.add_dtc(ev.sa, code3, status, code, flags, ev.ts)
                    if ev.service == 0x14:
                        self.tables.add_event(ev.sa, ev.ts, ev.summary)
                elif kind == "error":
                    self.status_var.set(payload)
                elif kind == "opstatus":
                    self.op_status_var.set(payload)
                elif kind == "opdone":
                    state = "normal" if self.connected else "disabled"
                    for btn in (self.read_idents_btn, self.read_dtc_btn, self.clear_dtc_btn, self.read_did_btn):
                        btn.configure(state=state)
        except queue.Empty:
            pass

        rstats = self.analyzer.reassembler.stats
        corrupted = rstats["corrupted_gap"] + rstats["abandoned"]
        extra = f"   Повреждено многокадровых: {corrupted}/{rstats['started']}" if corrupted else ""
        self.stats_var.set(f"Кадров: {self.frame_count}   UDS-событий: {self.event_count}{extra}")
        self.after(200, self._poll_display_queue)

    # ------------------------------------------------- отправка запросов
    def _set_status(self, text: str):
        self.display_queue.put(("opstatus", text))

    def _send_uds_payload(self, payload: bytes):
        """Отправляет запрос тестер->ЭБУ: Single Frame, если payload помещается
        в 7 байт, иначе — многокадровый запрос (First Frame + Consecutive
        Frame(ы) с ожиданием Flow Control от ЭБУ между блоками, см.
        _send_multi_frame_request)."""
        if self.bridge is None:
            raise RuntimeError("Мост не подключён")
        addrs = self._addrs()
        if addrs is None:
            raise RuntimeError("Некорректные адреса ЭБУ/тестера")
        tester_sa, ecu_da = addrs
        can_id = build_can_id(tester_sa, ecu_da)
        # Опустошаем очереди ответов/Flow Control от предыдущего запроса на всякий случай.
        while not self._resp_queue.empty():
            try:
                self._resp_queue.get_nowait()
            except queue.Empty:
                break
        while not self._fc_queue.empty():
            try:
                self._fc_queue.get_nowait()
            except queue.Empty:
                break

        if len(payload) <= 7:
            frame = build_sf(payload)
            self.bridge.send(f"SEND {can_id:X} " + " ".join(f"{b:02X}" for b in frame))
        else:
            if len(payload) > MAX_MULTI_FRAME_PAYLOAD:
                raise ValueError(
                    f"Слишком длинный запрос: {len(payload)} байт (максимум {MAX_MULTI_FRAME_PAYLOAD})"
                )
            self._send_multi_frame_request(can_id, payload)

    def _wait_fc(self, timeout: float):
        """Ждёт Flow Control от ЭБУ (ответ на наш First Frame или очередной
        блок Consecutive Frame). None — если не дождались."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self._fc_queue.get(timeout=remaining)
            except queue.Empty:
                return None

    def _wait_fc_continue(self, timeout: float):
        """Как _wait_fc, но сама разбирается с FlowStatus=WAIT (1) — повторно
        ждёт следующий FC (до FC_WAIT_MAX_RETRIES раз), пока не придёт
        ContinueToSend (0) или Overflow (2), либо не кончится терпение."""
        fc = self._wait_fc(timeout)
        waits = 0
        while fc is not None and fc[0] == 1:
            waits += 1
            if waits > self.FC_WAIT_MAX_RETRIES:
                return None
            fc = self._wait_fc(timeout)
        return fc

    def _send_multi_frame_request(self, can_id: int, payload: bytes):
        """Отправляет payload длиннее 7 байт как First Frame + Consecutive
        Frame(ы), с ожиданием Flow Control от ЭБУ (ISO 15765-2): после First
        Frame ждём FC перед первой Consecutive Frame, а если ЭБУ прислала
        blockSize > 0 — ждём новый FC после каждого блока из blockSize
        кадров. STmin из FC используется как пауза между кадрами внутри
        блока."""
        ff = build_ff(payload)
        self.bridge.send(f"SEND {can_id:X} " + " ".join(f"{b:02X}" for b in ff))

        fc = self._wait_fc_continue(self.FC_WAIT_TIMEOUT_S)
        if fc is None:
            raise RuntimeError("ЭБУ не подтвердила приём многокадрового запроса (нет Flow Control) — отправка прервана")
        fs, block_size, st_min = fc
        if fs == 2:
            raise RuntimeError("ЭБУ отклонила многокадровый запрос (Flow Control: overflow)")

        sent = 6  # первые 6 байт уже ушли в First Frame
        seq = 1
        delay = st_min_to_seconds(st_min)
        count_in_block = 0
        while sent < len(payload):
            chunk = payload[sent:sent + 7]
            cf = build_cf(seq, chunk)
            self.bridge.send(f"SEND {can_id:X} " + " ".join(f"{b:02X}" for b in cf))
            sent += len(chunk)
            seq = (seq + 1) & 0xF
            count_in_block += 1
            if sent >= len(payload):
                break
            if block_size != 0 and count_in_block >= block_size:
                fc = self._wait_fc_continue(self.FC_WAIT_TIMEOUT_S)
                if fc is None:
                    raise RuntimeError("ЭБУ не прислала очередной Flow Control — отправка многокадрового запроса прервана")
                fs, block_size, st_min = fc
                if fs == 2:
                    raise RuntimeError("ЭБУ отклонила продолжение многокадрового запроса (Flow Control: overflow)")
                delay = st_min_to_seconds(st_min)
                count_in_block = 0
            elif delay > 0:
                time.sleep(delay)

    def _wait_response(self, timeout: float):
        """Ждёт ответ ЭБУ на последний отправленный запрос. Автоматически
        продлевает ожидание при NRC 0x78 (responsePending) — именно так вела
        себя реальная ЭБУ при чтении идента и при очистке DTC в захваченном
        логе (7F .. 78, затем настоящий ответ спустя секунды)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ev = self._resp_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if ev.kind == "negative" and ev.nrc == 0x78:
                deadline = time.monotonic() + self.PENDING_EXTRA_S
                continue
            return ev

    def _send_and_wait(self, payload: bytes, label: str):
        """Отправляет запрос и ждёт ответ, при необходимости повторяя отправку.

        На реальной шине ЭБУ иногда не отвечает на первую же попытку (это
        видно и в исходном захваченном логе — сторонний тестер там тоже
        слал один и тот же запрос повторно, прежде чем получал ответ) —
        поэтому здесь до SEND_ATTEMPTS попыток с быстрым таймаутом между
        ними, а не одна попытка с длинным ожиданием."""
        last_ev = None
        for attempt in range(1, self.SEND_ATTEMPTS + 1):
            if attempt > 1:
                self._set_status(f"{label}: нет ответа, повтор {attempt}/{self.SEND_ATTEMPTS}...")
            self._send_uds_payload(payload)
            ev = self._wait_response(self.RESPONSE_TIMEOUT_S)
            if ev is not None:
                return ev
            last_ev = ev
        return last_ev

    def _run_op(self, worker):
        """Запускает worker в фоновом потоке, если нет другой активной
        операции; отключает кнопки на время выполнения."""
        if not self.connected:
            messagebox.showinfo("Не подключено", "Сначала нажмите «Подключиться».")
            return
        if self._addrs() is None:
            return
        if self._op_lock.locked():
            messagebox.showinfo("Занято", "Дождитесь завершения текущего запроса.")
            return

        buttons = (self.read_idents_btn, self.read_dtc_btn, self.clear_dtc_btn, self.read_did_btn)
        for btn in buttons:
            btn.configure(state="disabled")

        def run():
            with self._op_lock:
                try:
                    worker()
                except Exception as e:
                    self._set_status(f"Ошибка: {e}")
            # Кнопки — виджеты Tk, трогать их можно только из главного потока,
            # поэтому просим об этом через ту же очередь, что и остальной UI.
            self.display_queue.put(("opdone", None))

        threading.Thread(target=run, daemon=True).start()

    def _on_read_idents_click(self):
        self._run_op(self._worker_read_idents)

    def _on_read_custom_did_click(self):
        try:
            did = int(self.custom_did_var.get(), 16) & 0xFFFF
        except ValueError:
            messagebox.showerror("Ошибка", "DID — hex-число (например, F190).")
            return
        self._run_op(lambda: self._worker_read_one_did(did))

    def _on_read_dtc_click(self):
        self._run_op(self._worker_read_dtc)

    def _on_clear_dtc_click(self):
        addrs = self._addrs()
        if addrs is None:
            return
        _tester_sa, ecu_da = addrs
        if not messagebox.askyesno(
            "Подтверждение",
            f"Это НЕОБРАТИМО сотрёт ВСЕ коды ошибок (DTC) на ЭБУ по адресу 0x{ecu_da:02X}.\n\n"
            "Продолжить?",
            icon="warning",
        ):
            return
        self._run_op(self._worker_clear_dtc)

    def _worker_read_one_did(self, did: int):
        label = f"0x{did:04X}"
        self._set_status(f"Запрос идента {label}...")
        ev = self._send_and_wait(req_read_did(did), label)
        if ev is None:
            self._set_status(f"0x{did:04X}: нет ответа (таймаут)")
        elif ev.kind == "negative":
            nrc_name = NRC_NAMES.get(ev.nrc, f"0x{ev.nrc:02X}" if ev.nrc is not None else "?")
            self._set_status(f"0x{did:04X}: отрицательный ответ ({nrc_name})")
        else:
            self._set_status(f"0x{did:04X}: {ev.summary}")

    def _worker_read_idents(self):
        for did in DEFAULT_IDENT_DIDS:
            self._worker_read_one_did(did)
            time.sleep(0.15)
        self._set_status("Готово: чтение идентов завершено")

    def _worker_read_dtc(self):
        self._set_status("Запрос списка DTC...")
        ev = self._send_and_wait(req_read_dtc_by_status_mask(0xFF), "Чтение DTC")
        if ev is None:
            self._set_status("Чтение DTC: нет ответа (таймаут)")
        elif ev.kind == "negative":
            nrc_name = NRC_NAMES.get(ev.nrc, f"0x{ev.nrc:02X}" if ev.nrc is not None else "?")
            self._set_status(f"Чтение DTC: отрицательный ответ ({nrc_name})")
        else:
            self._set_status(f"Готово: {ev.summary}")

    def _worker_clear_dtc(self):
        self._set_status("Отправляю запрос очистки DTC...")
        ev = self._send_and_wait(req_clear_dtc(), "Очистка DTC")
        if ev is None:
            self._set_status("Очистка DTC: нет ответа (таймаут)")
        elif ev.kind == "negative":
            nrc_name = NRC_NAMES.get(ev.nrc, f"0x{ev.nrc:02X}" if ev.nrc is not None else "?")
            self._set_status(f"Очистка DTC не выполнена: {nrc_name}")
        else:
            self._set_status("Готово: DTC успешно очищены")

    def on_close(self):
        if self.connected:
            self._disconnect()


# --------------------------------------------------------------------- конструктор произвольных UDS-запросов

# Описание сервисов, доступных в конструкторе: "kind" определяет, какие
# виджеты параметров показывать и как собирать байты запроса (см.
# RequestTab._build_params/_build_payload). "danger" — сервисы, которые
# реально меняют состояние ЭБУ (сброс, запись, рутины, произвольные байты) —
# перед отправкой требуют подтверждения, как и «Очистить DTC».
SERVICE_DEFS = [
    {"key": "22", "label": "0x22 — ReadDataByIdentifier (чтение идента по DID)", "kind": "did_read", "danger": False},
    {"key": "19", "label": "0x19 — ReadDTCInformation (чтение кодов ошибок)", "kind": "read_dtc", "danger": False},
    {"key": "14", "label": "0x14 — ClearDiagnosticInformation (очистка DTC)", "kind": "clear_dtc", "danger": True},
    {"key": "10", "label": "0x10 — DiagnosticSessionControl (смена сессии)", "kind": "session", "danger": True},
    {"key": "11", "label": "0x11 — ECUReset (сброс ЭБУ)", "kind": "reset", "danger": True},
    {"key": "27", "label": "0x27 — SecurityAccess (запрос seed)", "kind": "security", "danger": False},
    {"key": "2E", "label": "0x2E — WriteDataByIdentifier (запись идента)", "kind": "did_write", "danger": True},
    {"key": "3E", "label": "0x3E — TesterPresent (я на связи)", "kind": "tester_present", "danger": False},
    {"key": "31", "label": "0x31 — RoutineControl (управление рутиной)", "kind": "routine", "danger": True},
    {"key": "raw", "label": "Свой запрос — произвольные байты (hex, включая SID)", "kind": "raw", "danger": True},
]


class RequestTab(ttk.Frame):
    """Вкладка «Свои UDS-запросы»: конструктор произвольного запроса —
    сервис и параметры выбираются из списков с человекочитаемыми именами
    (а не только вводом чисел), есть предпросмотр байтов, очередь для
    отправки нескольких запросов подряд и журнал отправленных
    запросов/ответов (журнал и таблицы идентов/DTC — с копированием в
    буфер обмена: выделение мышью + Ctrl+C, или через правую кнопку мыши).

    Соединение с прибором НЕ своё — используется то же самое, что открыто
    на вкладке «Живое подключение» (self.live_tab): J2534-адаптер отдаёт
    эксклюзивный доступ только одному открывшему его процессу/каналу, так
    что вторая независимая попытка PassThruOpen с этим же устройством
    гарантированно провалится с DEVICE_NOT_FOUND."""

    def __init__(self, master, live_tab: LiveTab):
        super().__init__(master)
        self.live_tab = live_tab
        self.display_queue: "queue.Queue[tuple]" = queue.Queue()
        self._param_vars: dict[str, tk.StringVar] = {}
        # Очередь запросов для отправки нескольких подряд одной кнопкой —
        # список {"payload": bytes, "desc": str, "danger": bool}.
        self.request_queue: list[dict] = []

        note = ("Отправка идёт через то же соединение, что и вкладка «Живое подключение» — "
                "сначала подключитесь там. Выберите сервис и параметры (можно из списка, можно "
                "вписать своё hex-значение) и нажмите «Отправить запрос», либо «В очередь», чтобы "
                "накопить несколько запросов и отправить их все подряд кнопкой ниже. Журнал и таблицы "
                "идентов/DTC можно копировать: выделите текст и Ctrl+C, или через правую кнопку мыши.")
        ttk.Label(self, text=note, foreground="#555", wraplength=920, justify="left").pack(fill="x", padx=6, pady=(6, 4))

        svc_frame = ttk.Frame(self)
        svc_frame.pack(fill="x", padx=6)
        ttk.Label(svc_frame, text="Сервис (UDS SID):").grid(row=0, column=0, sticky="w")
        self.service_var = tk.StringVar()
        self.service_combo = ttk.Combobox(
            svc_frame, textvariable=self.service_var, state="readonly", width=56,
            values=[d["label"] for d in SERVICE_DEFS],
        )
        self.service_combo.current(0)
        self.service_combo.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self.service_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_service_change())
        svc_frame.grid_columnconfigure(1, weight=1)

        self.params_frame = ttk.LabelFrame(self, text="Параметры запроса")
        self.params_frame.pack(fill="x", padx=6, pady=(6, 4))

        preview_frame = ttk.Frame(self)
        preview_frame.pack(fill="x", padx=6)
        ttk.Label(preview_frame, text="Будет отправлено:").pack(side="left")
        self.preview_var = tk.StringVar(value="—")
        ttk.Label(preview_frame, textvariable=self.preview_var, foreground="#1565c0", font=("Consolas", 10, "bold")).pack(side="left", padx=6)

        send_frame = ttk.Frame(self)
        send_frame.pack(fill="x", padx=6, pady=(4, 4))
        self.send_btn = tk.Button(
            send_frame, text="📤  ОТПРАВИТЬ ЗАПРОС", width=22, height=2, bg="#1565c0", fg="white",
            font=("Segoe UI", 10, "bold"), state="disabled", command=self._on_send_click,
        )
        self.send_btn.pack(side="left")
        self.add_queue_btn = tk.Button(send_frame, text="➕ В очередь", command=self._on_add_to_queue)
        self.add_queue_btn.pack(side="left", padx=8)

        # --------------------------------------------------- очередь запросов
        queue_frame = ttk.LabelFrame(self, text="Очередь запросов — отправить несколько подряд")
        queue_frame.pack(fill="x", padx=6, pady=(0, 4))

        queue_btns = ttk.Frame(queue_frame)
        queue_btns.pack(fill="x", padx=4, pady=(4, 2))
        self.remove_queue_btn = tk.Button(queue_btns, text="🗑 Удалить", command=self._on_remove_from_queue)
        self.remove_queue_btn.pack(side="left")
        self.move_up_btn = tk.Button(queue_btns, text="▲", width=3, command=lambda: self._move_queue_item(-1))
        self.move_up_btn.pack(side="left", padx=(6, 0))
        self.move_down_btn = tk.Button(queue_btns, text="▼", width=3, command=lambda: self._move_queue_item(1))
        self.move_down_btn.pack(side="left", padx=2)
        self.clear_queue_btn = tk.Button(queue_btns, text="Очистить очередь", command=self._on_clear_queue)
        self.clear_queue_btn.pack(side="left", padx=8)
        self.send_queue_btn = tk.Button(
            queue_btns, text="▶▶  ОТПРАВИТЬ ОЧЕРЕДЬ ПОДРЯД", bg="#2e7d32", fg="white",
            font=("Segoe UI", 9, "bold"), state="disabled", command=self._on_send_queue_click,
        )
        self.send_queue_btn.pack(side="right")

        self.queue_tree = ttk.Treeview(queue_frame, columns=("n", "desc", "bytes"), show="headings", height=4)
        for col, text, w in (("n", "#", 30), ("desc", "Запрос", 560), ("bytes", "Байт", 60)):
            self.queue_tree.heading(col, text=text)
            self.queue_tree.column(col, width=w, anchor="w")
        self.queue_tree.pack(fill="x", padx=4, pady=(0, 4))
        _bind_treeview_copy(self.queue_tree)

        log_header = ttk.Frame(self)
        log_header.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(log_header, text="Журнал запросов и ответов:").pack(side="left")
        tk.Button(log_header, text="📋 Копировать журнал", command=self._copy_log).pack(side="right")
        tk.Button(log_header, text="Очистить журнал", command=self._clear_log).pack(side="right", padx=(0, 6))

        self.log_text = scrolledtext.ScrolledText(self, height=16, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        _make_text_copyable(self.log_text)

        self._on_service_change()
        self._poll()

    # ------------------------------------------------------------- helpers

    def _current_def(self) -> dict:
        idx = self.service_combo.current()
        if idx < 0:
            idx = 0
        return SERVICE_DEFS[idx]

    @staticmethod
    def _parse_hex_int(text: str, nbytes: int, field_name: str) -> int:
        text = text.strip().replace("0x", "").replace("0X", "")
        if not text:
            raise ValueError(f"{field_name}: пустое значение.")
        try:
            v = int(text, 16)
        except ValueError:
            raise ValueError(f"{field_name}: «{text}» — это не hex-число.")
        limit = 1 << (nbytes * 8)
        if not (0 <= v < limit):
            raise ValueError(f"{field_name}: значение вне диапазона (0..{limit - 1:X} hex).")
        return v

    @staticmethod
    def _parse_hex_bytes(text: str, allow_empty: bool = True) -> bytes:
        text = text.strip()
        if not text:
            if allow_empty:
                return b""
            raise ValueError("Нужно ввести hex-байты.")
        tokens = text.replace(",", " ").split()
        try:
            return bytes(int(t, 16) for t in tokens)
        except ValueError:
            raise ValueError(f"«{text}» — некорректные hex-байты (пример: 41 42 43).")

    def _named_picker(self, row: int, label_text: str, names: dict[int, str], default: int,
                       var_key: str, nbytes: int = 1, combo_width: int = 44):
        """Строка 'выбор из списка по имени' + рядом поле с hex-значением —
        можно выбрать вариант, а можно и вписать своё число, оба способа
        синхронизированы через одну StringVar."""
        hex_digits = nbytes * 2
        hex_var = tk.StringVar(value=f"{default:0{hex_digits}X}")
        self._param_vars[var_key] = hex_var

        ttk.Label(self.params_frame, text=label_text).grid(row=row, column=0, sticky="w", pady=3, padx=(4, 0))

        keys = sorted(names.keys())
        combo = ttk.Combobox(
            self.params_frame, state="readonly", width=combo_width,
            values=[f"0x{k:0{hex_digits}X} — {v}" for k, v in sorted(names.items())],
        )
        if default in keys:
            combo.current(keys.index(default))
        combo.grid(row=row, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(self.params_frame, text="hex:").grid(row=row, column=2, sticky="e")
        entry = ttk.Entry(self.params_frame, textvariable=hex_var, width=hex_digits + 2)
        entry.grid(row=row, column=3, sticky="w", padx=4, pady=3)

        def on_combo_select(_evt=None):
            idx = combo.current()
            if 0 <= idx < len(keys):
                hex_var.set(f"{keys[idx]:0{hex_digits}X}")

        combo.bind("<<ComboboxSelected>>", on_combo_select)
        hex_var.trace_add("write", lambda *a: self._update_preview())
        return hex_var

    def _plain_entry(self, row: int, label_text: str, default: str, var_key: str, width: int = 18, note: str = ""):
        var = tk.StringVar(value=default)
        self._param_vars[var_key] = var
        ttk.Label(self.params_frame, text=label_text).grid(row=row, column=0, sticky="w", pady=3, padx=(4, 0))
        ttk.Entry(self.params_frame, textvariable=var, width=width).grid(row=row, column=1, sticky="w", padx=4, pady=3)
        if note:
            ttk.Label(self.params_frame, text=note, foreground="#777").grid(row=row, column=2, columnspan=2, sticky="w")
        var.trace_add("write", lambda *a: self._update_preview())
        return var

    # ------------------------------------------------------------- построение UI под сервис

    def _on_service_change(self):
        for w in self.params_frame.winfo_children():
            w.destroy()
        self._param_vars = {}

        kind = self._current_def()["kind"]

        if kind == "did_read":
            self._named_picker(0, "DID (идент):", DID_NAMES, 0xF190, "did", nbytes=2)
        elif kind == "did_write":
            self._named_picker(0, "DID (идент):", DID_NAMES, 0xF190, "did", nbytes=2)
            self._plain_entry(1, "Новое значение (hex-байты):", "", "value", width=50,
                               note="например: 41 42 43 (ASCII 'ABC'); длиннее 7 байт — уйдёт многокадровым запросом")
        elif kind == "read_dtc":
            self._named_picker(0, "Подфункция:", READ_DTC_SUBFUNCTIONS, 0x02, "subfn")
            self._plain_entry(1, "Маска статуса (hex):", "FF", "mask", width=8,
                               note="используется не всеми подфункциями (напр. не нужна для reportSupportedDTC)")
        elif kind == "clear_dtc":
            self._plain_entry(0, "Группа DTC (hex):", "FFFFFF", "group", width=10, note="FFFFFF = все группы")
        elif kind == "session":
            self._named_picker(0, "Тип сессии:", SESSION_NAMES, 0x03, "session")
        elif kind == "reset":
            self._named_picker(0, "Тип сброса:", RESET_TYPE_NAMES, 0x01, "reset_type")
        elif kind == "security":
            self._plain_entry(0, "Уровень доступа (hex):", "01", "level", width=8,
                               note="нечётный уровень = запрос seed; ключ по seed мы не считаем (нужен алгоритм производителя)")
        elif kind == "tester_present":
            ttk.Label(self.params_frame, text="Параметров нет — просто сигнал 'тестер на связи'.", foreground="#777").grid(
                row=0, column=0, sticky="w", padx=4, pady=6)
        elif kind == "routine":
            self._named_picker(0, "Тип управления:", ROUTINE_CONTROL_NAMES, 0x01, "ctrl")
            self._plain_entry(1, "ID рутины (hex):", "0000", "routine_id", width=8)
            self._plain_entry(2, "Доп. данные (hex-байты, необязательно):", "", "data", width=24)
        elif kind == "raw":
            self._plain_entry(0, "Байты запроса (hex, первый байт — SID):", "22 F1 90", "raw", width=50,
                               note="например: 22 F1 90 — готовый ReadDataByIdentifier(VIN); длиннее 7 байт — многокадровый запрос")

        self._update_preview()

    # ------------------------------------------------------------- сборка запроса

    def _build_payload(self) -> tuple[bytes, str]:
        kind = self._current_def()["kind"]
        v = self._param_vars

        if kind == "did_read":
            did = self._parse_hex_int(v["did"].get(), 2, "DID")
            return req_read_did(did), f"ReadDataByIdentifier: {DID_NAMES.get(did, f'DID 0x{did:04X}')} (0x{did:04X})"

        if kind == "did_write":
            did = self._parse_hex_int(v["did"].get(), 2, "DID")
            value = self._parse_hex_bytes(v["value"].get(), allow_empty=False)
            return req_write_did(did, value), (
                f"WriteDataByIdentifier: {DID_NAMES.get(did, f'DID 0x{did:04X}')} (0x{did:04X}) = "
                f"{value.hex(' ').upper()}"
            )

        if kind == "read_dtc":
            subfn = self._parse_hex_int(v["subfn"].get(), 1, "Подфункция")
            mask = self._parse_hex_int(v["mask"].get(), 1, "Маска статуса")
            subfn_name = READ_DTC_SUBFUNCTIONS.get(subfn, f"0x{subfn:02X}")
            desc = f"ReadDTCInformation: {subfn_name}"
            if subfn in DTC_SUBFUNCTIONS_WITH_MASK:
                desc += f", маска=0x{mask:02X}"
            return req_read_dtc(subfn, mask), desc

        if kind == "clear_dtc":
            group = self._parse_hex_int(v["group"].get(), 3, "Группа")
            grp_txt = "ВСЕ группы" if group == 0xFFFFFF else f"группа 0x{group:06X}"
            return req_clear_dtc(group), f"ClearDiagnosticInformation: {grp_txt}"

        if kind == "session":
            session = self._parse_hex_int(v["session"].get(), 1, "Тип сессии")
            return req_diag_session_control(session), f"DiagnosticSessionControl: {SESSION_NAMES.get(session, f'0x{session:02X}')}"

        if kind == "reset":
            rtype = self._parse_hex_int(v["reset_type"].get(), 1, "Тип сброса")
            return req_ecu_reset(rtype), f"ECUReset: {RESET_TYPE_NAMES.get(rtype, f'0x{rtype:02X}')}"

        if kind == "security":
            level = self._parse_hex_int(v["level"].get(), 1, "Уровень")
            return req_security_access(level), f"SecurityAccess: запрос уровня 0x{level:02X}"

        if kind == "tester_present":
            return req_tester_present(), "TesterPresent"

        if kind == "routine":
            ctrl = self._parse_hex_int(v["ctrl"].get(), 1, "Тип управления")
            routine_id = self._parse_hex_int(v["routine_id"].get(), 2, "ID рутины")
            data = self._parse_hex_bytes(v["data"].get(), allow_empty=True)
            ctrl_name = ROUTINE_CONTROL_NAMES.get(ctrl, f"0x{ctrl:02X}")
            return req_routine_control(ctrl, routine_id, data), f"RoutineControl {ctrl_name}, ID=0x{routine_id:04X}"

        if kind == "raw":
            payload = self._parse_hex_bytes(v["raw"].get(), allow_empty=False)
            return payload, f"Свой запрос ({len(payload)} байт)"

        raise ValueError("Неизвестный тип сервиса.")

    def _update_preview(self):
        try:
            payload, desc = self._build_payload()
        except ValueError as e:
            self.preview_var.set(f"— ({e})")
            return
        if len(payload) == 0:
            self.preview_var.set("— (пустой запрос)")
            return
        if len(payload) > MAX_MULTI_FRAME_PAYLOAD:
            self.preview_var.set(
                f"{desc}  ⚠ {len(payload)} байт — превышен предел ISO-TP ({MAX_MULTI_FRAME_PAYLOAD} байт)"
            )
            return
        if len(payload) <= 7:
            self.preview_var.set(f"{payload.hex(' ').upper()}   —   {desc}")
        else:
            # Многокадровый запрос — показываем, во сколько кадров он реально
            # разложится (First Frame + Consecutive Frame), а не полный hex-дамп.
            n_cf = -(-(len(payload) - 6) // 7)  # округление вверх
            preview_bytes = payload[:12].hex(" ").upper()
            self.preview_var.set(
                f"{preview_bytes}...   —   {desc}   [{len(payload)} байт, многокадровый: "
                f"1 First Frame + {n_cf} Consecutive Frame]"
            )

    # ------------------------------------------------------------- журнал

    def _log(self, line: str):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {line}\n")
        self.log_text.see("end")

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _copy_log(self):
        _copy_text_to_clipboard(self.log_text, self.log_text.get("1.0", "end-1c"))

    # -------------------------------------------------------------- очередь

    def _refresh_queue_view(self):
        self.queue_tree.delete(*self.queue_tree.get_children())
        for i, item in enumerate(self.request_queue, start=1):
            self.queue_tree.insert("", "end", values=(i, item["desc"], len(item["payload"])))
        can_send = bool(self.request_queue) and self.live_tab.connected and not self.live_tab._op_lock.locked()
        self.send_queue_btn.configure(state="normal" if can_send else "disabled")

    def _selected_queue_index(self) -> int | None:
        sel = self.queue_tree.selection()
        if not sel:
            return None
        children = self.queue_tree.get_children()
        return children.index(sel[0])

    def _on_add_to_queue(self):
        try:
            payload, desc = self._build_payload()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        if len(payload) == 0:
            messagebox.showerror("Ошибка", "Пустой запрос — нечего добавлять в очередь.")
            return
        if len(payload) > MAX_MULTI_FRAME_PAYLOAD:
            messagebox.showerror(
                "Слишком длинный запрос",
                f"Payload {len(payload)} байт — превышен предел классического ISO-TP "
                f"({MAX_MULTI_FRAME_PAYLOAD} байт). Сократите данные.",
            )
            return
        self.request_queue.append({"payload": payload, "desc": desc, "danger": self._current_def()["danger"]})
        self._refresh_queue_view()

    def _on_remove_from_queue(self):
        idx = self._selected_queue_index()
        if idx is None:
            return
        del self.request_queue[idx]
        self._refresh_queue_view()

    def _on_clear_queue(self):
        if not self.request_queue:
            return
        self.request_queue.clear()
        self._refresh_queue_view()

    def _move_queue_item(self, delta: int):
        idx = self._selected_queue_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.request_queue)):
            return
        self.request_queue[idx], self.request_queue[new_idx] = self.request_queue[new_idx], self.request_queue[idx]
        self._refresh_queue_view()
        children = self.queue_tree.get_children()
        self.queue_tree.selection_set(children[new_idx])

    # ------------------------------------------------------------- отправка

    def _on_send_click(self):
        if not self.live_tab.connected:
            messagebox.showinfo("Не подключено", "Сначала нажмите «Подключиться» на вкладке «Живое подключение».")
            return
        if self.live_tab._addrs() is None:
            return
        try:
            payload, desc = self._build_payload()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        if len(payload) == 0:
            messagebox.showerror("Ошибка", "Пустой запрос — нечего отправлять.")
            return
        if len(payload) > MAX_MULTI_FRAME_PAYLOAD:
            messagebox.showerror(
                "Слишком длинный запрос",
                f"Payload {len(payload)} байт — превышен предел классического ISO-TP "
                f"({MAX_MULTI_FRAME_PAYLOAD} байт). Сократите данные.",
            )
            return

        definition = self._current_def()
        if definition["danger"]:
            if not messagebox.askyesno(
                "Подтверждение",
                f"Этот запрос может изменить состояние ЭБУ:\n\n{desc}\n\nПродолжить?",
                icon="warning",
            ):
                return

        if self.live_tab._op_lock.locked():
            messagebox.showinfo("Занято", "Дождитесь завершения текущего запроса (в т.ч. на вкладке «Живое подключение»).")
            return

        self.send_btn.configure(state="disabled")
        self._log_sent(payload, desc)

        def run():
            with self.live_tab._op_lock:
                self._send_one_blocking(payload, desc)
            self.display_queue.put(("done", None))

        threading.Thread(target=run, daemon=True).start()

    def _log_sent(self, payload: bytes, desc: str):
        if len(payload) <= 32:
            sent_hex = payload.hex(" ").upper()
        else:
            sent_hex = payload[:16].hex(" ").upper() + f"... ({len(payload)} байт всего)"
        frame_note = "" if len(payload) <= 7 else "  [многокадровый запрос: First Frame + Consecutive Frame]"
        self._log(f"→ ОТПРАВЛЕНО: {sent_hex}   ({desc}){frame_note}")

    def _send_one_blocking(self, payload: bytes, desc: str) -> bool:
        """Отправляет один запрос с повторами (до SEND_ATTEMPTS раз, как и
        кнопки на вкладке «Живое подключение») и логирует результат через
        display_queue. Вызывается из фонового потока УЖЕ ВНУТРИ
        self.live_tab._op_lock — сама лок не берёт. Возвращает True, если
        получен положительный ответ ЭБУ."""
        last_ev = None
        last_err = None
        for attempt in range(1, LiveTab.SEND_ATTEMPTS + 1):
            if attempt > 1:
                self.display_queue.put(("log", f"   повтор {attempt}/{LiveTab.SEND_ATTEMPTS}..."))
            try:
                self.live_tab._send_uds_payload(payload)
            except Exception as e:
                # Ошибка отправки (например, многокадровый запрос не
                # дождался Flow Control от ЭБУ) — как и отсутствие ответа,
                # это повод повторить попытку, а не сразу сдаваться: на
                # реальной шине бывают одиночные сбои.
                last_err = e
                last_ev = None
                self.display_queue.put(("log", f"   попытка {attempt}: ошибка отправки — {e}"))
                continue
            last_err = None
            ev = self.live_tab._wait_response(LiveTab.RESPONSE_TIMEOUT_S)
            if ev is not None:
                last_ev = ev
                break
            last_ev = ev

        if last_err is not None and last_ev is None:
            self.display_queue.put(("log", f"✗ ОШИБКА (после {LiveTab.SEND_ATTEMPTS} попыток): {last_err}"))
            return False
        if last_ev is None:
            self.display_queue.put(("log", "✗ Нет ответа (таймаут после всех попыток)"))
            return False
        if last_ev.kind == "negative":
            nrc_name = NRC_NAMES.get(last_ev.nrc, f"0x{last_ev.nrc:02X}" if last_ev.nrc is not None else "?")
            self.display_queue.put(("log", f"✗ ОТРИЦАТЕЛЬНЫЙ ОТВЕТ: {nrc_name}  (raw={last_ev.raw_hex})"))
            return False
        self.display_queue.put(("log", f"✓ ОТВЕТ: {last_ev.summary}  (raw={last_ev.raw_hex})"))
        return True

    # --------------------------------------------------- отправка очереди

    def _on_send_queue_click(self):
        if not self.live_tab.connected:
            messagebox.showinfo("Не подключено", "Сначала нажмите «Подключиться» на вкладке «Живое подключение».")
            return
        if not self.request_queue:
            return
        dangerous = [item["desc"] for item in self.request_queue if item["danger"]]
        if dangerous:
            listing = "\n".join(f"— {d}" for d in dangerous)
            if not messagebox.askyesno(
                "Подтверждение",
                f"В очереди есть запросы, способные изменить состояние ЭБУ:\n\n{listing}"
                f"\n\nВсего запросов в очереди: {len(self.request_queue)}. Отправить всю очередь подряд?",
                icon="warning",
            ):
                return
        if self.live_tab._op_lock.locked():
            messagebox.showinfo("Занято", "Дождитесь завершения текущего запроса (в т.ч. на вкладке «Живое подключение»).")
            return

        items = list(self.request_queue)  # снимок — очередь на экране можно менять, пока идёт отправка
        self.send_btn.configure(state="disabled")
        self.send_queue_btn.configure(state="disabled")
        self._log(f"▶▶ ОТПРАВКА ОЧЕРЕДИ: {len(items)} запрос(ов) подряд...")

        def run():
            ok = 0
            with self.live_tab._op_lock:
                for i, item in enumerate(items, start=1):
                    self.display_queue.put(("log", f"--- Запрос {i}/{len(items)}: {item['desc']} ---"))
                    payload = item["payload"]
                    if len(payload) <= 32:
                        sent_hex = payload.hex(" ").upper()
                    else:
                        sent_hex = payload[:16].hex(" ").upper() + f"... ({len(payload)} байт всего)"
                    self.display_queue.put(("log", f"→ ОТПРАВЛЕНО: {sent_hex}"))
                    if self._send_one_blocking(payload, item["desc"]):
                        ok += 1
                    time.sleep(0.1)  # небольшая пауза между запросами очереди
            self.display_queue.put(("log", f"▶▶ Очередь завершена: {ok}/{len(items)} успешно (положительный ответ)"))
            self.display_queue.put(("done", None))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.display_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self.send_btn.configure(state="normal" if self.live_tab.connected else "disabled")
                    self._refresh_queue_view()
        except queue.Empty:
            pass
        # Кнопки всегда должны отражать текущее состояние соединения (могли
        # подключиться/отключиться с другой вкладки).
        if not self.live_tab.connected:
            if self.send_btn["state"] != "disabled":
                self.send_btn.configure(state="disabled")
            if self.send_queue_btn["state"] != "disabled":
                self.send_queue_btn.configure(state="disabled")
        elif not self.live_tab._op_lock.locked():
            if self.send_btn["state"] == "disabled":
                self.send_btn.configure(state="normal")
            want_queue_state = "normal" if self.request_queue else "disabled"
            if self.send_queue_btn["state"] != want_queue_state:
                self.send_queue_btn.configure(state=want_queue_state)
        self.after(200, self._poll)


class MonitorTab(ttk.Frame):
    """Вкладка "Монитор шины": пассивный просмотр ВСЕГО трафика на шине —
    не только UDS-обмена, а вообще каждого кадра, который видит адаптер.

    Своего соединения не открывает (J2534-адаптер отдаёт эксклюзивный
    доступ только одному каналу — см. RequestTab) — вместо этого подписан
    на поток "сырых" кадров вкладки "Живое подключение" через
    live_tab.add_frame_listener(). Поэтому мониторинг идёт всегда, пока
    открыто то соединение, независимо от того, какая вкладка сейчас видна.

    Кнопка "Начать/Остановить запись" включает и выключает ТОЛЬКО запись в
    CSV-файл поверх уже идущего мониторинга — просмотр и подсчёт адресов
    работают всегда, запись — по требованию (как и в отдельном логгере
    can_logger_gui.py).

    Снизу — отдельная таблица уникальных адресов блоков, встреченных на
    шине, с попыткой расшифровать, что это за блок: для режима J1939 —
    по адресу-источнику (SA) и стандартной таблице SAE J1939-71
    (uds_decoder.J1939_SA_NAMES), для обычного CAN — по типовым диапазонам
    ID классического OBD-II (uds_decoder.OBD_STD_ID_NAMES). Это ОРИЕНТИР:
    конкретный производитель мог отступить от стандартной таблицы адресов."""

    MAX_LOG_LINES = 3000
    TRIM_TO_LINES = 2000

    def __init__(self, master, live_tab: LiveTab):
        super().__init__(master)
        self.live_tab = live_tab

        # Кадры кладёт сюда callback, вызываемый из потока-читателя моста
        # (см. LiveTab._on_frame/_frame_listeners) — сам разбор и запись в
        # CSV делаем здесь, в основном потоке, при опросе очереди.
        self.frame_queue: "queue.Queue" = queue.Queue()

        self.frame_count = 0
        self.recorded_count = 0
        self.recording = False
        self._csv_file = None
        self._csv_writer = None
        self._last_connected = False
        # ключ адреса ("SA 0xNN" / "ID 0xNNN") -> {"name","count","last_pgn","last_data","last_seen"}
        self._addresses: dict[str, dict] = {}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="Файл лога (CSV):").grid(row=0, column=0, sticky="w")
        self.log_path_var = tk.StringVar(value=self._default_log_path())
        self.log_path_entry = ttk.Entry(top, textvariable=self.log_path_var, width=52)
        self.log_path_entry.grid(row=0, column=1, sticky="we", padx=4)
        self.browse_btn = ttk.Button(top, text="Обзор...", command=self._browse_log_path)
        self.browse_btn.grid(row=0, column=2, sticky="w")
        top.grid_columnconfigure(1, weight=1)

        note = ("Просмотр и подсчёт адресов идут ВСЕГДА, пока открыто соединение на "
                "вкладке «Живое подключение» — отдельно подключаться здесь не нужно. "
                "Кнопка ниже включает/выключает только запись в CSV поверх уже идущего "
                "мониторинга.")
        ttk.Label(self, text=note, foreground="#555", wraplength=920, justify="left").pack(fill="x", padx=6)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=4)
        self.record_btn = tk.Button(
            ctrl, text="●  НАЧАТЬ ЗАПИСЬ", width=18, height=2, bg="#616161", fg="white",
            command=self._toggle_recording, font=("Segoe UI", 10, "bold"), state="disabled",
        )
        self.record_btn.pack(side="left", padx=(0, 12))

        self.show_live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="Показывать кадры в журнале", variable=self.show_live_var).pack(side="left")

        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=6)
        self.stats_var = tk.StringVar(value="Кадров: 0   Уникальных адресов: 0   Запись: выкл.")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side="left")

        log_header = ttk.Frame(self)
        log_header.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(log_header, text="Журнал кадров:").pack(side="left")
        ttk.Button(log_header, text="📋 Копировать журнал", command=self._copy_log).pack(side="right", padx=2)
        ttk.Button(log_header, text="Очистить журнал", command=self._clear_log).pack(side="right", padx=2)

        self.log_text = scrolledtext.ScrolledText(self, height=12, font=("Consolas", 9), wrap="none")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        _make_text_copyable(self.log_text)

        addr_frame = ttk.LabelFrame(self, text="Обнаруженные адреса блоков на шине")
        addr_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        filter_note = (
            "Фильтр по блокам: выделите одну или несколько строк в таблице ниже "
            "(Ctrl/Shift + клик), затем включите нужный переключатель. Если ничего "
            "не выделено — фильтр не действует, показывается/пишется весь трафик."
        )
        ttk.Label(addr_frame, text=filter_note, foreground="#555", wraplength=920, justify="left").pack(
            fill="x", padx=2, pady=(2, 0)
        )
        filter_ctrl = ttk.Frame(addr_frame)
        filter_ctrl.pack(fill="x", padx=2, pady=(2, 0))
        self.filter_view_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_ctrl, text="Фильтровать журнал (просмотр)", variable=self.filter_view_var
        ).pack(side="left")
        self.filter_record_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_ctrl, text="Фильтровать запись в CSV", variable=self.filter_record_var
        ).pack(side="left", padx=(16, 0))

        addr_header = ttk.Frame(addr_frame)
        addr_header.pack(fill="x", padx=2, pady=(4, 0))
        ttk.Button(addr_header, text="📋 Копировать таблицу", command=self._copy_addr_table).pack(side="right", padx=2)
        ttk.Button(addr_header, text="Очистить список", command=self._clear_addresses).pack(side="right", padx=2)
        self.addr_tree = ttk.Treeview(
            addr_frame, columns=("addr", "block", "count", "pgn", "data", "seen"),
            show="headings", height=8, selectmode="extended",
        )
        for col, text, w in (
            ("addr", "Адрес", 90),
            ("block", "Блок (расшифровка)", 300),
            ("count", "Кадров", 70),
            ("pgn", "Посл. PGN", 90),
            ("data", "Последние данные", 200),
            ("seen", "Последний раз", 130),
        ):
            self.addr_tree.heading(col, text=text)
            self.addr_tree.column(col, width=w, anchor="w")
        addr_yscroll = ttk.Scrollbar(addr_frame, orient="vertical", command=self.addr_tree.yview)
        self.addr_tree.configure(yscrollcommand=addr_yscroll.set)
        self.addr_tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        addr_yscroll.pack(side="right", fill="y", pady=(0, 2))
        _bind_treeview_copy(self.addr_tree)

        self.live_tab.add_frame_listener(self._on_raw_frame)
        self.after(150, self._poll)

    # ------------------------------------------------------------ файл лога
    def _default_log_path(self) -> str:
        logs_dir = APP_DIR / "logs"
        logs_dir.mkdir(exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(logs_dir / f"monitor_{ts}.csv")

    def _browse_log_path(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=Path(self.log_path_var.get()).name,
            initialdir=str(Path(self.log_path_var.get()).parent),
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
        )
        if path:
            self.log_path_var.set(path)

    # --------------------------------------------------------------- запись
    def _toggle_recording(self):
        if not self.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not self.live_tab.connected:
            messagebox.showinfo(
                "Не подключено",
                "Сначала нажмите «Подключиться» на вкладке «Живое подключение» — "
                "монитор работает поверх того же соединения.",
            )
            return
        log_path = Path(self.log_path_var.get())
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = open(log_path, "w", newline="", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл лога:\n{e}")
            return
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(
            ["pc_time_iso", "pc_time_ms", "dev_ts_us", "can_id_hex", "extended_id", "dlc", "data_hex",
             "rx_status_hex", "адрес", "блок", "pgn_hex"]
        )
        self.recorded_count = 0
        self.recording = True
        self.record_btn.configure(text="■  СТОП ЗАПИСИ", bg="#c62828")
        self.log_path_entry.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self._append_log_line(f"# Запись начата -> {log_path.name}")

    def _stop_recording(self, auto: bool = False):
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None
        self.recording = False
        self.record_btn.configure(
            text="●  НАЧАТЬ ЗАПИСЬ", bg="#2e7d32" if self.live_tab.connected else "#616161"
        )
        self.log_path_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")
        reason = " (соединение разорвано)" if auto else ""
        self._append_log_line(f"# Запись остановлена, записано кадров: {self.recorded_count}{reason}")

    # -------------------------------------------------------- разбор адреса
    def _decode_address(self, can_id_hex: str, ide: str) -> tuple[str, str, str]:
        """Возвращает (ключ_адреса, название_блока, pgn_hex)."""
        if ide == "1":
            try:
                addr = decode_29bit_id(can_id_hex)
            except ValueError:
                return can_id_hex, "не удалось разобрать ID", ""
            key = f"SA 0x{addr.sa:02X}"
            name = J1939_SA_NAMES.get(addr.sa, "нет в стандартной таблице J1939 (нестандартный адрес/производителя)")
            return key, name, f"0x{addr.pgn:04X}"
        try:
            v = int(can_id_hex, 16)
        except ValueError:
            return can_id_hex, "", ""
        key = f"ID 0x{v:X}"
        return key, OBD_STD_ID_NAMES.get(v, ""), ""

    # ------------------------------------------------------ приём кадров
    def _on_raw_frame(self, pc_ms: str, dev_ts_us: str, can_id_hex: str, ide: str, data_hex: str, rx_status_hex: str):
        """Вызывается из потока-читателя моста для КАЖДОГО кадра на шине —
        только кладём в очередь, никакой работы с Tk и файлами здесь
        (см. правило: фоновые потоки не трогают виджеты Tk напрямую).
        dev_ts_us — таймstamp самого адаптера (не ПК), см. _handle_frame."""
        self.frame_queue.put((pc_ms, dev_ts_us, can_id_hex, ide, data_hex, rx_status_hex))

    def _handle_frame(
        self, pc_ms: str, dev_ts_us: str, can_id_hex: str, ide: str, data_hex: str, rx_status_hex: str,
        selected_keys: frozenset[str],
    ) -> str | None:
        """Разбирает один кадр (вызывается уже в основном потоке из _poll):
        обновляет счётчики/таблицу адресов, пишет в CSV, если идёт запись,
        и возвращает строку для журнала (или None, если показ кадров
        выключен).

        В CSV пишем ОБА таймstamp'а: pc_time_iso/pc_time_ms — по часам ПК
        (удобно для сопоставления с "человеческим" временем, но на Windows
        разрешение обычно ~15 мс — несколько кадров, реально пришедших с
        разницей в пару миллисекунд, могут получить одинаковый pc_time_ms),
        и dev_ts_us — счётчик самого J2534-адаптера в моста smbridge.exe,
        куда точнее и не зависит от загрузки Python/Tk. Если в логе кажется,
        что кадр "задвоился" — надёжнее сверяться по dev_ts_us, а не по
        pc_time_ms."""
        try:
            pc_time_iso = dt.datetime.fromtimestamp(int(pc_ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except (ValueError, OSError, OverflowError):
            pc_time_iso = pc_ms

        self.frame_count += 1
        addr_key, block_name, pgn_hex = self._decode_address(can_id_hex, ide)

        rec = self._addresses.get(addr_key)
        if rec is None:
            self._addresses[addr_key] = {
                "name": block_name, "count": 1, "last_pgn": pgn_hex,
                "last_data": data_hex, "last_seen": pc_time_iso,
            }
        else:
            rec["count"] += 1
            rec["last_pgn"] = pgn_hex
            rec["last_data"] = data_hex
            rec["last_seen"] = pc_time_iso
            if block_name and not rec["name"]:
                rec["name"] = block_name

        # Фильтр по блокам: если в таблице адресов что-то выделено и
        # соответствующий переключатель включён — учитываем только кадры с
        # выделенными адресами. Пустое выделение = фильтр не действует
        # (иначе включение галочки без выбора строк молча "съедало" бы
        # весь трафик, что не очевидно и легко принять за поломку).
        is_filtered_out = bool(selected_keys) and addr_key not in selected_keys

        if self.recording and self._csv_writer is not None and not (self.filter_record_var.get() and is_filtered_out):
            self.recorded_count += 1
            dlc = len(data_hex.split()) if data_hex else 0
            self._csv_writer.writerow(
                [pc_time_iso, pc_ms, dev_ts_us, can_id_hex, ide, dlc, data_hex, rx_status_hex,
                 addr_key, block_name, pgn_hex]
            )
            if self.recorded_count % 200 == 0:
                self._csv_file.flush()

        if not self.show_live_var.get():
            return None
        if self.filter_view_var.get() and is_filtered_out:
            return None
        marker = "●" if self.recording else " "
        kind = "EXT" if ide == "1" else "STD"
        return f"{marker} {pc_time_iso}  {addr_key:<10}  {kind}  {data_hex}"

    def _poll(self):
        lines: list[str] = []
        got_any = False
        # Выделение в таблице адресов читаем ОДИН раз на пачку кадров, а не
        # на каждый кадр — при плотном трафике за один _poll() (раз в 150 мс)
        # может прийти сотни кадров, а само выделение за это время не
        # меняется (меняет его только пользователь мышью).
        selected_keys = frozenset(self.addr_tree.selection())
        try:
            while True:
                pc_ms, dev_ts_us, can_id_hex, ide, data_hex, rx_status_hex = self.frame_queue.get_nowait()
                got_any = True
                line = self._handle_frame(pc_ms, dev_ts_us, can_id_hex, ide, data_hex, rx_status_hex, selected_keys)
                if line is not None:
                    lines.append(line)
        except queue.Empty:
            pass

        if lines:
            self.log_text.insert("end", "\n".join(lines) + "\n")
            n_lines = int(self.log_text.index("end-1c").split(".")[0])
            if n_lines > self.MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{n_lines - self.TRIM_TO_LINES}.0")
            self.log_text.see("end")

        now_connected = self.live_tab.connected
        if now_connected != self._last_connected:
            if not now_connected and self.recording:
                self._stop_recording(auto=True)
            self.record_btn.configure(state="normal" if now_connected else "disabled")
            self._last_connected = now_connected

        if got_any:
            self._refresh_addr_tree()
        self.stats_var.set(
            f"Кадров: {self.frame_count}   Уникальных адресов: {len(self._addresses)}   "
            + (f"Записано: {self.recorded_count}" if self.recording else "Запись: выкл.")
        )
        self.after(150, self._poll)

    def _refresh_addr_tree(self):
        """Обновляет таблицу адресов НА МЕСТЕ (а не перестройкой с нуля):
        используем сам ключ адреса как iid строки Treeview. Раньше строки
        удалялись и вставлялись заново при каждом обновлении с
        авто-сгенерированными id — из-за этого попытка восстановить
        выделение (`if key in selected`) сравнивала адрес вида "SA 0x23" с
        id вида "I001" и никогда не срабатывала, так что выделение слетало
        на каждый кадр. Обновление на месте с iid=ключ_адреса решает это
        само по себе — Treeview сохраняет выделение существующих строк
        автоматически, что и нужно для фильтра по адресам ниже."""
        for index, key in enumerate(sorted(self._addresses.keys())):
            rec = self._addresses[key]
            values = (key, rec["name"], rec["count"], rec["last_pgn"], rec["last_data"], rec["last_seen"])
            if self.addr_tree.exists(key):
                self.addr_tree.item(key, values=values)
            else:
                self.addr_tree.insert("", "end", iid=key, values=values)
            self.addr_tree.move(key, "", index)

    def _clear_addresses(self):
        self._addresses.clear()
        self.addr_tree.delete(*self.addr_tree.get_children())

    # ------------------------------------------------------------- журнал
    def _append_log_line(self, line: str):
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _copy_log(self):
        _copy_text_to_clipboard(self.log_text, self.log_text.get("1.0", "end-1c"))

    def _copy_addr_table(self):
        EcuTables._copy_tree_all(self.addr_tree)

    def on_close(self):
        if self.recording:
            self._stop_recording()
        self.live_tab.remove_frame_listener(self._on_raw_frame)


class AllDtcTab(ttk.Frame):
    """Вкладка "Ошибки всех блоков" — опрашивает ReadDTCInformation (0x19)
    ПО ОЧЕРЕДИ у всех адресов (J1939 SA), которые видны на шине, а не только
    у одного адреса, указанного в поле "Адрес ЭБУ" на вкладке "Живое
    подключение".

    Список блоков пополняется сам, пассивно — как у "Монитор шины": вкладка
    подписывается на поток "сырых" кадров live_tab и запоминает все
    встреченные SA (плюс расшифровку по стандартной таблице SAE J1939-71).
    Адрес можно добавить и вручную — если блок ещё ничего не передавал на
    шину сам, но его адрес известен.

    Сам опрос — обычный директный (point-to-point) UDS-запрос, как и на
    "Живом подключении", просто по очереди для каждого адреса: вкладка на
    время запроса переключает live_tab.ecu_da_var на нужный адрес (все
    операции в программе и так сериализованы через live_tab._op_lock, то
    есть одновременно с этим ничего больше не шлётся) и возвращает поле
    обратно по завершении опроса — даже если он был прерван."""

    MAX_LOG_LINES = 3000
    TRIM_TO_LINES = 2000

    def __init__(self, master, live_tab: LiveTab):
        super().__init__(master)
        self.live_tab = live_tab
        self.display_queue: "queue.Queue[tuple]" = queue.Queue()
        self.frame_queue: "queue.Queue[tuple]" = queue.Queue()
        # sa(int) -> {"name": str, "seen": int, "source": "шина"|"вручную",
        #             "status": str, "dtcs": list, "last_poll": str}
        self._blocks: dict[int, dict] = {}
        self._abort_event = threading.Event()
        self._busy = False

        note = (
            "Список ниже пополняется САМ — как только на шине встречается новый адрес (SA), "
            "он появляется в таблице (нужно активное подключение на вкладке «Живое подключение», "
            "неважно, какая вкладка сейчас открыта). Кнопки ниже опрашивают DTC (0x19, "
            "reportDTCByStatusMask) у выбранных адресов по очереди — так же, как кнопка "
            "«Прочитать DTC» на «Живом подключении», только сразу для всех блоков. В результате и "
            "журнале: ● — DTC активна СЕЙЧАС (testFailed), ○ — неактивна (историческая); тип "
            "неисправности (по 3-му байту кода) — предположительная расшифровка из присланного "
            "справочника, полный список компонентов — на вкладке «Справочник Volvo». Кнопки "
            "«Очистить DTC» необратимо стирают ошибки на выбранных блоках (0x14, "
            "ClearDiagnosticInformation) — только после подтверждения."
        )
        ttk.Label(self, text=note, foreground="#555", wraplength=920, justify="left").pack(fill="x", padx=6, pady=(6, 4))

        manual_frame = ttk.Frame(self)
        manual_frame.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(manual_frame, text="Добавить адрес вручную (hex SA):").pack(side="left")
        self.manual_addr_var = tk.StringVar(value="")
        ttk.Entry(manual_frame, textvariable=self.manual_addr_var, width=8).pack(side="left", padx=4)
        tk.Button(manual_frame, text="➕ Добавить", command=self._on_add_manual).pack(side="left")

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        columns = ("addr", "name", "seen", "source", "result", "last_poll")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for col, text, w in (
            ("addr", "Адрес", 70),
            ("name", "Блок (расшифровка)", 300),
            ("seen", "Кадров на шине", 90),
            ("source", "Источник", 80),
            ("result", "Результат опроса DTC", 260),
            ("last_poll", "Время опроса", 90),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        _bind_treeview_copy(self.tree)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=6, pady=(0, 4))
        self.scan_all_btn = tk.Button(
            btns, text="🔍 Опросить ВСЕ обнаруженные", bg="#1565c0", fg="white",
            font=("Segoe UI", 9, "bold"), state="disabled", command=lambda: self._on_scan_click(only_selected=False),
        )
        self.scan_all_btn.pack(side="left")
        self.scan_selected_btn = tk.Button(
            btns, text="▶ Опросить выделенные", state="disabled", command=lambda: self._on_scan_click(only_selected=True),
        )
        self.scan_selected_btn.pack(side="left", padx=8)
        self.abort_btn = tk.Button(btns, text="⏹ Остановить", width=14, state="disabled", command=self._on_abort_click)
        self.abort_btn.pack(side="left", padx=8)
        tk.Button(btns, text="🗑 Забыть все адреса", command=self._on_forget_all).pack(side="right")

        clear_btns = ttk.Frame(self)
        clear_btns.pack(fill="x", padx=6, pady=(0, 4))
        self.clear_selected_btn = tk.Button(
            clear_btns, text="🧹 Очистить DTC у выделенных", bg="#b71c1c", fg="white",
            state="disabled", command=lambda: self._on_clear_click(only_selected=True),
        )
        self.clear_selected_btn.pack(side="left")
        self.clear_all_btn = tk.Button(
            clear_btns, text="🧹 Очистить DTC у ВСЕХ обнаруженных", bg="#b71c1c", fg="white",
            state="disabled", command=lambda: self._on_clear_click(only_selected=False),
        )
        self.clear_all_btn.pack(side="left", padx=8)

        log_header = ttk.Frame(self)
        log_header.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(log_header, text="Журнал опроса:").pack(side="left")
        tk.Button(log_header, text="📋 Копировать журнал", command=self._copy_log).pack(side="right")
        tk.Button(log_header, text="Очистить журнал", command=self._clear_log).pack(side="right", padx=(0, 6))

        self.log_text = scrolledtext.ScrolledText(self, height=10, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        _make_text_copyable(self.log_text)

        self.live_tab.add_frame_listener(self._on_raw_frame)
        self.after(200, self._poll)

    # ------------------------------------------------------------- журнал
    def _log(self, line: str):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {line}\n")
        self.log_text.see("end")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - self.TRIM_TO_LINES}.0")

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _copy_log(self):
        _copy_text_to_clipboard(self.log_text, self.log_text.get("1.0", "end-1c"))

    # ------------------------------------------------------------ приём кадров
    def _on_raw_frame(self, pc_ms: str, dev_ts_us: str, can_id_hex: str, ide: str, data_hex: str, rx_status_hex: str):
        """Вызывается из потока-читателя моста для каждого кадра — только
        кладём в очередь, разбор и обновление таблицы делаем в основном
        потоке (см. _poll), как и в MonitorTab."""
        self.frame_queue.put((can_id_hex, ide))

    def _note_seen(self, sa: int):
        rec = self._blocks.get(sa)
        if rec is None:
            self._blocks[sa] = {
                "name": J1939_SA_NAMES.get(sa, "нет в стандартной таблице J1939"),
                "seen": 1, "source": "шина", "status": "— не опрошено —",
                "dtcs": [], "last_poll": "",
            }
        else:
            rec["seen"] += 1

    def _on_add_manual(self):
        text = self.manual_addr_var.get().strip().replace("0x", "").replace("0X", "")
        if not text:
            return
        try:
            sa = int(text, 16) & 0xFF
        except ValueError:
            messagebox.showerror("Ошибка", f"«{self.manual_addr_var.get()}» — это не hex-адрес (например, 23).")
            return
        if sa not in self._blocks:
            self._blocks[sa] = {
                "name": J1939_SA_NAMES.get(sa, "нет в стандартной таблице J1939"),
                "seen": 0, "source": "вручную", "status": "— не опрошено —",
                "dtcs": [], "last_poll": "",
            }
            self._refresh_tree()
        self.manual_addr_var.set("")

    def _on_forget_all(self):
        if not self._blocks:
            return
        if messagebox.askyesno("Подтверждение", f"Забыть все {len(self._blocks)} обнаруженных адресов из таблицы?"):
            self._blocks.clear()
            self._refresh_tree()

    # ------------------------------------------------------------ таблица
    def _refresh_tree(self):
        keys = sorted(self._blocks.keys())
        valid_iids = {f"{sa:02X}" for sa in keys}
        # Строки адресов, которых больше нет в self._blocks (например, после
        # "Забыть все адреса"), нужно убрать явно — просто не трогать их
        # недостаточно: вставка/обновление ниже никогда не удаляет строки.
        for iid in self.tree.get_children():
            if iid not in valid_iids:
                self.tree.delete(iid)
        for index, sa in enumerate(keys):
            rec = self._blocks[sa]
            iid = f"{sa:02X}"
            values = (
                f"0x{sa:02X}", rec["name"], rec["seen"], rec["source"], rec["status"], rec["last_poll"],
            )
            if self.tree.exists(iid):
                self.tree.item(iid, values=values)
            else:
                self.tree.insert("", "end", iid=iid, values=values)
            self.tree.move(iid, "", index)

    # ------------------------------------------------------------ опрос
    def _check_ready(self) -> bool:
        if not self.live_tab.connected:
            messagebox.showinfo("Не подключено", "Сначала нажмите «Подключиться» на вкладке «Живое подключение».")
            return False
        if self.live_tab._addrs() is None:
            return False
        if self.live_tab._op_lock.locked():
            messagebox.showinfo("Занято", "Дождитесь завершения текущей операции (в т.ч. на других вкладках).")
            return False
        return True

    def _on_scan_click(self, only_selected: bool):
        if not self._check_ready():
            return
        if only_selected:
            addrs = sorted(int(iid, 16) for iid in self.tree.selection())
        else:
            addrs = sorted(self._blocks.keys())
        if not addrs:
            messagebox.showinfo(
                "Нет адресов",
                "Пока не обнаружено ни одного блока (или ничего не выделено). "
                "Подождите немного при активном подключении — список пополняется автоматически, "
                "либо добавьте адрес вручную.",
            )
            return
        self._start_scan(addrs)

    def _start_scan(self, addrs: list[int]):
        self._abort_event.clear()
        self._busy = True
        self.scan_all_btn.configure(state="disabled")
        self.scan_selected_btn.configure(state="disabled")
        self.abort_btn.configure(state="normal")
        self._clear_log()
        self._log(f"=== Опрос {len(addrs)} блок(ов): {', '.join(f'0x{a:02X}' for a in addrs)} ===")

        def run():
            orig_da = self.live_tab.ecu_da_var.get()
            try:
                with self.live_tab._op_lock:
                    for i, addr in enumerate(addrs, start=1):
                        if self._abort_event.is_set():
                            self.display_queue.put(("log", "⏹ Остановлено пользователем"))
                            break
                        self.live_tab.ecu_da_var.set(f"{addr:02X}")
                        name = J1939_SA_NAMES.get(addr, "нет в стандартной таблице J1939")
                        self.display_queue.put(("log", f"--- [{i}/{len(addrs)}] 0x{addr:02X} ({name}) ---"))
                        try:
                            ev = self.live_tab._send_and_wait(req_read_dtc_by_status_mask(0xFF), f"DTC 0x{addr:02X}")
                        except Exception as e:
                            self.display_queue.put(("log", f"  ошибка отправки: {e}"))
                            ev = None
                        result = self._interpret(addr, ev)
                        self.display_queue.put(("result", (addr, result)))
                        time.sleep(0.12)
            finally:
                self.live_tab.ecu_da_var.set(orig_da)
                self.display_queue.put(("scan_done", None))

        threading.Thread(target=run, daemon=True).start()

    def _interpret(self, addr: int, ev) -> dict:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        if ev is None:
            self.display_queue.put(("log", "  нет ответа (таймаут)"))
            return {"status": "нет ответа (таймаут)", "dtcs": [], "last_poll": ts}
        if ev.kind == "negative":
            nrc_name = NRC_NAMES.get(ev.nrc, f"0x{ev.nrc:02X}" if ev.nrc is not None else "?")
            self.display_queue.put(("log", f"  отрицательный ответ: {nrc_name}"))
            return {"status": f"отрицательный ответ: {nrc_name}", "dtcs": [], "last_poll": ts}
        dtcs = ev.dtcs or []
        if not dtcs:
            self.display_queue.put(("log", "  ошибок нет"))
            return {"status": "ошибок нет", "dtcs": [], "last_poll": ts}
        active_count = 0
        preview_parts = []
        for code3, status, code, flags in dtcs:
            active = is_dtc_active(status)
            if active:
                active_count += 1
            active_txt = "АКТИВНА" if active else "неактивна"
            ftype = describe_failure_type(code3)
            ftype_txt = f"  — {ftype}" if ftype else ""
            self.display_queue.put(("log", f"  {code}  status=0x{status:02X}  {active_txt}  {', '.join(flags)}{ftype_txt}"))
            marker = "●" if active else "○"
            preview_parts.append(f"{marker}{code}")
        preview = ", ".join(preview_parts[:4])
        if len(dtcs) > 4:
            preview += ", ..."
        return {
            "status": f"{len(dtcs)} DTC ({active_count} актив.): {preview}",
            "dtcs": dtcs, "last_poll": ts,
        }

    def _on_abort_click(self):
        self._abort_event.set()

    # ------------------------------------------------------------ очистка DTC
    def _on_clear_click(self, only_selected: bool):
        if not self._check_ready():
            return
        if only_selected:
            addrs = sorted(int(iid, 16) for iid in self.tree.selection())
        else:
            addrs = sorted(self._blocks.keys())
        if not addrs:
            messagebox.showinfo(
                "Нет адресов",
                "Не выбрано ни одного адреса для очистки DTC (выделите строки в таблице, "
                "либо используйте «у ВСЕХ обнаруженных»).",
            )
            return
        addr_list = ", ".join(f"0x{a:02X}" for a in addrs)
        if not messagebox.askyesno(
            "Подтверждение",
            f"Это НЕОБРАТИМО сотрёт ВСЕ коды ошибок (DTC) на {len(addrs)} блок(ах): {addr_list}.\n\n"
            "Продолжить?",
            icon="warning",
        ):
            return
        self._start_clear(addrs)

    def _start_clear(self, addrs: list[int]):
        self._abort_event.clear()
        self._busy = True
        for btn in (self.scan_all_btn, self.scan_selected_btn, self.clear_selected_btn, self.clear_all_btn):
            btn.configure(state="disabled")
        self.abort_btn.configure(state="normal")
        self._clear_log()
        self._log(f"=== Очистка DTC у {len(addrs)} блок(ах): {', '.join(f'0x{a:02X}' for a in addrs)} ===")

        def run():
            orig_da = self.live_tab.ecu_da_var.get()
            try:
                with self.live_tab._op_lock:
                    for i, addr in enumerate(addrs, start=1):
                        if self._abort_event.is_set():
                            self.display_queue.put(("log", "⏹ Остановлено пользователем"))
                            break
                        self.live_tab.ecu_da_var.set(f"{addr:02X}")
                        name = J1939_SA_NAMES.get(addr, "нет в стандартной таблице J1939")
                        self.display_queue.put(("log", f"--- [{i}/{len(addrs)}] очистка 0x{addr:02X} ({name}) ---"))
                        try:
                            ev = self.live_tab._send_and_wait(req_clear_dtc(), f"Очистка DTC 0x{addr:02X}")
                        except Exception as e:
                            self.display_queue.put(("log", f"  ошибка отправки: {e}"))
                            ev = None
                        result = self._interpret_clear(addr, ev)
                        self.display_queue.put(("result", (addr, result)))
                        time.sleep(0.12)
            finally:
                self.live_tab.ecu_da_var.set(orig_da)
                self.display_queue.put(("scan_done", None))

        threading.Thread(target=run, daemon=True).start()

    def _interpret_clear(self, addr: int, ev) -> dict:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        if ev is None:
            self.display_queue.put(("log", "  нет ответа (таймаут)"))
            return {"status": "очистка: нет ответа (таймаут)", "last_poll": ts}
        if ev.kind == "negative":
            nrc_name = NRC_NAMES.get(ev.nrc, f"0x{ev.nrc:02X}" if ev.nrc is not None else "?")
            self.display_queue.put(("log", f"  очистка не выполнена: {nrc_name}"))
            return {"status": f"очистка не выполнена: {nrc_name}", "last_poll": ts}
        self.display_queue.put(("log", "  DTC успешно очищены"))
        return {"status": "DTC очищены", "dtcs": [], "last_poll": ts}

    # ------------------------------------------------------------- опрос очереди
    def _poll(self):
        new_frames = False
        try:
            while True:
                can_id_hex, ide = self.frame_queue.get_nowait()
                if ide == "1":
                    try:
                        addr = decode_29bit_id(can_id_hex)
                    except ValueError:
                        continue
                    self._note_seen(addr.sa)
                    new_frames = True
        except queue.Empty:
            pass

        try:
            while True:
                kind, payload = self.display_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "result":
                    addr, result = payload
                    rec = self._blocks.get(addr)
                    if rec is not None:
                        rec.update(result)
                    new_frames = True
                elif kind == "scan_done":
                    self._busy = False
                    self.abort_btn.configure(state="disabled")
        except queue.Empty:
            pass

        if new_frames:
            self._refresh_tree()

        if not self._busy:
            can_op = self.live_tab.connected and not self.live_tab._op_lock.locked()
            want_state = "normal" if can_op else "disabled"
            for btn in (self.scan_all_btn, self.scan_selected_btn, self.clear_selected_btn, self.clear_all_btn):
                if btn["state"] != want_state:
                    btn.configure(state=want_state)

        self.after(200, self._poll)

    def on_close(self):
        self.live_tab.remove_frame_listener(self._on_raw_frame)


class ReferenceTab(ttk.Frame):
    """Вкладка "Справочник Volvo" — расшифровка кодов ошибок из присланного
    пользователем файла Коды_ошибок_RU.xlsx (см. dtc_reference.py).

    Таблица "Коды компонентов" (DiagnosticObjectId, напр. "D1A0A") — это
    ВНУТРЕННИЕ последовательные ID Volvo, они НЕ вычисляются из байтов DTC,
    реально приходящих с шины (нужна ещё отдельная таблица-связка конкретной
    прошивки ЭБУ с этими ID, которой в присланном файле нет). Поэтому здесь
    — только ручной поиск по названию/ID, эта таблица НЕ привязана
    автоматически к живым DTC на других вкладках.

    Таблица "Типы неисправностей" (0-163), наоборот, расшифровывается
    АВТОМАТИЧЕСКИ на других вкладках (по 3-му байту кода DTC) — здесь просто
    показан полный список для справки."""

    def __init__(self, master):
        super().__init__(master)

        note = (
            "Расшифровка кодов ошибок Volvo из присланного файла Коды_ошибок_RU.xlsx. "
            "«Коды компонентов» — это внутренние ID Volvo, без отдельной таблицы-связки с "
            "конкретной прошивкой их нельзя автоматически сопоставить с живыми DTC на шине — "
            "здесь только ручной поиск. «Типы неисправностей» (3-й байт кода DTC), наоборот, "
            "расшифровываются автоматически на других вкладках — здесь просто полный список."
        )
        ttk.Label(self, text=note, foreground="#555", wraplength=920, justify="left").pack(fill="x", padx=6, pady=(6, 4))

        comp_frame = ttk.LabelFrame(self, text=f"Коды компонентов Volvo (ручной поиск, всего {len(COMPONENT_RU)})")
        comp_frame.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        search_row = ttk.Frame(comp_frame)
        search_row.pack(fill="x", padx=4, pady=4)
        ttk.Label(search_row, text="Поиск (ID или название):").pack(side="left")
        self.search_var = tk.StringVar(value="")
        entry = ttk.Entry(search_row, textvariable=self.search_var, width=40)
        entry.pack(side="left", padx=4)
        entry.bind("<KeyRelease>", lambda _e: self._on_search())
        ttk.Button(search_row, text="📋 Копировать таблицу", command=lambda: EcuTables._copy_tree_all(self.comp_tree)).pack(side="right")

        self.comp_tree = ttk.Treeview(comp_frame, columns=("id", "name"), show="headings", height=14)
        self.comp_tree.heading("id", text="ID компонента")
        self.comp_tree.column("id", width=120, anchor="w")
        self.comp_tree.heading("name", text="Название")
        self.comp_tree.column("name", width=560, anchor="w")
        yscroll1 = ttk.Scrollbar(comp_frame, orient="vertical", command=self.comp_tree.yview)
        self.comp_tree.configure(yscrollcommand=yscroll1.set)
        self.comp_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=(0, 4))
        yscroll1.pack(side="right", fill="y", pady=(0, 4))
        _bind_treeview_copy(self.comp_tree)

        ftype_frame = ttk.LabelFrame(
            self, text=f"Типы неисправностей — расшифровка 3-го байта DTC, предположительно (всего {len(FAILURE_TYPE_RU)})"
        )
        ftype_frame.pack(fill="both", expand=False, padx=6, pady=(0, 6))
        self.ftype_tree = ttk.Treeview(ftype_frame, columns=("val", "name"), show="headings", height=6)
        self.ftype_tree.heading("val", text="Значение байта")
        self.ftype_tree.column("val", width=100, anchor="w")
        self.ftype_tree.heading("name", text="Описание")
        self.ftype_tree.column("name", width=580, anchor="w")
        yscroll2 = ttk.Scrollbar(ftype_frame, orient="vertical", command=self.ftype_tree.yview)
        self.ftype_tree.configure(yscrollcommand=yscroll2.set)
        self.ftype_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=(0, 4))
        yscroll2.pack(side="right", fill="y", pady=(0, 4))
        _bind_treeview_copy(self.ftype_tree)

        self._on_search()
        for val, name in sorted(FAILURE_TYPE_RU.items()):
            self.ftype_tree.insert("", "end", values=(f"0x{val:02X}", name))

    def _on_search(self):
        self.comp_tree.delete(*self.comp_tree.get_children())
        for oid, name in search_components(self.search_var.get()):
            self.comp_tree.insert("", "end", values=(oid, name))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scanmatik: иденты и коды ошибок (UDS)")
        self.geometry("980x700")
        self.minsize(820, 560)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        self.live_tab = LiveTab(nb)
        self.request_tab = RequestTab(nb, self.live_tab)
        self.monitor_tab = MonitorTab(nb, self.live_tab)
        self.all_dtc_tab = AllDtcTab(nb, self.live_tab)
        self.reference_tab = ReferenceTab(nb)
        nb.add(self.live_tab, text="Живое подключение")
        nb.add(self.request_tab, text="Свои UDS-запросы")
        nb.add(self.monitor_tab, text="Монитор шины")
        nb.add(self.all_dtc_tab, text="Ошибки всех блоков")
        nb.add(self.reference_tab, text="Справочник Volvo")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.monitor_tab.on_close()
        self.all_dtc_tab.on_close()
        self.live_tab.on_close()
        self.destroy()


def _run_headless_analyze(path: str) -> int:
    """Режим без GUI: разобрать CSV и напечатать отчёт в stdout.
    Используется для автотестов/проверки на машине без дисплея."""
    from dtc_report import analyze_csv_file, format_report

    # Консоль Windows по умолчанию не UTF-8 (cp866/cp1251) — без этого
    # кириллица в выводе превращается в "кракозябры" (см. ту же проблему,
    # решённую для моста smbridge.exe). На вывод в файл/GUI это не влияет.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    analyzer, total_rows, total_events = analyze_csv_file(path)
    print(f"Обработано строк: {total_rows}, UDS-событий: {total_events}\n")
    print(format_report(analyzer, title=f"Отчёт по логу: {Path(path).name}"))
    return 0


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--analyze":
        sys.exit(_run_headless_analyze(sys.argv[2]))

    if os.name != "nt":
        print("Этот инструмент рассчитан на Windows (J2534/Scanmatik) для живого режима; "
              "офлайн-разбор CSV работает на любой ОС.")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
