#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Декодер UDS (ISO 14229) поверх ISO-TP (ISO 15765-2) для кадров CAN/J1939,
снятых в формате нашего логгера (can_id_hex, data_hex, ...).

Что делает:
  1. Разбирает 29-битный CAN ID на Priority/PGN/DA(dest)/SA(source) —
     по J1939-подобной схеме адресации "точка-точка", которую используют
     диагностические инструменты поверх CAN (в т.ч. видно в реальном логе
     Scanmatik: тестер SA=0xF2 опрашивает ECU по DA=16/17/35/113/114/115).
  2. Собирает многокадровые ISO-TP сообщения (Single/First/Consecutive
     Frame) в цельные UDS-пакеты.
  3. Разбирает сами UDS-пакеты:
       - 0x22/0x62 ReadDataByIdentifier — "иденты" (номера ПО/железа, VIN
         и т.п., см. ISO 14229-1 Annex F, диапазон DID 0xF180-0xF19F)
       - 0x19/0x59 ReadDTCInformation — коды ошибок (DTC) + статус
       - 0x14/0x54 ClearDiagnosticInformation — события очистки ошибок
       - 0x7F — отрицательный ответ (с расшифровкой кода причины, NRC)
       - прочие сервисы — как есть, по номеру сервиса

Модуль не привязан к Windows/Scanmatik: на вход достаточно потока
(can_id_hex:str, data_hex:str, ts) — работает и для офлайн-разбора CSV,
и для «живого» потока кадров из моста smbridge.exe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------- J1939-адресация

@dataclass
class AddrInfo:
    priority: int
    pgn: int
    da: Optional[int]   # None для широковещательных PGN (PF >= 240)
    sa: int


def decode_29bit_id(can_id_hex: str) -> AddrInfo:
    v = int(can_id_hex, 16)
    prio = (v >> 26) & 0x7
    dp = (v >> 24) & 0x1
    pf = (v >> 16) & 0xFF
    ps = (v >> 8) & 0xFF
    sa = v & 0xFF
    if pf >= 240:
        pgn = (dp << 16) | (pf << 8) | ps
        da = None
    else:
        pgn = (dp << 16) | (pf << 8)
        da = ps
    return AddrInfo(priority=prio, pgn=pgn, da=da, sa=sa)


def channel_key(addr: AddrInfo) -> Optional[tuple]:
    """Ключ 'диалога' тестер<->ECU без учёта направления — (min(sa,da), max(sa,da), pgn_pf).

    Для широковещательных PGN (da is None) диалога нет — возвращаем None,
    такие кадры в UDS-декодер не идут (это не диагностика точка-точка).
    """
    if addr.da is None:
        return None
    lo, hi = sorted((addr.sa, addr.da))
    return (lo, hi)


# --------------------------------------------------------------------- ISO-TP (ISO 15765-2)

