"""One CIP session and everything that serialises access to its slot.

The bridge used to hold four dicts keyed by link name: the clients, the locks,
the last-activity stamps and the join tables. Every operation looked up the same
key in two or three of them, `_idle_watch` touched three in one loop body, and
the A/V controller had to be handed three of the four separately because there
was no object to pass. This is that state as the one thing it always was.

It lives in its own module rather than in bridge.py because av.py needs it too,
and bridge.py already imports av.py.
"""

from __future__ import annotations

import asyncio

from .cip import CipClient
from .const import IDLE_RETURN_SECONDS, Load


class Link:
    """A CIP session, the lock that serialises its slot, and its join table."""

    def __init__(self, name: str, client: CipClient) -> None:
        self.name = name
        self.client = client
        # Held for the length of an operation, so what is serialised is the
        # subsystem the slot is in and not merely the presses. Audio takes this
        # same lock, which is what stops a lighting write going out while the
        # slot sits in A/V.
        self.lock = asyncio.Lock()
        # join -> load. Aliases resolve to the same load, which is how one
        # physical light on five buttons stays one entity.
        self.by_join: dict[int, Load] = {}
        self._busy_until = 0.0

    def touch(self, hold_seconds: float = 0.0) -> None:
        """Mark the link busy, optionally pushing the idle deadline out."""
        self._busy_until = asyncio.get_running_loop().time() + hold_seconds

    @property
    def connected(self) -> bool:
        """Registered and synced, and therefore worth reading or writing.

        Both halves matter: a registered slot that has not yet had its
        subsystem dump is connected but knows nothing, and reporting its loads
        off on that basis is the 2026-09-15 failure.
        """
        return bool(self.client.connected and self.client.synced)

    @property
    def idle(self) -> bool:
        """Nothing has touched this link for long enough to send it home."""
        return asyncio.get_running_loop().time() - self._busy_until >= IDLE_RETURN_SECONDS
