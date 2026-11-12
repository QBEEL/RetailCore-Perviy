"""Transport-independent state for incremental payment synchronization."""
from dataclasses import dataclass, field

from .models import Payment


@dataclass(frozen=True)
class Cursor:
    epoch: str
    revision: int
    access: str
    metadata_dirty: bool = False


@dataclass(frozen=True)
class Changes:
    cursor: Cursor
    upserted: list[Payment] = field(default_factory=list)
    removed_ids: list[int] = field(default_factory=list)
    metadata: bool = False
    reset: bool = False
    more: bool = False