class IsoTpReassembler:
    """Пересобирает Single/First/Consecutive Frame в цельные сообщения.

    Состояние держим ОТДЕЛЬНО по каждому CAN ID — этого достаточно: у
    каждого однонаправленного потока данных свой CAN ID (запрос тестера и
    ответ ECU идут на разных ID), а Flow Control кадры (PCI 0x3x) не несут
    данных и просто пропускаются.
    """

    def __init__(self):
        self._buffers: dict[str, dict] = {}
        # Статистика по многокадровым (First+Consecutive Frame) сообщениям —
        # чтобы можно было явно посчитать, сколько "обменов" собралось
        # целиком, а сколько повреждено (см. corrupted_gap/abandoned ниже).
        # started — сколько раз видели First Frame (начало многокадрового
        # сообщения); completed_ok — собралось полностью и без разрывов
        # последовательности; corrupted_gap — оборвано из-за разрыва seq
        # (пропал(и) Consecutive Frame); abandoned — новый First Frame на
        # том же CAN ID пришёл раньше, чем предыдущая пересборка успела
        # завершиться (тоже потеря кадров, просто с другой стороны).
        self.stats = {"started": 0, "completed_ok": 0, "corrupted_gap": 0, "abandoned": 0}

    def feed(self, can_id_hex: str, data: bytes, ts) -> Optional[bytes]:
        if not data:
            return None
        pci_hi = (data[0] >> 4) & 0xF

        if pci_hi == 0x0:
            # Single Frame: длина в младшем нибле (классический ISO-TP, 0-7)
            length = data[0] & 0xF
            if length == 0 or length > len(data) - 1:
                return None
            return bytes(data[1:1 + length])

        if pci_hi == 0x1:
            # First Frame: 12 бит длины (старший нибл byte0 + byte1)
            if len(data) < 2:
                return None
            length = ((data[0] & 0xF) << 8) | data[1]
            payload = bytes(data[2:])
            if can_id_hex in self._buffers:
                # Предыдущая пересборка на этом же CAN ID не успела
                # завершиться (не дождались всех Consecutive Frame), а её уже
                # перекрывает новый First Frame — считаем брошенной/потерянной.
                self.stats["abandoned"] += 1
            self.stats["started"] += 1
            self._buffers[can_id_hex] = {
                "total": length,
                "buf": bytearray(payload),
                "next_seq": 1,
                "ts_start": ts,
            }
            return None

        if pci_hi == 0x2:
            st = self._buffers.get(can_id_hex)
            if st is None:
                return None  # CF без предшествующего FF — игнорируем
            seq = data[0] & 0xF
            if seq != st["next_seq"]:
                # Разрыв последовательности — обычно значит, что один или
                # несколько Consecutive Frame потерялись при живом захвате на
                # загруженной шине. РАНЬШЕ здесь просто склеивали то, что
                # пришло, игнорируя разрыв — это давало "целое на вид"
                # сообщение (нужная длина набралась), но собранное из ЧУЖИХ
                # кусков данных: например, неверный VIN/иденты без единого
                # признака ошибки. Теперь такую пересборку ПРЕРЫВАЕМ и считаем
                # повреждённой, а не выдаём наружу заведомо неверные байты.
                del self._buffers[can_id_hex]
                self.stats["corrupted_gap"] += 1
                return None
            st["buf"].extend(data[1:])
            st["next_seq"] = (seq + 1) & 0xF
            if len(st["buf"]) >= st["total"]:
                complete = bytes(st["buf"][: st["total"]])
                del self._buffers[can_id_hex]
                self.stats["completed_ok"] += 1
                return complete
            return None

        if pci_hi == 0x3:
            return None  # Flow Control — не данные

        return None


# --------------------------------------------------------------------- UDS-словари

UDS_SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# ISO 14229-1 Annex F — стандартные идентификаторы данных (DID).
DID_NAMES = {
    0xF180: "Boot Software Identification",
    0xF181: "Application Software Identification",
    0xF182: "Application Data Identification",
    0xF183: "Boot Software Fingerprint",
    0xF184: "Application Software Fingerprint",
    0xF185: "Application Data Fingerprint",
    0xF186: "Active Diagnostic Session",
    0xF187: "Vehicle Manufacturer Spare Part Number",
    0xF188: "Vehicle Manufacturer ECU Software Number",
    0xF189: "Vehicle Manufacturer ECU Software Version",
    0xF18A: "System Supplier Identifier",
    0xF18B: "ECU Manufacturing Date",
    0xF18C: "ECU Serial Number",
    0xF18D: "Supported Functional Units",
    0xF18E: "Vehicle Manufacturer Kit Assembly Part Number",
    0xF190: "VIN",
    0xF191: "Vehicle Manufacturer ECU Hardware Number",
    0xF192: "System Supplier ECU Hardware Number",
    0xF193: "System Supplier ECU Hardware Version",
    0xF194: "System Supplier ECU Software Number",
    0xF195: "System Supplier ECU Software Version",
    0xF196: "Exhaust Regulation Or Type Approval Number",
    0xF197: "System Name Or Engine Type",
    0xF198: "Repair Shop Code Or Tester Serial Number",
    0xF199: "Programming Date",
    0xF19A: "Calibration Repair Shop Code",
    0xF19B: "Calibration Date",
    0xF19C: "Calibration Equipment Software Number",
    0xF19D: "ECU Installation Date",
    0xF19E: "ODX File Identifier",
    0xF19F: "Entity",
}


