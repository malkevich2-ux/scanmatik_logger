#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Справочник расшифровки кодов ошибок (DTC) — загружается из dtc_reference_ru.json
(создан один раз из присланного пользователем Excel-файла, см. историю
диалога). В самом JSON — два словаря:

  - failure_types: {значение_байта (0-163) -> русское описание типа
    неисправности}. Судя по структуре справочника (163 значения — то есть
    ровно влезает в один байт) и по сопоставлению с реальными DTC,
    прочитанными с ЭБУ 0x23, это, по всей видимости, ПРЯМАЯ расшифровка
    ТРЕТЬЕГО байта 3-байтного DTC-кода (b0,b1 — это "код" в формате
    P/C/B/U — см. dtc_bytes_to_code в uds_decoder.py, а b2 — тип
    неисправности). Это не подтверждено официальной документацией Volvo,
    поэтому в UI это помечается как предположительная трактовка.

  - components: {DiagnosticObjectId (например, "D1A0A") -> русское название
    компонента/подсистемы}. Это ВНУТРЕННИЕ последовательные ID Volvo
    (проверено: они НЕ вычисляются из байтов DTC, которые реально приходят
    с шины — нужна ещё отдельная таблица-связка конкретной прошивки ЭБУ с
    этими ID, которой в присланном файле нет). Поэтому этот словарь НЕ
    используется для автоматической расшифровки живых DTC — только для
    ручного поиска (вкладка "Справочник Volvo").

Если файла dtc_reference_ru.json нет рядом со скриптом — оба словаря будут
пустыми, и все функции ниже просто вернут "нет данных"/False, ничего не
ломая (расшифровка типа неисправности молча отключится).
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "dtc_reference_ru.json"


def _load() -> tuple[dict[int, str], dict[str, str]]:
    if not _DATA_PATH.exists():
        return {}, {}
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    failure_types = {}
    for k, v in data.get("failure_types", {}).items():
        try:
            failure_types[int(k)] = v
        except ValueError:
            continue
    components = dict(data.get("components", {}))
    return failure_types, components


FAILURE_TYPE_RU, COMPONENT_RU = _load()


def describe_failure_type(code3_hex: str) -> str:
    """code3_hex — 3-байтный DTC-идентификатор как hex-строка (например,
    "500001", БЕЗ пробелов). Возвращает русское описание типа
    неисправности по третьему байту, либо "" если справочник не загружен
    или значение не найдено."""
    code3_hex = code3_hex.replace(" ", "")
    if len(code3_hex) < 6:
        return ""
    try:
        b2 = int(code3_hex[4:6], 16)
    except ValueError:
        return ""
    return FAILURE_TYPE_RU.get(b2, "")


def is_dtc_active(status: int) -> bool:
    """testFailed (бит 0x01 маски статуса DTC, ISO 14229-1 Table D.1) —
    неисправность проявляется ПРЯМО СЕЙЧАС. Без этого бита DTC считается
    неактивным (историческим) — даже если стоит confirmedDTC (0x08): это
    значит "была подтверждена ранее", но сейчас не проявляется."""
    return bool(status & 0x01)


def search_components(query: str, limit: int = 200) -> list[tuple[str, str]]:
    """Ищет по подстроке (без учёта регистра) в ID компонента ИЛИ в его
    русском названии. Возвращает не больше limit пар (id, название),
    отсортированных по id — специально ограничено, т.к. словарь на 5380
    записей и без лимита список в Treeview будет неудобным/медленным при
    пустом запросе."""
    q = query.strip().lower()
    out = []
    for oid, name in sorted(COMPONENT_RU.items()):
        if not q or q in oid.lower() or q in name.lower():
            out.append((oid, name))
            if len(out) >= limit:
                break
    return out
