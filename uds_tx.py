#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка исходящих кадров для АКТИВНОГО опроса ЭБУ по UDS (ISO 14229) поверх
CAN/J1939-подобной точка-точка адресации — той же схемы, что видна в
реальном захваченном логе (см. uds_decoder.decode_29bit_id):

    CAN ID = (TOP_BYTE << 24) | (PF_DIAG << 16) | (DA << 8) | SA

где SA — адрес источника (для запроса тестера — адрес тестера), DA — адрес
получателя. Так же был устроен обмен между сторонним диагностическим
инструментом и ЭБУ в захваченном логе (тестер SA=0xF2, ЭБУ SA=0x23) —
воспроизводим ту же схему для собственных запросов.

TOP_BYTE = 0x1F взят ЦЕЛИКОМ из реальных кадров лога (0x1F4023F2 и
0x1F40F223), а не собран из отдельных битов Priority/EDP/DP по спецификации
J1939 — раньше здесь была ошибка: биты собирались как
(7<<26)|(1<<24)|... что даёт 0x1D4023F2, а не настоящие 0x1F4023F2 (не
хватало бита EDP, который в этих кадрах установлен в 1). Из-за этого
исходящие запросы уходили на несуществующий для ЭБУ ID и оставались без
ответа — при этом кадры СТОРОННЕГО инструмента (на правильном ID) по-прежнему
корректно ловились и декодировались пассивным приёмником, что и создавало
впечатление "работает только вместе с другим прибором". Взяв верхний байт
готовым числом, а не собирая по битам, эту ошибку исключаем в принципе.
"""

from __future__ import annotations

TOP_BYTE = 0x1F  # старший байт CAN ID, как в реальных кадрах лога (0x1F4023F2 / 0x1F40F223)
PF_DIAG = 0x40   # наблюдаемый в реальном логе PF для точка-точка диагностики


def build_can_id(sa: int, da: int) -> int:
    """CAN ID для кадра, отправляемого узлом с адресом sa узлу da."""
    return (TOP_BYTE << 24) | (PF_DIAG << 16) | ((da & 0xFF) << 8) | (sa & 0xFF)


def build_sf(payload: bytes) -> bytes:
    """Single Frame ISO-TP: [длина_в_младшем_нибле, *payload, паддинг нулями до 8 байт].

    Наблюдаемый в реальных кадрах паддинг — нулевой (см. запросы в логе:
    '03 22 F1 91 00 00 00 00'), поэтому паддим нулями, а не 0xCC/0xAA.
    """
    if not (1 <= len(payload) <= 7):
        raise ValueError(f"Single Frame поддерживает 1-7 байт payload, получено {len(payload)}")
    frame = bytes([len(payload)]) + payload
    return frame + bytes(8 - len(frame))


def build_flow_control(block_size: int = 0, st_min: int = 0) -> bytes:
    """Flow Control кадр (PCI 0x3x): ContinueToSend, blockSize, STmin, паддинг."""
    return bytes([0x30, block_size & 0xFF, st_min & 0xFF]) + bytes(5)


def is_first_frame(data: bytes) -> bool:
    return bool(data) and ((data[0] >> 4) & 0xF) == 0x1


def is_flow_control(data: bytes) -> bool:
    return bool(data) and ((data[0] >> 4) & 0xF) == 0x3


def parse_flow_control(data: bytes) -> tuple[int, int, int] | None:
    """Разбирает Flow Control кадр -> (flow_status, block_size, st_min) или
    None, если это не FC. flow_status: 0=ContinueToSend, 1=Wait, 2=Overflow."""
    if not is_flow_control(data):
        return None
    fs = data[0] & 0xF
    block_size = data[1] if len(data) > 1 else 0
    st_min = data[2] if len(data) > 2 else 0
    return fs, block_size, st_min


def st_min_to_seconds(st_min: int) -> float:
    """STmin (ISO 15765-2 Table 12) -> пауза в секундах между Consecutive Frame.
    0x00-0x7F — миллисекунды, 0xF1-0xF9 — 100-900 микросекунд, остальное —
    зарезервировано (не ждём)."""
    if st_min <= 0x7F:
        return st_min / 1000.0
    if 0xF1 <= st_min <= 0xF9:
        return (st_min - 0xF0) / 10000.0
    return 0.0


# Классический (не extended) ISO-TP: 12 бит на длину в First Frame -> максимум 4095 байт.
MAX_MULTI_FRAME_PAYLOAD = 0xFFF


def build_ff(payload: bytes) -> bytes:
    """First Frame ISO-TP: PCI 0x1X + 12-битная длина (X — старший нибл длины,
    следующий байт — младшие 8 бит), затем первые 6 байт payload. Кадр всегда
    ровно 8 байт (2 байта PCI/длины + 6 байт данных), паддинг не нужен.
    Используется, когда len(payload) > 7 — иначе нужен build_sf."""
    length = len(payload)
    if length <= 7:
        raise ValueError("Для payload <= 7 байт нужен build_sf (Single Frame), не First Frame")
    if length > MAX_MULTI_FRAME_PAYLOAD:
        raise ValueError(f"Слишком длинный payload для классического ISO-TP: {length} байт (максимум {MAX_MULTI_FRAME_PAYLOAD})")
    b0 = 0x10 | ((length >> 8) & 0xF)
    b1 = length & 0xFF
    return bytes([b0, b1]) + payload[:6]


def build_cf(seq: int, chunk: bytes) -> bytes:
    """Consecutive Frame ISO-TP: PCI 0x2X (X — номер по модулю 16, начиная с 1
    после First Frame) + до 7 байт данных, паддинг нулями до 8 байт."""
    if not (1 <= len(chunk) <= 7):
        raise ValueError(f"Consecutive Frame поддерживает 1-7 байт данных, получено {len(chunk)}")
    frame = bytes([0x20 | (seq & 0xF)]) + chunk
    return frame + bytes(8 - len(frame))


def req_read_did(did: int) -> bytes:
    """ReadDataByIdentifier (0x22) — запрос идента по DID."""
    return bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])


def req_read_dtc_by_status_mask(mask: int = 0xFF) -> bytes:
    """ReadDTCInformation (0x19), reportDTCByStatusMask (0x02) — список DTC."""
    return bytes([0x19, 0x02, mask & 0xFF])


def req_clear_dtc(group: int = 0xFFFFFF) -> bytes:
    """ClearDiagnosticInformation (0x14). group=0xFFFFFF — все группы (все DTC)."""
    return bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF])


# --------------------------------------------------------------------- доп. сервисы для конструктора запросов

# Подфункции 0x19 ReadDTCInformation, для которых второй параметр —
# маска статуса (остальные подфункции параметр не берут либо берут другие
# аргументы, которые конструктор пока не строит).
DTC_SUBFUNCTIONS_WITH_MASK = {0x01, 0x02, 0x0F, 0x42}


def req_read_dtc(subfunction: int = 0x02, mask: int = 0xFF) -> bytes:
    """ReadDTCInformation (0x19) с произвольной подфункцией.

    Для подфункций из DTC_SUBFUNCTIONS_WITH_MASK добавляется байт маски
    статуса, для остальных (например 0x0A reportSupportedDTC) — нет, как
    того требует ISO 14229-1.
    """
    if subfunction in DTC_SUBFUNCTIONS_WITH_MASK:
        return bytes([0x19, subfunction & 0xFF, mask & 0xFF])
    return bytes([0x19, subfunction & 0xFF])


def req_diag_session_control(session: int) -> bytes:
    """DiagnosticSessionControl (0x10) — переключение диагностической сессии."""
    return bytes([0x10, session & 0xFF])


def req_ecu_reset(reset_type: int) -> bytes:
    """ECUReset (0x11) — запрос сброса ЭБУ."""
    return bytes([0x11, reset_type & 0xFF])


def req_security_access(level: int, key: bytes = b"") -> bytes:
    """SecurityAccess (0x27). Нечётный level — запрос seed; чётный + key —
    отправка ключа (сам ключ по seed мы не считаем — нужен алгоритм
    производителя ЭБУ, конструктор умеет только запросить seed)."""
    return bytes([0x27, level & 0xFF]) + key


def req_write_did(did: int, value: bytes) -> bytes:
    """WriteDataByIdentifier (0x2E) — запись значения в идент по DID."""
    return bytes([0x2E, (did >> 8) & 0xFF, did & 0xFF]) + value


def req_tester_present() -> bytes:
    """TesterPresent (0x3E), zeroSubFunction — сигнал 'тестер на связи'."""
    return bytes([0x3E, 0x00])


def req_routine_control(control_type: int, routine_id: int, data: bytes = b"") -> bytes:
    """RoutineControl (0x31) — запуск/остановка/чтение результата рутины по ID."""
    return bytes([0x31, control_type & 0xFF, (routine_id >> 8) & 0xFF, routine_id & 0xFF]) + data


def req_communication_control(control_type: int, comm_type: int = 0x01) -> bytes:
    """CommunicationControl (0x28) — включить/выключить обмен другими
    сообщениями на время программирования. control_type: 0x00
    enableRxAndTx, 0x01 enableRxAndDisableTx, 0x02 disableRxAndEnableTx,
    0x03 disableRxAndTx. comm_type=0x01 — normal communication messages
    (наиболее частый вариант перед флешем)."""
    return bytes([0x28, control_type & 0xFF, comm_type & 0xFF])


# ----------------------------------------------------------- флеш ЭБУ (ISO 14229-1 §11)
#
# RequestDownload(0x34)/RequestUpload(0x35)/TransferData(0x36)/
# RequestTransferExit(0x37) — стандартная последовательность программирования
# памяти ЭБУ. В отличие от остальных сервисов выше, тут ДВА параметра прямо
# зависят от конкретного ЭБУ и не имеют универсального дефолта:
#   - addressAndLengthFormatIdentifier — по одномуниблу на ширину (в байтах)
#     поля адреса и поля размера (обычно 4+4, но бывает 2+2, 3+3 и т.п.);
#   - dataFormatIdentifier — обычно 0x00 (без сжатия/шифрования), но ЭБУ
#     может требовать другое значение.
# Эти параметры не гадаем — они приходят от вызывающего кода (см.
# uds_flash.py), который получает их из документации/лицензии на прошивку
# конкретного блока, а не хардкодится здесь.

def encode_addr_and_len_format_id(addr_bytes: int, size_bytes: int) -> int:
    """Собирает addressAndLengthFormatIdentifier: старший нибл — ширина поля
    memorySize в байтах, младший — ширина поля memoryAddress в байтах
    (порядок именно такой согласно ISO 14229-1 Table 396)."""
    if not (1 <= addr_bytes <= 0xF) or not (1 <= size_bytes <= 0xF):
        raise ValueError("Ширина поля адреса/размера должна быть 1-15 байт")
    return ((size_bytes & 0xF) << 4) | (addr_bytes & 0xF)


def req_request_download(
    memory_address: int, memory_size: int, *,
    addr_bytes: int = 4, size_bytes: int = 4, data_format_identifier: int = 0x00,
) -> bytes:
    """RequestDownload (0x34) — тестер сообщает ЭБУ адрес и размер области,
    КУДА собирается писать (запись прошивки ИЗ инструмента В ЭБУ)."""
    fmt_id = encode_addr_and_len_format_id(addr_bytes, size_bytes)
    return (
        bytes([0x34, data_format_identifier & 0xFF, fmt_id])
        + memory_address.to_bytes(addr_bytes, "big")
        + memory_size.to_bytes(size_bytes, "big")
    )


def req_request_upload(
    memory_address: int, memory_size: int, *,
    addr_bytes: int = 4, size_bytes: int = 4, data_format_identifier: int = 0x00,
) -> bytes:
    """RequestUpload (0x35) — тестер сообщает ЭБУ адрес и размер области,
    ОТКУДА собирается читать (чтение текущей прошивки/калибровки ИЗ ЭБУ)."""
    fmt_id = encode_addr_and_len_format_id(addr_bytes, size_bytes)
    return (
        bytes([0x35, data_format_identifier & 0xFF, fmt_id])
        + memory_address.to_bytes(addr_bytes, "big")
        + memory_size.to_bytes(size_bytes, "big")
    )


def req_transfer_data(block_sequence_counter: int, chunk: bytes = b"") -> bytes:
    """TransferData (0x36). При записи (после RequestDownload) chunk — это
    сами байты прошивки для этого блока. При чтении (после RequestUpload)
    chunk обычно пустой — тело ответа ЭБУ вернёт запрошенные данные.
    block_sequence_counter — счётчик 0x01-0xFF, после 0xFF наматывается на
    0x00 и продолжается (ISO 14229-1 §11.3.5.2)."""
    return bytes([0x36, block_sequence_counter & 0xFF]) + chunk


def req_request_transfer_exit(transfer_request_parameter: bytes = b"") -> bytes:
    """RequestTransferExit (0x37) — завершение передачи (после последнего
    TransferData). Параметр обычно пустой, но некоторые ЭБУ ждут в нём
    контрольную сумму переданных данных — тогда её передают сюда явно."""
    return bytes([0x37]) + transfer_request_parameter


# Небольшой curated-набор часто поддерживаемых идентов (ISO 14229-1 Annex F) —
# используется кнопкой "Прочитать иденты" как разумный набор по умолчанию,
# чтобы не спамить ЭБУ полным перебором 0xF180-0xF19F.
DEFAULT_IDENT_DIDS = [
    0xF190,  # VIN
    0xF191,  # Vehicle Manufacturer ECU Hardware Number
    0xF181,  # Application Software Identification
    0xF182,  # Application Data Identification
    0xF180,  # Boot Software Identification
    0xF187,  # Vehicle Manufacturer Spare Part Number
    0xF188,  # Vehicle Manufacturer ECU Software Number
    0xF18A,  # System Supplier Identifier
    0xF18C,  # ECU Serial Number
    0xF192,  # System Supplier ECU Hardware Number
    0xF194,  # System Supplier ECU Software Number
    0xF197,  # System Name Or Engine Type
]
