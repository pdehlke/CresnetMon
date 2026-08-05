"""Groups CresnetProtocol Message events into bursts correlated with a
single physical action (e.g. one keypad button press). Ground layer for
the labeling/capture mode described in STRATEGY.md's "Labeling / capture
mode" section (tasks 9-12); nothing above this module exists yet.

Pure logic - no I/O, no threads, no wall-clock reads. Callers supply
timestamps (float seconds, e.g. from time.monotonic()) so this stays
deterministically testable with synthetic event sequences, the same way
protocol.py stays pure by not touching the serial port itself.

PollTick is never signal for burst timing, per STRATEGY.md: it's routine
bus polling, not evidence something happened. Only Message events open or
extend a window.
"""

from dataclasses import dataclass

from cresnetmon.protocol import Message, PollTick, ProtocolEvent

DEFAULT_SILENCE_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Burst:
    """One completed burst: every Message captured between the window
    opening and the silence timeout that closed it."""

    messages: tuple[Message, ...]
    opened_at: float
    closed_at: float


class BurstGrouper:
    """Feed every ProtocolEvent to `feed()` as it arrives, and call
    `check()` on every polling tick - including ticks where nothing
    arrived - so a silence timeout can close an open window with no new
    event to trigger it. Caller contract: `check()` must run at roughly
    the same cadence `feed()` is fed at (e.g. both driven by one
    tk.after() loop), or a window can sit open long past the silence
    threshold before anyone notices.

    Armed/disarmed is a task-11 UI concern, not this class's: it only
    tracks "is a window currently open", not "should I be watching at
    all" - the caller decides whether to call feed()/check() in the first
    place.
    """

    def __init__(self, silence_seconds: float = DEFAULT_SILENCE_SECONDS) -> None:
        self._silence_seconds = silence_seconds
        self._messages: list[Message] = []
        self._opened_at: float | None = None
        self._last_message_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def feed(self, event: ProtocolEvent, now: float) -> None:
        """Process one event. A Message opens a window if none is open,
        or extends one already open; it never closes one - only check()
        does. PollTick is always ignored."""
        if isinstance(event, PollTick):
            return
        self._append_message(event, now)

    def check(self, now: float) -> Burst | None:
        """Call on every polling tick. Closes and returns the open burst
        if the silence window has elapsed since its last Message."""
        if self._opened_at is None or self._last_message_at is None:
            return None
        if now - self._last_message_at < self._silence_seconds:
            return None
        return self._close(self._opened_at, now)

    def reset(self) -> None:
        """Discard any in-progress burst without emitting it (e.g. on
        Disarm)."""
        self._messages = []
        self._opened_at = None
        self._last_message_at = None

    def _append_message(self, message: Message, now: float) -> None:
        self._messages.append(message)
        if self._opened_at is None:
            self._opened_at = now
        self._last_message_at = now

    def _close(self, opened_at: float, now: float) -> Burst:
        burst = Burst(messages=tuple(self._messages), opened_at=opened_at, closed_at=now)
        self.reset()
        return burst
