import ast
import importlib
import sys

with open("can_dtc_reader_gui.py", encoding="utf-8") as fh:
    ast.parse(fh.read())
print("AST_OK")

sys.path.insert(0, ".")
import can_dtc_reader_gui as gui
importlib.reload(gui)
print("import OK")
print("has FileTab:", hasattr(gui, "FileTab"))
print("SERVICE_DEFS:", [d["key"] for d in gui.SERVICE_DEFS])
print("RequestTab has queue methods:", all(hasattr(gui.RequestTab, m) for m in (
    "_on_add_to_queue", "_on_remove_from_queue", "_on_clear_queue", "_move_queue_item",
    "_on_send_queue_click", "_send_one_blocking", "_refresh_queue_view",
)))