# --------------------------------------------------------------------- стандартная таблица адресов J1939 (SAE J1939-71)
# Source Address (SA) — байт-источник в 29-битном CAN ID (см. decode_29bit_id
# выше) — по стандарту SAE J1939-71 закреплён за определённым типом блока.
# Это СТАНДАРТНАЯ (рекомендованная) таблица: конкретный производитель может
# от неё отступать, поэтому названия — ориентир для расшифровки, а не 100%
# гарантия (проверено по исходникам протокольного анализатора Wireshark,
# packet-j1939.c, который сверен с самим стандартом).
J1939_SA_NAMES = {
    0x00: "Двигатель №1",
    0x01: "Двигатель №2",
    0x02: "Турбокомпрессор",
    0x03: "КПП №1 (коробка передач)",
    0x04: "КПП №2",
    0x05: "Консоль переключения передач — основная",
    0x06: "Консоль переключения передач — вторая",
    0x07: "Коробка отбора мощности (основная/задняя)",
    0x08: "Управляемый мост",
    0x09: "Ведущий мост №1",
    0x0A: "Ведущий мост №2",
    0x0B: "Тормозная система — контроллер",
    0x0C: "Тормоза управляемого моста",
    0x0D: "Тормоза ведущего моста №1",
    0x0E: "Тормоза ведущего моста №2",
    0x0F: "Ретардер (моторный тормоз-замедлитель)",
    0x10: "Ретардер трансмиссии",
    0x11: "Круиз-контроль",
    0x12: "Топливная система",
    0x13: "Контроллер рулевого управления",
    0x14: "Подвеска управляемого моста",
    0x15: "Подвеска ведущего моста №1",
    0x16: "Подвеска ведущего моста №2",
    0x17: "Приборная панель №1",
    0x18: "Тахограф / регистратор поездки",
    0x19: "Климат-контроль салона №1",
    0x1A: "Генератор / система зарядки",
    0x1B: "Управление аэродинамикой",
    0x1C: "Навигация",
    0x1D: "Охранная система",
    0x1E: "Электрооборудование (общая система)",
    0x1F: "Система стартера",
    0x20: "Мост «тягач-прицеп» №1",
    0x21: "Контроллер кузова (BCM)",
    0x22: "Управление доп. клапанами / воздушной системой двигателя",
    0x23: "Управление сцепным устройством (Hitch Control)",
    0x24: "Коробка отбора мощности (передняя/вторая)",
    0x25: "Внешний шлюз (Off Vehicle Gateway)",
    0x26: "Виртуальный терминал (в кабине)",
    0x27: "Управляющий компьютер №1",
    0x28: "Дисплей кабины №1",
    0x29: "Выхлопной ретардер, двигатель №1",
    0x2A: "Контроллер дистанции (система предупреждения столкновений)",
    0x2B: "Блок бортовой диагностики",
    0x2C: "Выхлопной ретардер, двигатель №2",
    0x2D: "Система непрерывного торможения",
    0x2E: "Контроллер гидронасоса",
    0x2F: "Контроллер подвески №1",
    0x30: "Контроллер пневмосистемы",
    0x31: "Контроллер кабины — основной",
    0x32: "Контроллер кабины — второй",
    0x33: "Контроллер давления в шинах",
    0x34: "Модуль зажигания №1",
    0x35: "Модуль зажигания №2",
    0x36: "Управление сиденьем №1",
    0x37: "Управление освещением (органы управления оператора)",
    0x38: "Контроллер рулевого управления задней осью №1",
    0x39: "Контроллер водяного насоса",
    0x3A: "Климат-контроль салона №2",
    0x3B: "Дисплей КПП — основной",
    0x3C: "Дисплей КПП — второй",
    0x3D: "Контроллер выхлопных выбросов",
    0x3E: "Контроль динамической стабилизации (ESP)",
    0x3F: "Датчик масла",
    0x40: "Контроллер подвески №2",
    0x41: "Контроллер информационной системы №1",
    0x42: "Управление рампой/трапом",
    0x43: "Блок сцепления/гидротрансформатора",
    0x44: "Дополнительный отопитель №1",
    0x45: "Дополнительный отопитель №2",
    0x46: "Контроллер клапанов двигателя",
    0x47: "Контроллер шасси №1",
    0x48: "Контроллер шасси №2",
    0x49: "Зарядное устройство тяговой батареи",
    0x4A: "Блок связи (сотовый)",
    0x4B: "Блок связи (спутниковый)",
    0x4C: "Блок связи (радио)",
    0x4D: "Блок рулевой колонки",
    0x4E: "Контроллер привода вентилятора",
    0x4F: "Управление сиденьем №2",
    0x50: "Контроллер стояночного тормоза",
    0x51: "Система нейтрализации ОГ №1 — впуск",
    0x52: "Система нейтрализации ОГ №1 — выпуск",
    0x53: "Система пассивной безопасности (ремни/подушки)",
    0x54: "Дисплей кабины №2",
    0x55: "Контроллер сажевого фильтра (DPF)",
    0x56: "Система нейтрализации ОГ №2 — впуск",
    0x57: "Система нейтрализации ОГ №2 — выпуск",
    0x58: "Система пассивной безопасности №2",
    0x59: "Датчик атмосферного давления",
    0xF8: "Файл-сервер/принтер",
    0xF9: "Внешний диагностический прибор №1 (например, наш тестер)",
    0xFA: "Внешний диагностический прибор №2",
    0xFB: "Бортовой регистратор данных",
    0xFC: "Зарезервировано (эксперимент.)",
    0xFD: "Зарезервировано производителем",
    0xFE: "Нулевой адрес (не назначен)",
    0xFF: "Широковещательный адрес (всем)",
}

