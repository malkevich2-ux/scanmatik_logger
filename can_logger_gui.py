#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Логгер CAN/J1939 для прибора Scanmatik (SM2/SM3), подключённого по USB.

Работает через маленький 32-битный "мост" smbridge.exe (см. папку bridge/),
который умеет говорить с J2534-драйвером Scanmatik напрямую. Сам этот GUI
может быть хоть 64-битным Python — с мостом он общается через обычный
subprocess (stdin/stdout), поэтому битность самого Python не важна.

Возможности:
  - Кнопка "Подключиться/Отключиться" — открывает канал и сразу начинает
    слушать шину (живой просмотр кадров), независимо от записи.
  - Отдельная кнопка "Начать запись/Остановить запись" — включает запись в
    CSV, не разрывая соединение. Так можно постоянно видеть шину и включать
    запись только когда нужно.
  - Выбор J2534-устройства (автопоиск в реестре Windows).
  - Переключатель режима шины: CAN (обычный J2534/OBD) или SAE J1939.
  - Выбор скорости CAN.

ВАЖНО: инструмент только СЛУШАЕТ шину (логирование), передачу кадров
(PassThruWriteMsgs) мост не реализует.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import winreg  # только Windows
except ImportError:
    winreg = None

# Когда программа собрана PyInstaller-ом в exe, __file__ указывает во
# временную распаковку — реальное расположение exe тогда в sys.executable.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

BRIDGE_CANDIDATES = [
    APP_DIR / "bridge" / "publish" / "smbridge.exe",
    APP_DIR / "smbridge.exe",
]

CAN_BAUD_OPTIONS = ["125000", "250000", "500000", "1000000"]
J1939_BAUD_OPTIONS = ["250000", "500000"]


def find_bridge_exe() -> Path | None:
    for p in BRIDGE_CANDIDATES:
        if p.exists():
            return p
    return None


def find_j2534_devices() -> list[tuple[str, str]]:
    """Возвращает список (человекочитаемое_имя, путь_к_dll) из реестра Windows.

    Ищет и в 32-битной, и в "родной" ветке PassThruSupport.04.04 — так
    находятся все установленные J2534-драйверы, не только Scanmatik.
    """
    devices: list[tuple[str, str]] = []
    if winreg is None:
        return devices

    roots = [
        r"SOFTWARE\WOW6432Node\PassThruSupport.04.04",
        r"SOFTWARE\PassThruSupport.04.04",
    ]
    seen_paths = set()
    for root in roots:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    sub = winreg.OpenKey(key, sub_name)
                    vendor = _reg_val(sub, "Vendor")
                    name = _reg_val(sub, "Name") or sub_name
                    dll = _reg_val(sub, "FunctionLibrary")
                    sub.Close()
                except OSError:
                    continue
                if not dll or dll in seen_paths:
                    continue
                seen_paths.add(dll)
                label = f"{vendor} — {name}" if vendor else name
                devices.append((label, dll))
        finally:
            key.Close()

    # Scanmatik — в начало списка, чтобы был выбран по умолчанию.
    devices.sort(key=lambda d: (0 if "scanmatik" in d[0].lower() else 1, d[0]))
    return devices


def _reg_val(key, name: str) -> str | None:
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return str(val)
    except OSError:
        return None


