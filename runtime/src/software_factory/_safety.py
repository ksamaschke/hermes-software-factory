"""Shared bounded traversal primitives for authority-bearing snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field

# These limits are deliberately shared by policy canonicalization and routing
# readback.  Normal project policies are orders of magnitude below them.
MAX_TRAVERSAL_DEPTH = 64
MAX_TRAVERSAL_NODES = 10_000
MAX_CONTAINER_ITEMS = 4_096
MAX_STRING_BYTES = 1_048_576
MAX_ENCODED_BYTES = 4_194_304


class TraversalBudgetError(ValueError):
    """Raised when a bounded authority snapshot is unsafe to traverse."""


@dataclass
class TraversalBudget:
    """Track aggregate work and identities for one complete object graph."""

    max_depth: int = MAX_TRAVERSAL_DEPTH
    max_nodes: int = MAX_TRAVERSAL_NODES
    max_container_items: int = MAX_CONTAINER_ITEMS
    max_string_bytes: int = MAX_STRING_BYTES
    max_encoded_bytes: int = MAX_ENCODED_BYTES
    nodes: int = 0
    string_bytes: int = 0
    encoded_bytes: int = 0
    identities: set[int] = field(default_factory=set)
    # Trusted immutable model snapshots may intentionally share a child model
    # (for example, ClaimedRun.run and Lease.run).  Cache those snapshots while
    # still rejecting recursive active traversal.
    model_snapshots: dict[int, object] = field(default_factory=dict)
    active_models: set[int] = field(default_factory=set)

    def enter(
        self,
        value: object,
        *,
        depth: int,
        label: str,
        length: int,
    ) -> None:
        """Reserve one exact admitted container/model before descending."""

        if depth > self.max_depth:
            raise TraversalBudgetError(f"{label} exceeds the snapshot nesting limit")
        if length > self.max_container_items:
            raise TraversalBudgetError(f"{label} exceeds the bounded container length")
        # Pydantic uses the process-wide immutable empty tuple for many
        # independent default fields.  It cannot contain descendants or
        # mutate, so sharing that sentinel is not an expandable alias.
        if not (type(value) is tuple and length == 0):
            identity = id(value)
            if identity in self.identities:
                raise TraversalBudgetError(
                    f"{label} contains a cycle or repeated container/model identity"
                )
            self.identities.add(identity)
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise TraversalBudgetError(f"{label} exceeds the aggregate node limit")
        # Charge a small structural representation for every admitted node so
        # a graph made only of empty containers cannot bypass the byte budget.
        self.charge_encoded(1, label)

    def charge_string(self, value: str, label: str) -> None:
        """Charge one exact built-in string by its UTF-8 representation."""

        try:
            size = len(value.encode("utf-8"))
        except (UnicodeError, RecursionError, MemoryError) as exc:
            raise TraversalBudgetError(f"{label} is not safely encodable") from exc
        self.string_bytes += size
        if self.string_bytes > self.max_string_bytes:
            raise TraversalBudgetError(
                f"{label} exceeds the aggregate string-byte limit"
            )
        self.charge_encoded(size, label)

    def charge_scalar(self, value: object, label: str) -> None:
        """Charge a previously exact-admitted JSON scalar."""

        if value is None:
            self.charge_encoded(4, label)
        elif type(value) is bool:
            self.charge_encoded(5 if value else 4, label)
        elif type(value) is int:
            try:
                size = len(str(value).encode("ascii"))
            except (ValueError, UnicodeError, RecursionError, MemoryError) as exc:
                raise TraversalBudgetError(f"{label} is not safely encodable") from exc
            self.charge_encoded(size, label)
        elif type(value) is float:
            self.charge_encoded(24, label)
        elif type(value) is str:
            self.charge_string(value, label)
        else:
            raise TraversalBudgetError(f"{label} is not an admitted scalar")

    def charge_encoded(self, size: int, label: str) -> None:
        """Charge encoded bytes without allowing integer overflow or negatives."""

        if size < 0 or self.encoded_bytes > self.max_encoded_bytes - size:
            raise TraversalBudgetError(
                f"{label} exceeds the aggregate encoded-byte limit"
            )
        self.encoded_bytes += size


__all__ = [
    "MAX_CONTAINER_ITEMS",
    "MAX_ENCODED_BYTES",
    "MAX_STRING_BYTES",
    "MAX_TRAVERSAL_DEPTH",
    "MAX_TRAVERSAL_NODES",
    "TraversalBudget",
    "TraversalBudgetError",
]
