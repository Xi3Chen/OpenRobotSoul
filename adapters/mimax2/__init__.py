"""Xiaomi MiMax2 Linux phone adapter.

The factory is imported lazily so importing ``robotsoul.models`` cannot create
a package cycle through the adapter registry.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.mimax2.adapter import MiMax2Adapter
def create_adapter(profile):
    from adapters.mimax2.factory import create_adapter as factory

    return factory(profile)


__all__ = ["create_adapter"]
