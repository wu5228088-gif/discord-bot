from __future__ import annotations

import sys
import zipfile
import xml.etree.ElementTree as ET


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def main() -> None:
    path = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//a:t", NS)))

        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        for row in root.findall(".//a:row", NS)[:limit]:
            values = []
            for cell in row.findall("a:c", NS):
                ref = cell.attrib.get("r", "")
                cell_type = cell.attrib.get("t")
                value_node = cell.find("a:v", NS)
                formula_node = cell.find("a:f", NS)
                value = ""
                if value_node is not None and value_node.text is not None:
                    value = value_node.text
                    if cell_type == "s":
                        value = shared[int(value)]
                elif formula_node is not None and formula_node.text:
                    value = "=" + formula_node.text
                values.append(f"{ref}={value}")
            if values:
                print(" | ".join(values))


if __name__ == "__main__":
    main()
