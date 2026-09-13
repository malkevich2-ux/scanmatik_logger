#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Формирование читаемого отчёта по результатам работы Analyzer (uds_decoder.py):
иденты и коды ошибок (DTC) по каждому обнаруженному ECU, плюс события
очистки DTC. Используется и CLI-анализатором, и GUI-программой.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from uds_decoder import Analyzer, DID_NAMES, UdsEvent


def analyze_csv_file(path: str | Path) -> tuple[Analyzer, int, int]:
    """Читает CSV в формате нашего логгера, прогоняет через Analyzer.

    Возвращает (analyzer, всего_строк, событий_UDS).

    ВАЖНО: колонки читаем ПО ИМЕНИ (csv.DictReader), а не по фиксированной
    позиции. У нас теперь ДВА разных источника CSV с разным набором и
    порядком колонок — can_logger_gui.py (pc_time_iso, pc_time_ms,
    device_ts_us, mode, baud, can_id_hex, extended_id, dlc, data_hex, ...) и
    вкладка "Монитор шины" в can_dtc_reader_gui.py (pc_time_iso, pc_time_ms,
    dev_ts_us, can_id_hex, extended_id, dlc, data_hex, rx_status_hex, адрес,
    блок, pgn_hex) — при чтении по ФИКСИРОВАННЫМ позициям (row[:9]) один из
    двух форматов читался бы с чужими значениями в чужих колонках (например,
    dlc вместо can_id_hex), что и давало вид "повреждённых" данных без
    единой настоящей потери кадра.
    """
    analyzer = Analyzer()
    total_rows = 0
    total_events = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"pc_time_iso", "can_id_hex", "extended_id", "data_hex"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Файл {path} не похож на CSV нашего логгера — не хватает колонок: {', '.join(sorted(missing))}"
            )
        for row in reader:
            total_rows += 1
            if row.get("extended_id") != "1":
                continue  # диагностика точка-точка живёт только на 29-битных ID
            ev = analyzer.feed_frame(row["can_id_hex"], row["data_hex"], row["pc_time_iso"])
            if ev is not None:
                total_events += 1
    return analyzer, total_rows, total_events


def _ecu_is_interesting(ecu: dict) -> bool:
    return bool(ecu["idents"]) or bool(ecu["dtcs"]) or bool(ecu["events"])


def format_report(analyzer: Analyzer, *, title: str | None = None) -> str:
    """Человекочитаемый текстовый отчёт (по-русски) для показа пользователю
    или сохранения в файл."""
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("=" * len(title))
        lines.append("")

    stats = analyzer.reassembler.stats
    if stats["started"]:
        corrupted = stats["corrupted_gap"] + stats["abandoned"]
        pct = 100 * corrupted / stats["started"]
        lines.append(
            f"Многокадровых (First+Consecutive Frame) обменов: {stats['started']}, "
            f"собралось целиком: {stats['completed_ok']}, "
            f"повреждено потерей кадра: {corrupted} ({pct:.0f}%)"
            + (f" [разрыв seq: {stats['corrupted_gap']}, брошено новым FF: {stats['abandoned']}]"
               if corrupted else "")
        )
        lines.append("")

    interesting = {sa: ecu for sa, ecu in analyzer.ecus.items() if _ecu_is_interesting(ecu)}

    if not interesting:
        lines.append("Диагностических обменов (UDS) в этом логе не найдено.")
        return "\n".join(lines)

    for sa in sorted(interesting):
        ecu = interesting[sa]
        lines.append(f"ЭБУ (адрес источника SA=0x{sa:02X}):")

        if ecu["idents"]:
            lines.append("  Иденты:")
            for did in sorted(ecu["idents"]):
                name, val = ecu["idents"][did]
                lines.append(f"    0x{did:04X}  {name}: {val}")
        else:
            lines.append("  Иденты: не прочитаны")

        if ecu["dtcs"]:
            lines.append("  Коды ошибок (DTC):")
            for code3, (status, flags, code, ts) in sorted(ecu["dtcs"].items()):
                flags_txt = ", ".join(flags) if flags else "—"
                lines.append(
                    f"    {code}  (raw {code3}, статус 0x{status:02X})  [{flags_txt}]  время: {ts}"
                )
        else:
            lines.append("  Коды ошибок (DTC): не обнаружено / не запрашивались")

        if ecu["events"]:
            lines.append("  События очистки DTC:")
            for ts, text in ecu["events"]:
                lines.append(f"    {ts}: {text}")

        lines.append("")

    return "\n".join(lines)


def dedup_events(events: Iterable[UdsEvent]) -> list[tuple[UdsEvent, int]]:
    """Схлопывает подряд идущие одинаковые события (частая ретрансляция
    одного и того же запроса тестером) в (событие, счётчик_повторов)."""
    out: list[tuple[UdsEvent, int]] = []
    for ev in events:
        if out:
            prev_ev, cnt = out[-1]
            if prev_ev.kind == ev.kind and prev_ev.service == ev.service and prev_ev.summary == ev.summary \
               and prev_ev.sa == ev.sa and prev_ev.da == ev.da:
                out[-1] = (prev_ev, cnt + 1)
                continue
        out.append((ev, 1))
    return out


if __name__ == "__main__":
    import sys

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("Использование: python dtc_report.py <путь_к_логу.csv>")
        sys.exit(1)

    csv_path = sys.argv[1]
    analyzer, total_rows, total_events = analyze_csv_file(csv_path)
    print(f"Обработано строк: {total_rows}, UDS-событий: {total_events}\n")
    print(format_report(analyzer, title=f"Отчёт по логу: {Path(csv_path).name}"))
