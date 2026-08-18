"""CSV reader. Delegates the actual file read to the engine so the frame is
native to whichever backend the test case selected."""

from __future__ import annotations

from typing import Any

from ..config import Source
from ..engines.base import Engine


def read(engine: Engine, source: Source) -> Any:
    return engine.read_csv(source)