# Стандартные диапазоны 11-битных ID для классического OBD-II поверх CAN
# (ISO 15765-4) — на случай, если прибор работает в режиме "CAN", а не
# "J1939". Тут адресация не по SA/DA, а напрямую по CAN ID.
OBD_STD_ID_NAMES = {
    0x7DF: "Функциональный запрос (широковещательно всем ЭБУ)",
    0x7E0: "Запрос -> ЭБУ №1 (физическая адресация)",
    0x7E1: "Запрос -> ЭБУ №2",
    0x7E2: "Запрос -> ЭБУ №3",
    0x7E3: "Запрос -> ЭБУ №4",
    0x7E4: "Запрос -> ЭБУ №5",
    0x7E5: "Запрос -> ЭБУ №6",
    0x7E6: "Запрос -> ЭБУ №7",
    0x7E7: "Запрос -> ЭБУ №8",
    0x7E8: "Ответ ЭБУ №1",
    0x7E9: "Ответ ЭБУ №2",
    0x7EA: "Ответ ЭБУ №3",
    0x7EB: "Ответ ЭБУ №4",
    0x7EC: "Ответ ЭБУ №5",
    0x7ED: "Ответ ЭБУ №6",
    0x7EE: "Ответ ЭБУ №7",
    0x7EF: "Ответ ЭБУ №8",
}


SESSION_NAMES = {
    0x01: "defaultSession",
    0x02: "programmingSession",
    0x03: "extendedDiagnosticSession",
    0x04: "safetySystemDiagnosticSession",
}

RESET_TYPE_NAMES = {
    0x01: "hardReset",
    0x02: "keyOffOnReset",
    0x03: "softReset",
    0x04: "enableRapidPowerShutDown",
    0x05: "disableRapidPowerShutDown",
}

ROUTINE_CONTROL_NAMES = {
    0x01: "startRoutine",
    0x02: "stopRoutine",
    0x03: "requestRoutineResults",
}

READ_DTC_SUBFUNCTIONS = {
    0x01: "reportNumberOfDTCByStatusMask",
    0x02: "reportDTCByStatusMask",
    0x03: "reportDTCSnapshotIdentification",
    0x04: "reportDTCSnapshotRecordByDTCNumber",
    0x06: "reportDTCExtendedDataRecordByDTCNumber",
    0x0A: "reportSupportedDTC",
    0x0F: "reportMirrorMemoryDTCByStatusMask",
    0x14: "reportDTCFaultDetectionCounter",
    0x15: "reportDTCWithPermanentStatus",
    0x42: "reportWWHOBDDTCByMaskRecord",
}

# Биты статуса DTC (ISO 14229-1 Table D.1), бит -> короткое имя.
DTC_STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "testFailedThisOperationCycle"),
    (0x04, "pendingDTC"),
    (0x08, "confirmedDTC"),
    (0x10, "testNotCompletedSinceLastClear"),
    (0x20, "testFailedSinceLastClear"),
    (0x40, "testNotCompletedThisOperationCycle"),
    (0x80, "warningIndicatorRequested"),
]


def decode_dtc_status(status: int) -> list[str]:
    return [name for bit, name in DTC_STATUS_BITS if status & bit]


