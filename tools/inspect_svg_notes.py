from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


NS = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def main() -> None:
    for arg in sys.argv[1:]:
        path = Path(arg)
        root = ET.parse(path).getroot()
        uses = Counter()
        images = Counter()
        classes = Counter()
        in_defs: set[int] = set()
        for el in root.iter():
            if local_name(el.tag) == "defs":
                for child in el.iter():
                    in_defs.add(id(child))
        for el in root.iter():
            if id(el) in in_defs:
                continue
            cls = el.attrib.get("class")
            if cls:
                classes[cls] += 1
            if local_name(el.tag) == "use":
                href = el.attrib.get(f"{{{NS['xlink']}}}href") or el.attrib.get("href") or ""
                if href.startswith("#notes-"):
                    uses[href.lstrip("#")] += 1
            elif local_name(el.tag) == "image":
                href = el.attrib.get(f"{{{NS['xlink']}}}href") or el.attrib.get("href") or ""
                if "notes_" in href:
                    images[Path(href).name] += 1
        print(path.name)
        print("  note uses:", dict(sorted(uses.items())))
        print("  note images:", dict(sorted(images.items())))
        interesting = {
            k: v
            for k, v in sorted(classes.items())
            if k in {"slide", "slide-critical", "decoration", "decoration-critical", "event-flag"}
        }
        print("  classes:", interesting)


if __name__ == "__main__":
    main()