class BridgeProcess:
    """Обёртка над подпроцессом smbridge.exe: отправка команд, чтение строк.

    Мост печатает в UTF-8, поэтому здесь ЯВНО задаём encoding="utf-8" —
    иначе на русской Windows Python попытается декодировать вывод в cp1251/
    cp866 (локаль консоли) и кириллица в сообщениях об ошибках побьётся.
    """

    def __init__(self, exe_path: Path, on_frame, on_info, on_error):
        self.exe_path = exe_path
        self.on_frame = on_frame
        self.on_info = on_info
        self.on_error = on_error
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = False

    def start_process(self):
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [str(self.exe_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._stop_reader = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            if self._stop_reader:
                break
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            if line.startswith("FRAME "):
                self.on_frame(line)
            elif line.startswith("ERR "):
                self.on_error(line)
            else:
                self.on_info(line)

    def send(self, line: str):
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Мост не запущен")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def terminate(self):
        self._stop_reader = True
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scanmatik CAN/J1939 логгер")
        self.geometry("900x620")
        self.minsize(780, 500)

        self.bridge: BridgeProcess | None = None
        self.connected = False
        self.recording = False

        self.csv_file = None
        self.csv_writer = None
        self.frame_count = 0          # всего кадров с момента подключения
        self.recorded_count = 0       # кадров записано в текущий CSV
        self.connect_time: float | None = None
        self.record_time: float | None = None

        self.display_queue: "queue.Queue[str]" = queue.Queue()
        self.recent_lines: deque[str] = deque(maxlen=500)

        self.devices = find_j2534_devices()
        self.bridge_exe = find_bridge_exe()

        self._build_ui()
        self._poll_display_queue()
        self._tick_stats()

        if self.bridge_exe is None:
            self._log_status(
                "Не найден smbridge.exe. Соберите мост: смотрите README.md "
                "(build_bridge.ps1) в этой же папке.",
                error=True,
            )
        if not self.devices:
            self._log_status(
                "В реестре Windows не найдено ни одного J2534-устройства. "
                "Проверьте, что драйвер Scanmatik установлен.",
                error=True,
            )

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Устройство J2534:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            top, textvariable=self.device_var, width=48, state="readonly"
        )
        self.device_combo["values"] = [d[0] for d in self.devices] or ["(не найдено)"]
        if self.devices:
            self.device_combo.current(0)
        self.device_combo.grid(row=0, column=1, columnspan=3, sticky="we", **pad)

        self.refresh_btn = ttk.Button(top, text="Обновить", command=self._refresh_devices)
        self.refresh_btn.grid(row=0, column=4, sticky="e")

        ttk.Label(top, text="Режим шины:").grid(row=1, column=0, sticky="w")
        self.mode_var = tk.StringVar(value="CAN")
        mode_frame = ttk.Frame(top)
        mode_frame.grid(row=1, column=1, sticky="w")
        self.mode_can_rb = ttk.Radiobutton(
            mode_frame, text="CAN (J2534)", variable=self.mode_var, value="CAN",
            command=self._on_mode_change,
        )
        self.mode_can_rb.pack(side="left")
        self.mode_j1939_rb = ttk.Radiobutton(
            mode_frame, text="SAE J1939", variable=self.mode_var, value="J1939",
            command=self._on_mode_change,
        )
        self.mode_j1939_rb.pack(side="left", padx=(12, 0))

        ttk.Label(top, text="Скорость, бит/с:").grid(row=1, column=2, sticky="e")
        self.baud_var = tk.StringVar(value=CAN_BAUD_OPTIONS[2])  # 500000
        self.baud_combo = ttk.Combobox(
            top, textvariable=self.baud_var, width=12, values=CAN_BAUD_OPTIONS
        )
        self.baud_combo.grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(top, text="Файл лога (CSV):").grid(row=2, column=0, sticky="w")
        self.log_path_var = tk.StringVar(value=self._default_log_path())
        self.log_path_entry = ttk.Entry(top, textvariable=self.log_path_var, width=48)
        self.log_path_entry.grid(row=2, column=1, columnspan=3, sticky="we", **pad)
        self.browse_btn = ttk.Button(top, text="Обзор...", command=self._browse_log_path)
        self.browse_btn.grid(row=2, column=4, sticky="e")

        for c in range(5):
            top.grid_columnconfigure(c, weight=1 if c in (1,) else 0)

        # Кнопки управления
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)

        self.connect_btn = tk.Button(
            ctrl, text="🔌  ПОДКЛЮЧИТЬСЯ", width=20, height=2, bg="#1565c0", fg="white",
            command=self._toggle_connection, font=("Segoe UI", 10, "bold"),
        )
        self.connect_btn.pack(side="left", padx=6)

        self.record_btn = tk.Button(
            ctrl, text="●  ЗАПИСЬ", width=16, height=2, bg="#616161", fg="white",
            command=self._toggle_recording, font=("Segoe UI", 10, "bold"),
            state="disabled",
        )
        self.record_btn.pack(side="left", padx=6)

        self.show_live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="Показывать кадры в окне", variable=self.show_live_var
        ).pack(side="left", padx=12)

        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Не подключено")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="#555").pack(
            side="left"
        )

        self.stats_var = tk.StringVar(value="Кадров получено: 0   Записано: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side="right")

        # Текстовое окно с логом
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, **pad)

        self.text = tk.Text(
            text_frame, wrap="none", font=("Consolas", 9), state="disabled",
            bg="#0b0b0b", fg="#d7d7d7", insertbackground="#d7d7d7",
        )
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="we")
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        self.text.tag_config("err", foreground="#ff6b6b")
        self.text.tag_config("info", foreground="#6ba8ff")
        self.text.tag_config("rec", foreground="#ffb74d")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _default_log_path(self) -> str:
        logs_dir = APP_DIR / "logs"
        logs_dir.mkdir(exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(logs_dir / f"can_log_{ts}.csv")

    def _on_mode_change(self):
        if self.mode_var.get() == "J1939":
            self.baud_combo["values"] = J1939_BAUD_OPTIONS
            self.baud_var.set(J1939_BAUD_OPTIONS[0])  # 250000 — стандарт для J1939
        else:
            self.baud_combo["values"] = CAN_BAUD_OPTIONS
            self.baud_var.set(CAN_BAUD_OPTIONS[2])  # 500000

    def _refresh_devices(self):
        self.devices = find_j2534_devices()
        self.device_combo["values"] = [d[0] for d in self.devices] or ["(не найдено)"]
        if self.devices:
            self.device_combo.current(0)

    def _browse_log_path(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=Path(self.log_path_var.get()).name,
            initialdir=str(Path(self.log_path_var.get()).parent),
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
        )
        if path:
            self.log_path_var.set(path)

    def _selected_dll_path(self) -> str | None:
        idx = self.device_combo.current()
        if idx < 0 or idx >= len(self.devices):
            return None
        return self.devices[idx][1]

    # ------------------------------------------------------- подключение
    # "Подключиться" открывает канал и сразу начинает читать шину (живой
    # просмотр всегда доступен после подключения). Запись в CSV — отдельная,
    # независимая кнопка, включаемая/выключаемая поверх активного соединения.

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

        self.bridge = BridgeProcess(
            self.bridge_exe, on_frame=self._on_frame, on_info=self._on_info, on_error=self._on_bridge_error
        )
        try:
            self.bridge.start_process()
            self.bridge.send(f"OPEN {dll_path}")
            self.bridge.send(f"CONNECT {self.mode_var.get()} {baud}")
            self.bridge.send("START")
        except Exception as e:
            self._log_status(f"Не удалось запустить мост: {e}", error=True)
            self.bridge = None
            return

        self._mode_at_connect = self.mode_var.get()
        self._baud_at_connect = baud
        self.frame_count = 0
        self.connect_time = time.time()
        self.connected = True

        self.connect_btn.configure(text="⏻  ОТКЛЮЧИТЬСЯ", bg="#c62828")
        self.record_btn.configure(state="normal")
        self.status_var.set(f"Подключено: {self.mode_var.get()} @ {baud} бод — слушаю шину")
        self._set_connect_controls_enabled(False)

    def _disconnect(self):
        if self.recording:
            self._stop_recording()

        if self.bridge is not None:
            try:
                self.bridge.send("STOP")
                self.bridge.send("CLOSE")
            except Exception:
                pass
            self.bridge.terminate()
            self.bridge = None

        self.connected = False
        self.connect_time = None

        self.connect_btn.configure(text="🔌  ПОДКЛЮЧИТЬСЯ", bg="#1565c0")
        self.record_btn.configure(state="disabled")
        self.status_var.set("Не подключено")
        self._set_connect_controls_enabled(True)

    def _set_connect_controls_enabled(self, enabled: bool):
        state = "readonly" if enabled else "disabled"
        self.device_combo.configure(state=state)
        self.baud_combo.configure(state="normal" if enabled else "disabled")
        rb_state = "normal" if enabled else "disabled"
        self.mode_can_rb.configure(state=rb_state)
        self.mode_j1939_rb.configure(state=rb_state)
        self.refresh_btn.configure(state=rb_state)

    # ------------------------------------------------------------- запись
    # Включает/выключает только запись в CSV. Соединение и живой просмотр
    # при этом не трогаются — можно постоянно смотреть шину и записывать
    # только нужные куски.

    def _toggle_recording(self):
        if not self.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not self.connected:
            return
        log_path = Path(self.log_path_var.get())
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.csv_file = open(log_path, "w", newline="", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл лога:\n{e}")
            return
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            ["pc_time_iso", "pc_time_ms", "device_ts_us", "mode", "baud",
             "can_id_hex", "extended_id", "dlc", "data_hex", "rx_status_hex"]
        )
        self.recorded_count = 0
        self.record_time = time.time()
        self.recording = True

        self.record_btn.configure(text="■  СТОП ЗАПИСИ", bg="#c62828")
        self.log_path_entry.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self._log_status(f"Запись начата -> {log_path.name}")

    def _stop_recording(self):
        if self.csv_file is not None:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None
            self.csv_writer = None

        self.recording = False
        self.record_time = None

        self.record_btn.configure(text="●  ЗАПИСЬ", bg="#616161" if not self.connected else "#2e7d32")
        self.log_path_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")
        self._log_status(f"Запись остановлена, записано кадров: {self.recorded_count}")

    def _tick_stats(self):
        parts = []
        if self.connect_time is not None:
            elapsed = int(time.time() - self.connect_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            parts.append(f"На связи: {h:02d}:{m:02d}:{s:02d}")
        parts.append(f"Кадров получено: {self.frame_count}")
        if self.recording:
            parts.append(f"Записано: {self.recorded_count}")
        else:
            parts.append("Записано: —")
        self.stats_var.set("   ".join(parts))
        self.after(500, self._tick_stats)

    # ----------------------------------------------------- обработка кадров
    # Эти колбэки вызываются из потока-читателя моста — сюда только
    # безопасные для потоков операции (запись в CSV, кладём в очередь).
    # Живой просмотр и счётчик работают ВСЕГДА после подключения, запись —
    # только когда включена отдельной кнопкой.

    def _on_frame(self, line: str):
        # FRAME <pc_ms> <dev_ts_us> <can_id_hex> <ide> <dlc> <data_hex...> <rxstatus_hex>
        parts = line.split(" ")
        if len(parts) < 7:
            return
        _, pc_ms, dev_ts, can_id_hex, ide, dlc = parts[:6]
        rest = parts[6:]
        rx_status_hex = rest[-1]
        data_hex_parts = rest[:-1]
        data_hex = " ".join(data_hex_parts)

        pc_time_iso = dt.datetime.fromtimestamp(int(pc_ms) / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]

        self.frame_count += 1

        if self.recording and self.csv_writer is not None:
            self.recorded_count += 1
            self.csv_writer.writerow(
                [pc_time_iso, pc_ms, dev_ts, self._mode_at_connect, self._baud_at_connect,
                 can_id_hex, ide, dlc, data_hex, rx_status_hex]
            )
            # flush не на каждый кадр (дорого при высокой нагрузке) — раз в ~200 кадров
            if self.recorded_count % 200 == 0:
                self.csv_file.flush()

        if self.show_live_var.get():
            marker = "●" if self.recording else " "
            display = f"{marker} {pc_time_iso}  ID={can_id_hex:>8}  {'EXT' if ide=='1' else 'STD'}  DLC={dlc}  {data_hex}"
            self.display_queue.put(display)

    def _on_info(self, line: str):
        self.display_queue.put(f"# {line}")

    def _on_bridge_error(self, line: str):
        self.display_queue.put(f"! {line}")

    def _log_status(self, msg: str, error: bool = False):
        self.display_queue.put(("! " if error else "# ") + msg)

    def _poll_display_queue(self):
        lines = []
        try:
            while True:
                lines.append(self.display_queue.get_nowait())
        except queue.Empty:
            pass

        if lines:
            self.text.configure(state="normal")
            for line in lines:
                if line.startswith("!"):
                    tag = "err"
                elif line.startswith("#"):
                    tag = "info"
                elif line.startswith("●"):
                    tag = "rec"
                else:
                    tag = None
                self.text.insert("end", line + "\n", tag)
            # ограничиваем видимую историю, чтобы окно не пухло бесконечно
            n_lines = int(self.text.index("end-1c").split(".")[0])
            if n_lines > 2000:
                self.text.delete("1.0", f"{n_lines - 1500}.0")
            self.text.see("end")
            self.text.configure(state="disabled")

        self.after(150, self._poll_display_queue)

    def _on_close(self):
        if self.connected:
            self._disconnect()
        self.destroy()


def main():
    if os.name != "nt":
        print("Этот инструмент рассчитан на Windows (J2534/Scanmatik).")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