def dtc_bytes_to_code(b0: int, b1: int) -> str:
    """Классический 2-байтный DTC (SAE J2012 / ISO15031-6, DTCFormatIdentifier=0x00)
    -> строка вида 'P0301'. Это общепринятая трактовка ПЕРВЫХ ДВУХ байт
    3-байтного UDS DTC; третий байт в этой раскладке НЕ входит в код —
    он показывается отдельно, т.к. его смысл зависит от конкретного ЭБУ.
    """
    category = "PCBU"[(b0 >> 6) & 0x3]
    digit1 = (b0 >> 4) & 0x3
    digit2 = b0 & 0xF
    digit3 = (b1 >> 4) & 0xF
    digit4 = b1 & 0xF
    return f"{category}{digit1:01X}{digit2:01X}{digit3:01X}{digit4:01X}"


def try_ascii(data: bytes) -> str:
    """Печатные ASCII-байты как строка, непечатные — точками (для читаемости 'идентов')."""
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)


# --------------------------------------------------------------------- Разбор UDS-сообщений

@dataclass
class UdsEvent:
    ts: object
    can_id: str
    sa: int
    da: int
    kind: str            # 'request' | 'positive' | 'negative' | 'raw'
    service: int
    service_name: str
    summary: str          # человекочитаемое краткое описание
    raw_hex: str
    # Заполняется только для соответствующих сервисов:
    did: Optional[int] = None
    did_value: Optional[bytes] = None
    dtcs: list = field(default_factory=list)   # [(code3bytes_hex, status_byte, decoded_code, status_flags)]
    nrc: Optional[int] = None


