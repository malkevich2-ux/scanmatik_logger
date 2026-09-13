#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Одноразовый headless-тест очистки DTC на реальном ЭБУ — использует ТОЧНО ТЕ ЖЕ
модули (bridge_common, uds_decoder, uds_tx), что и can_dtc_reader_gui.py,
только без Tkinter, чтобы избежать проблем с буферизацией вывода при
управлении процессом через PowerShell-обёртки.

Порядок:
  1. Подключение к мосту (OPEN/CONNECT/START), как в GUI.
  2. Читаем список DTC ДО очистки (для сравнения).
  3. Отправляем ClearDiagnosticInformation (0x14, группа FFFFFF) с повторами
     до 4 раз, как это делает кнопка "Очистить DTC" в GUI.
  4. Читаем список DTC ПОСЛЕ очистки.
  5. Печатаем итог.
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_common import BridgeProcess, parse_frame_line, find_bridge_exe
from uds_decoder import Analyzer, decode_29bit_id
from uds_tx import (
    build_can_id, build_sf, build_flow_control, is_first_frame,
    req_read_dtc_by_status_mask, req_clear_dtc,
)

DLL = r"C:\Program Files (x86)\Scanmatik\smj2534.dll"
TESTER_SA = 0xF2
ECU_DA = 0x23
RESPONSE_TIMEOUT_S = 1.5
PENDING_EXTRA_S = 5.0
SEND_ATTEMPTS = 4

resp_queue: "queue.Queue" = queue.Queue()
analyzer = Analyzer()
bridge: BridgeProcess | None = None


def on_frame(line: str):
    parsed = parse_frame_line(line)
    if parsed is None:
        return
    pc_ms, _dev_ts, can_id_hex, ide, data_hex, _rx_status = parsed
    if ide != "1":
        return
    try:
        data_bytes = bytes.fromhex(data_hex.replace(" ", ""))
    except ValueError:
        return

    addr = decode_29bit_id(can_id_hex)
    if addr.da == TESTER_SA and is_first_frame(data_bytes):
        fc_id = build_can_id(TESTER_SA, addr.sa)
        fc_frame = build_flow_control()
        bridge.send(f"SEND {fc_id:X} " + " ".join(f"{b:02X}" for b in fc_frame))
        print(f"  -> авто-Flow-Control отправлен на {fc_id:X}")

    ev = analyzer.feed_frame(can_id_hex, data_hex, time.time())
    if ev is not None:
        print(f"  EVENT: {ev.kind} {ev.service_name} :: {ev.summary}  (raw={ev.raw_hex})")
        if ev.kind in ("positive", "negative") and ev.da == TESTER_SA and ev.sa == ECU_DA:
            resp_queue.put(ev)


def on_info(line: str):
    print(f"  INFO: {line}")


def on_error(line: str):
    print(f"  ERR: {line}")


def send_uds_payload(payload: bytes):
    can_id = build_can_id(TESTER_SA, ECU_DA)
    frame = build_sf(payload)
    while not resp_queue.empty():
        resp_queue.get_nowait()
    bridge.send(f"SEND {can_id:X} " + " ".join(f"{b:02X}" for b in frame))


def wait_response(timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ev = resp_queue.get(timeout=remaining)
        except queue.Empty:
            return None
        if ev.kind == "negative" and ev.nrc == 0x78:
            deadline = time.monotonic() + PENDING_EXTRA_S
            print("  (NRC 0x78 responsePending — продлеваем ожидание)")
            continue
        return ev


def send_and_wait(payload: bytes, label: str):
    for attempt in range(1, SEND_ATTEMPTS + 1):
        if attempt > 1:
            print(f"[{label}] нет ответа, повтор {attempt}/{SEND_ATTEMPTS}...")
        send_uds_payload(payload)
        ev = wait_response(RESPONSE_TIMEOUT_S)
        if ev is not None:
            return ev
    return None


def main():
    global bridge
    exe = find_bridge_exe()
    if exe is None:
        print("Не найден smbridge.exe")
        return 1
    print(f"bridge exe: {exe}")

    bridge = BridgeProcess(exe, on_frame=on_frame, on_info=on_info, on_error=on_error)
    bridge.start_process()
    time.sleep(0.3)

    bridge.send(f"OPEN {DLL}")
    time.sleep(0.5)
    bridge.send("CONNECT J1939 500000")
    time.sleep(0.5)
    bridge.send("START")
    time.sleep(0.5)

    print("\n=== 1. Чтение DTC ДО очистки ===")
    ev_before = send_and_wait(req_read_dtc_by_status_mask(0xFF), "Чтение DTC (до)")
    if ev_before is None:
        print("Нет ответа на чтение DTC (до очистки).")
    else:
        print(f"Результат (до): {ev_before.summary}, DTC: {ev_before.dtcs}")

    print("\n=== 2. Очистка DTC (0x14, группа FFFFFF) ===")
    ev_clear = send_and_wait(req_clear_dtc(), "Очистка DTC")
    if ev_clear is None:
        print("НЕТ ОТВЕТА на запрос очистки DTC после всех попыток.")
    else:
        print(f"Результат очистки: kind={ev_clear.kind} :: {ev_clear.summary}")

    time.sleep(0.5)

    print("\n=== 3. Чтение DTC ПОСЛЕ очистки ===")
    ev_after = send_and_wait(req_read_dtc_by_status_mask(0xFF), "Чтение DTC (после)")
    if ev_after is None:
        print("Нет ответа на чтение DTC (после очистки).")
    else:
        print(f"Результат (после): {ev_after.summary}, DTC: {ev_after.dtcs}")

    bridge.send("STOP")
    time.sleep(0.2)
    bridge.send("CLOSE")
    time.sleep(0.3)
    bridge.terminate()

    print("\n=== ИТОГ ===")
    print(f"До очистки:   {'нет ответа' if ev_before is None else ev_before.summary}")
    print(f"Очистка:      {'нет ответа' if ev_clear is None else ev_clear.summary}")
    print(f"После очистки:{'нет ответа' if ev_after is None else ev_after.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
