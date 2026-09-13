#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика: что видно в реестре как J2534-устройства."""
import sys
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bridge_common import find_j2534_devices

devices = find_j2534_devices()
print(f"Найдено устройств: {len(devices)}")
for label, dll in devices:
    print(f"  {label!r} -> {dll}")
