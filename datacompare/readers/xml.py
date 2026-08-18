"""XML reader: flatten repeating records into tabular rows.

The test case names a `record_path` (an XPath selecting the repeating record
element). Each matched record is flattened into a flat dict where nested
elements become dotted column names and attributes become `path@attr` columns.
Repeated sibling elements are indexed (`items.item[0]`, `items.item[1]`).
Namespaces are stripped by default so paths are predictable across files.

The flattened rows are handed to the engine's `read_rows`, so from that point on
XML data flows through the exact same comparison path as CSV.
"""

from __future__ import annotations

from typing import Any, Dict, List

from lxml import etree

from ..config import Source
from ..engines.base import Engine


def _strip_namespaces(root: "etree._Element") -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        # Also strip namespaced attribute keys.
        for name in list(el.attrib.keys()):
            if "}" in name:
                el.attrib[name.split("}", 1)[1]] = el.attrib.pop(name)


def _flatten(el: "etree._Element", prefix: str, out: Dict[str, Any]) -> None:
    # Attributes of this element.
    for attr, val in el.attrib.items():
        key = ("%s@%s" % (prefix, attr)) if prefix else ("@%s" % attr)
        out[key] = val

    children = list(el)
    if not children:
        # Leaf: record its text (may be None/empty).
        text = el.text.strip() if el.text and el.text.strip() else (el.text or None)
        if prefix:
            # Only set if not already occupied by an attribute-only element.
            out.setdefault(prefix, text if text != "" else None)
        return

    # Group children by tag to index repeats.
    counts: Dict[str, int] = {}
    tag_totals: Dict[str, int] = {}
    for c in children:
        tag_totals[c.tag] = tag_totals.get(c.tag, 0) + 1
    for c in children:
        tag = c.tag
        if tag_totals[tag] > 1:
            idx = counts.get(tag, 0)
            counts[tag] = idx + 1
            child_prefix = "%s.%s[%d]" % (prefix, tag, idx) if prefix else "%s[%d]" % (tag, idx)
        else:
            child_prefix = "%s.%s" % (prefix, tag) if prefix else tag
        _flatten(c, child_prefix, out)


def flatten_records(
    xml_bytes: bytes, record_path: str, strip_ns: bool = True
) -> List[Dict[str, Any]]:
    parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml_bytes, parser=parser)
    if strip_ns:
        _strip_namespaces(root)
    records = root.xpath(record_path)
    if not records:
        raise ValueError(
            "record_path '%s' matched no elements in the XML" % record_path
        )
    rows: List[Dict[str, Any]] = []
    for rec in records:
        row: Dict[str, Any] = {}
        _flatten(rec, "", row)
        rows.append(row)
    return rows


def read(engine: Engine, source: Source) -> Any:
    if not source.record_path:
        raise ValueError("xml source requires a record_path")
    with open(source.path, "rb") as fh:
        data = fh.read()
    rows = flatten_records(data, source.record_path)
    return engine.read_rows(rows)