def decode_uds_message(payload: bytes, ts, can_id: str, sa: int, da: int) -> Optional[UdsEvent]:
    if not payload:
        return None
    sid = payload[0]
    raw_hex = payload.hex(" ").upper()

    if sid == 0x7F and len(payload) >= 3:
        orig_service = payload[1]
        nrc = payload[2]
        nrc_name = NRC_NAMES.get(nrc, f"NRC_0x{nrc:02X}")
        svc_name = UDS_SERVICES.get(orig_service, f"0x{orig_service:02X}")
        return UdsEvent(
            ts=ts, can_id=can_id, sa=sa, da=da, kind="negative",
            service=orig_service, service_name=svc_name,
            summary=f"Отрицательный ответ на {svc_name}: {nrc_name} (0x{nrc:02X})",
            raw_hex=raw_hex, nrc=nrc,
        )

    is_response = sid >= 0x40 and (sid - 0x40) in UDS_SERVICES
    base_service = (sid - 0x40) if is_response else sid
    svc_name = UDS_SERVICES.get(base_service, f"0x{base_service:02X}")
    kind = "positive" if is_response else "request"

    # --- ReadDataByIdentifier (иденты) ---
    if base_service == 0x22:
        if is_response and len(payload) >= 3:
            did = (payload[1] << 8) | payload[2]
            value = payload[3:]
            did_name = DID_NAMES.get(did, f"DID 0x{did:04X}")
            ascii_val = try_ascii(value).strip(".")
            summary = f"{did_name} (0x{did:04X}) = {ascii_val or value.hex(' ').upper()}"
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name, summary=summary,
                             raw_hex=raw_hex, did=did, did_value=value)
        elif not is_response and len(payload) >= 3:
            did = (payload[1] << 8) | payload[2]
            did_name = DID_NAMES.get(did, f"DID 0x{did:04X}")
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name,
                             summary=f"Запрос {did_name} (0x{did:04X})",
                             raw_hex=raw_hex, did=did)

    # --- ReadDTCInformation (коды ошибок) ---
    if base_service == 0x19:
        subfn = payload[1] if len(payload) >= 2 else None
        subfn_name = READ_DTC_SUBFUNCTIONS.get(subfn, f"0x{subfn:02X}" if subfn is not None else "?")
        if is_response and subfn in (0x02, 0x0F, 0x42) and len(payload) >= 3:
            # ...59 02 [availabilityMask] (DTC:3 status:1)*
            records = payload[3:]
            dtcs = []
            for i in range(0, len(records) - 3, 4):
                b0, b1, b2, status = records[i], records[i + 1], records[i + 2], records[i + 3]
                code = dtc_bytes_to_code(b0, b1)
                dtcs.append((f"{b0:02X}{b1:02X}{b2:02X}", status, code, decode_dtc_status(status)))
            summary = f"Список DTC ({subfn_name}): {len(dtcs)} шт." if dtcs else f"Список DTC ({subfn_name}): пусто"
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name, summary=summary,
                             raw_hex=raw_hex, dtcs=dtcs)
        if is_response and subfn == 0x01 and len(payload) >= 6:
            avail, fmt, cnt_hi, cnt_lo = payload[2], payload[3], payload[4], payload[5]
            count = (cnt_hi << 8) | cnt_lo
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name,
                             summary=f"Количество DTC ({subfn_name}): {count}",
                             raw_hex=raw_hex)
        # прочие подфункции 0x19 — просто как есть
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name,
                         summary=f"{svc_name} {subfn_name}",
                         raw_hex=raw_hex)

    # --- ClearDiagnosticInformation ---
    if base_service == 0x14:
        if not is_response and len(payload) >= 4:
            group = (payload[1] << 16) | (payload[2] << 8) | payload[3]
            grp_txt = "ВСЕ группы" if group == 0xFFFFFF else f"группа 0x{group:06X}"
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name,
                             summary=f"Запрос очистки DTC: {grp_txt}", raw_hex=raw_hex)
        if is_response:
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name,
                             summary="DTC успешно очищены", raw_hex=raw_hex)

    # --- DiagnosticSessionControl ---
    if base_service == 0x10 and len(payload) >= 2:
        session = payload[1] & 0x7F  # старший бит — suppressPositiveResponse, к имени не относится
        name = SESSION_NAMES.get(session, f"0x{session:02X}")
        verb = "Сессия установлена" if is_response else "Запрос смены сессии"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name,
                         summary=f"{verb}: {name} (0x{session:02X})", raw_hex=raw_hex)

    # --- ECUReset ---
    if base_service == 0x11 and len(payload) >= 2:
        rtype = payload[1] & 0x7F
        name = RESET_TYPE_NAMES.get(rtype, f"0x{rtype:02X}")
        verb = "Сброс подтверждён" if is_response else "Запрос сброса ЭБУ"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name,
                         summary=f"{verb}: {name} (0x{rtype:02X})", raw_hex=raw_hex)

    # --- SecurityAccess ---
    if base_service == 0x27 and len(payload) >= 2:
        level = payload[1]
        extra = payload[2:]
        if is_response:
            if level % 2 == 1 and extra:
                summary = f"SecurityAccess: получен seed уровня 0x{level:02X} = {extra.hex(' ').upper()}"
            else:
                summary = f"SecurityAccess: доступ разрешён (уровень 0x{level:02X})"
        else:
            summary = f"SecurityAccess: запрос уровня 0x{level:02X}" + (f", ключ={extra.hex(' ').upper()}" if extra else "")
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- WriteDataByIdentifier ---
    if base_service == 0x2E:
        if len(payload) >= 3:
            did = (payload[1] << 8) | payload[2]
            did_name = DID_NAMES.get(did, f"DID 0x{did:04X}")
            if is_response:
                summary = f"{did_name} (0x{did:04X}) успешно записан"
            else:
                value = payload[3:]
                summary = f"Запись {did_name} (0x{did:04X}) = {value.hex(' ').upper()}"
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name, summary=summary,
                             raw_hex=raw_hex, did=did)

    # --- TesterPresent ---
    if base_service == 0x3E:
        summary = "TesterPresent: ЭБУ подтвердил связь" if is_response else "TesterPresent"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- RoutineControl ---
    if base_service == 0x31 and len(payload) >= 4:
        ctrl = payload[1]
        routine_id = (payload[2] << 8) | payload[3]
        ctrl_name = ROUTINE_CONTROL_NAMES.get(ctrl, f"0x{ctrl:02X}")
        extra = payload[4:]
        summary = f"RoutineControl {ctrl_name}, ID=0x{routine_id:04X}"
        if extra:
            summary += f", данные={extra.hex(' ').upper()}"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- RequestDownload / RequestUpload (флеш ЭБУ, ISO 14229-1 §11) ---
    if base_service in (0x34, 0x35):
        if is_response and len(payload) >= 2:
            length_fmt_id = payload[1]
            n_bytes = (length_fmt_id >> 4) & 0xF
            max_block = None
            if n_bytes and len(payload) >= 2 + n_bytes:
                max_block = int.from_bytes(payload[2:2 + n_bytes], "big")
            summary = f"{svc_name}: разрешено" + (f", maxNumberOfBlockLength={max_block}" if max_block is not None else "")
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)
        if not is_response and len(payload) >= 2:
            summary = f"{svc_name}: запрос (dataFormatIdentifier=0x{payload[1]:02X})"
            return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                             service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- TransferData (флеш ЭБУ) ---
    if base_service == 0x36 and len(payload) >= 2:
        counter = payload[1]
        data = payload[2:]
        if is_response:
            summary = f"TransferData: блок №{counter} принят" + (f", получено {len(data)} байт" if data else "")
        else:
            summary = f"TransferData: блок №{counter}, {len(data)} байт"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- RequestTransferExit (флеш ЭБУ) ---
    if base_service == 0x37:
        summary = "RequestTransferExit: передача завершена" if is_response else "RequestTransferExit: запрос завершения передачи"
        return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                         service=base_service, service_name=svc_name, summary=summary, raw_hex=raw_hex)

    # --- всё остальное — как есть ---
    return UdsEvent(ts=ts, can_id=can_id, sa=sa, da=da, kind=kind,
                     service=base_service, service_name=svc_name,
                     summary=f"{svc_name} [{kind}]", raw_hex=raw_hex)


