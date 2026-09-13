import ast
import importlib
import sys

files = ["can_dtc_reader_gui.py", "uds_tx.py", "uds_decoder.py"]
for f in files:
    with open(f, encoding="utf-8") as fh:
        ast.parse(fh.read())
print("AST_OK")

sys.path.insert(0, ".")
import uds_tx
import uds_decoder
importlib.reload(uds_tx)
importlib.reload(uds_decoder)
print("uds_tx / uds_decoder import OK")

# Проверим новые билдеры и словари не падают
print(uds_tx.req_read_dtc(0x02, 0xFF).hex())
print(uds_tx.req_diag_session_control(0x03).hex())
print(uds_tx.req_ecu_reset(0x01).hex())
print(uds_tx.req_security_access(0x01).hex())
print(uds_tx.req_write_did(0xF190, b"\x41\x42").hex())
print(uds_tx.req_tester_present().hex())
print(uds_tx.req_routine_control(0x01, 0x1234).hex())
print(sorted(uds_decoder.SESSION_NAMES.items()))
print(sorted(uds_decoder.RESET_TYPE_NAMES.items()))
print(sorted(uds_decoder.ROUTINE_CONTROL_NAMES.items()))

# Импортировать сам can_dtc_reader_gui.py целиком (это выполнит весь
# module-level код: импорты, определение классов, SERVICE_DEFS) — если тут
# ошибка (например опечатка в имени функции/класса), она вскроется прямо
# сейчас, без необходимости открывать окно вручную.
import can_dtc_reader_gui as gui
importlib.reload(gui)
print("can_dtc_reader_gui import OK")
print("SERVICE_DEFS keys:", [d["key"] for d in gui.SERVICE_DEFS])
