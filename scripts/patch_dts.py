#!/usr/bin/env python3
"""Patch Newifi D2 DTS: disable PCIe/WiFi and USB."""
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else \
    "target/linux/ramips/dts/mt7621_d-team_newifi-d2.dts"

s = open(path, encoding="utf-8").read()

# Remove PCIe enable block and WiFi child nodes
s = re.sub(r'&pcie \{\n\tstatus = "okay";\n\};\n\n', '', s)
s = re.sub(r'&pcie0 \{.*?\n\};\n\n', '', s, flags=re.S)
s = re.sub(r'&pcie1 \{.*?\n\};\n\n', '', s, flags=re.S)

# Remove USB LED node (depends on xhci port phandles)
s = re.sub(r'\n\tusb \{.*?\n\t\};\n', '\n', s, flags=re.S)

# Append disabled nodes
s += '\n&pcie {\n\tstatus = "disabled";\n};\n\n&xhci {\n\tstatus = "disabled";\n};\n'

open(path, "w", encoding="utf-8").write(s)
print("DTS patched:", path)
