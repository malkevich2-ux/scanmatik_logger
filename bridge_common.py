#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общий код для работы с мостом smbridge.exe (поиск J2534-устройств в реестре
Windows, поиск exe моста, обёртка над подпроцессом) — используется и
логгером (can_logger_gui.py), и программой чтения идентов/ошибок
(can_dtc_reader_gui.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import winreg  # только Windows
except ImportError:
    winreg = None

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


def _reg_val(key, name: str) -> str | None:
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return str(val)
    except OSError:
        return None


def find_j2534_devices() -> list[tuple[str, str]]:
    """Возвращает список (человекочитаемое_имя, путь_к_dll) из реестра Windows."""
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

    devices.sort(key=lambda d: (0 if "scanmatik" in d[0].lower() else 1, d[0]))
    return devices


class BridgeProcess:
    """Обёртка над подпроцессом smbridge.exe: отправка команд, чтение строк."""

    def __init__(self, exe_path: Path, on_frame, on_info, on_error):
        self.exe_path = exe_path
        self.on_frame = on_frame
        self.on_info = on_info
        self.on_error = on_error
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = False
        # send() может дёргаться из нескольких потоков одновременно (поток
        # чтения — при авто-Flow-Control, рабочий поток — при отправке
        # запроса) — оборачиваем запись в stdin в лок, чтобы строки не
        # перемешивались.
        self._send_lock = threading.Lock()

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
        with self._send_lock:
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


def parse_frame_line(line: str) -> tuple[str, str, str, str, str, str] | None:
    """Разбирает строку 'FRAME <pc_ms> <dev_ts_us> <can_id_hex> <ide> <dlc> <data...> <rxstatus>'.

    Возвращает (pc_ms, dev_ts_us, can_id_hex, ide, data_hex, rx_status_hex)
    или None, если строка не распознана.

    dev_ts_us — таймstamp самого J2534-адаптера (в мосте smbridge.exe), а не
    ПК: он куда точнее, чем pc_ms (см. NowMs() в bridge/Program.cs — часы ПК
    на Windows по умолчанию обновляются раз в ~15 мс, поэтому несколько
    кадров, реально пришедших с интервалом в пару миллисекунд, могут
    получить ОДИНАКОВЫЙ pc_ms — это выглядит как дубль в логе, хотя кадры
    разные и ничего не потерялось). Раньше это поле отбрасывалось при
    разборе — теперь прокидывается дальше, чтобы можно было отличить
    "просто грубое разрешение таймstamp'а ПК" от настоящей потери/задержки
    кадров.
    """
    parts = line.split(" ")
    if len(parts) < 7:
        return None
    _, pc_ms, dev_ts, can_id_hex, ide, dlc = parts[:6]
    rest = parts[6:]
    rx_status_hex = rest[-1]
    data_hex = " ".join(rest[:-1])
    return pc_ms, dev_ts, can_id_hex, ide, data_hex, rx_status_hex
