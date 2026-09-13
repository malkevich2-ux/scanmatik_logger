#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Быстрая проверка, что App() собирается целиком (все 5 вкладок) без ошибок."""
import sys
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import can_dtc_reader_gui as gui

app = gui.App()
app.withdraw()
print("App OK, tabs:", [
    app.live_tab.__class__.__name__,
    app.request_tab.__class__.__name__,
    app.monitor_tab.__class__.__name__,
    app.all_dtc_tab.__class__.__name__,
    app.reference_tab.__class__.__name__,
])
print("monitor_tab.live_tab is app.live_tab:", app.monitor_tab.live_tab is app.live_tab)
print("all_dtc_tab.live_tab is app.live_tab:", app.all_dtc_tab.live_tab is app.live_tab)
print("frame listeners registered:", len(app.live_tab._frame_listeners))
assert len(app.live_tab._frame_listeners) == 2, "monitor_tab и all_dtc_tab должны быть подписаны на сырые кадры"

# Кнопки опроса и очистки должны быть выключены, пока нет подключения к ЭБУ.
assert str(app.all_dtc_tab.scan_all_btn["state"]) == "disabled"
assert str(app.all_dtc_tab.scan_selected_btn["state"]) == "disabled"
assert str(app.all_dtc_tab.abort_btn["state"]) == "disabled"
assert str(app.all_dtc_tab.clear_selected_btn["state"]) == "disabled"
assert str(app.all_dtc_tab.clear_all_btn["state"]) == "disabled"
print("all_dtc_tab: кнопки корректно выключены без подключения")

# Справочник Volvo должен загрузиться (данные из dtc_reference_ru.json).
assert len(gui.COMPONENT_RU) > 0, "справочник компонентов пуст — dtc_reference_ru.json не найден/не читается"
assert len(gui.FAILURE_TYPE_RU) > 0, "справочник типов неисправностей пуст"
assert len(app.reference_tab.comp_tree.get_children()) > 0
assert len(app.reference_tab.ftype_tree.get_children()) == len(gui.FAILURE_TYPE_RU)
print(f"reference_tab: справочник загружен ({len(gui.COMPONENT_RU)} компонентов, {len(gui.FAILURE_TYPE_RU)} типов неисправностей)")

# add_dtc теперь должен добавлять колонки "Активна" и "Тип неисправности".
app.live_tab.tables.add_dtc(0x23, "500001", 0x01, "C1000", ["testFailed"], "12:00:00")
row = app.live_tab.tables.dtc_tree.item(app.live_tab.tables.dtc_tree.get_children()[0], "values")
assert row[4] == "Да", f"ожидался статус 'Да' (активна) в колонке 'active', получено {row}"
print("EcuTables.add_dtc: колонка 'Активна' работает —", row)

app._on_close()
print("frame listeners after close:", len(app.live_tab._frame_listeners))
assert len(app.live_tab._frame_listeners) == 0
print("DONE")
