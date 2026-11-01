"""Port and arc connection helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionSpec:
    """Serializable connection between two unit ports."""

    src_unit: str
    src_port: str
    dest_unit: str
    dest_port: str
    auto_create_translator: bool = False


def connection(
    src_unit: str,
    src_port: str,
    dest_unit: str,
    dest_port: str,
    *,
    auto_create_translator: bool = False,
) -> dict[str, object]:
    """Return the session-spec dictionary for one connection."""

    return ConnectionSpec(
        src_unit=src_unit,
        src_port=src_port,
        dest_unit=dest_unit,
        dest_port=dest_port,
        auto_create_translator=auto_create_translator,
    ).__dict__.copy()
