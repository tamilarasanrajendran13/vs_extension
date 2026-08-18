"""Format readers. Each returns an engine-native frame via the given engine."""

from __future__ import annotations

from typing import Any

from ..config import Source
from ..engines.base import Engine
from . import csv as csv_reader
from . import xml as xml_reader


def read_source(engine: Engine, data_format: str, source: Source) -> Any:
    if data_format == "csv":
        return csv_reader.read(engine, source)
    if data_format == "xml":
        return xml_reader.read(engine, source)
    raise ValueError("unsupported format: %s" % data_format)