# --------------------------------------------------------------------- Высокоуровневый анализатор

class Analyzer:
    """Копит состояние по ходу разбора потока кадров: идентификацию и
    коды ошибок по каждому обнаруженному ECU (ключ — SA, source address).
    """

    def __init__(self):
        self.reassembler = IsoTpReassembler()
        # sa -> {"da": set(), "idents": {did: (name, value_str)}, "dtcs": {code: (status, flags, ts)},
        #        "events": [ (ts, text) ] }
        self.ecus: dict[int, dict] = {}
        self.events: list[UdsEvent] = []

    def _ecu(self, sa: int) -> dict:
        return self.ecus.setdefault(sa, {"da": set(), "idents": {}, "dtcs": {}, "events": []})

    def feed_frame(self, can_id_hex: str, data_hex: str, ts) -> Optional[UdsEvent]:
        try:
            data = bytes.fromhex(data_hex.replace(" ", ""))
        except ValueError:
            return None
        addr = decode_29bit_id(can_id_hex)
        if addr.da is None:
            return None  # широковещательный PGN — не диагностика точка-точка

        complete = self.reassembler.feed(can_id_hex, data, ts)
        if complete is None:
            return None

        ev = decode_uds_message(complete, ts, can_id_hex, addr.sa, addr.da)
        if ev is None:
            return None
        self.events.append(ev)

        # Идентификацию/ошибки относим к ECU, который ОТВЕЧАЕТ (sa этого
        # кадра при positive/negative), либо к тому, КОМУ адресован запрос
        # (da этого кадра при kind == 'request') — так у каждого ECU
        # собирается его личная карточка независимо от направления кадра.
        target_sa = ev.sa if ev.kind in ("positive", "negative") else ev.da
        ecu = self._ecu(target_sa)
        ecu["da"].add(ev.da if ev.kind == "request" else ev.sa)

        if ev.kind == "positive" and ev.service == 0x22 and ev.did is not None:
            did_name = DID_NAMES.get(ev.did, f"DID 0x{ev.did:04X}")
            ascii_val = try_ascii(ev.did_value or b"").strip(".")
            ecu["idents"][ev.did] = (did_name, ascii_val or (ev.did_value or b"").hex(" ").upper())

        if ev.kind == "positive" and ev.service == 0x19 and ev.dtcs:
            for code3, status, code, flags in ev.dtcs:
                ecu["dtcs"][code3] = (status, flags, code, ts)

        if ev.service == 0x14:
            # Тестеры нередко шлют один и тот же запрос очистки по многу раз
            # подряд (без ответа/подтверждения) — не копим дубликаты одного
            # и того же события подряд, иначе список забивается сотнями
            # одинаковых строк.
            prev = ecu["events"][-1] if ecu["events"] else None
            if prev is None or prev[1] != ev.summary:
                ecu["events"].append((ts, ev.summary))
            else:
                ecu["events"][-1] = (ts, ev.summary)  # обновляем время последнего повтора

        return ev
