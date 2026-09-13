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
importlib.reload(uds_tx)
print("build_ff:", uds_tx.build_ff(bytes(range(1, 12))).hex())
print("build_cf:", uds_tx.build_cf(1, bytes([1, 2, 3])).hex())
print("MAX_MULTI_FRAME_PAYLOAD:", uds_tx.MAX_MULTI_FRAME_PAYLOAD)

import can_dtc_reader_gui as gui
importlib.reload(gui)
print("can_dtc_reader_gui import OK")
