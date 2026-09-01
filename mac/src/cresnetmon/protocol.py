"""Cresnet RS-485 protocol state machine.

Pure translation of the byte-level parser in the original Windows app —
`CresNetProcessByte`/`ShowMessage` in `CresnetMon/MainForm.cs:141-223`. No
serial I/O, no UI, no clock reads: feed it bytes one at a time, get back
events. `Message.read_at` exists as a field but is always `None` coming out
of here - wall-clock timestamps are the caller's job (see `Message`'s
docstring and STRATEGY.md task 13), same as `burst.py` taking
caller-supplied timestamps instead of reading its own clock.

Device-ID filtering (0 = all, else match a specific device) is *not* done
here, unlike the original where `ShowMessage` filtered before displaying.
That's caller/UI state, not protocol state — every parsed `Message` is
emitted; the caller decides what to show. See STRATEGY.md.
"""

from dataclasses import dataclass
from enum import Enum, auto

MASTER_ADDR = 0x02
MIN_MSG_ADDR = MASTER_ADDR
MAX_MSG_ADDR = 0xFE
MAX_MSG_SIZE = 30


class _State(Enum):
    SEARCHING = auto()
    READY = auto()
    ADDRESSED = auto()
    PAYLOAD = auto()


@dataclass(frozen=True, slots=True)
class PollTick:
    """A polling cycle was observed on the bus; carries no payload.

    Mirrors the `DisplayMessage(null, ...)` call in `MainForm.cs:190`.
    """

    cycle: int


@dataclass(frozen=True, slots=True)
class Message:
    """A complete, parsed message.

    `cycle` is the polling-cycle count *at the time of* the message (not
    incremented by the message itself — only `PollTick` advances it),
    matching the "ID" column in the original UI.

    `dev_id` is `send_id` when `to_master` is true — an *inference* from
    the last non-master address seen in READY state, not a byte this frame
    itself carried. `dest_id` is the byte actually read as this frame's
    destination, so a later reader can always tell inferred source from
    read destination: when `to_master`, `dest_id == MASTER_ADDR` and
    `dev_id` is the inference; otherwise `dest_id == dev_id`, read directly.

    `raw` is the frame exactly as it crossed the bus - destination byte,
    size byte, then payload - not just the payload `text` already encodes.
    Added in STRATEGY.md task 13 to close a gap the first live capture
    exposed: `text` alone can't be re-decoded from scratch since it drops
    the address/size framing around the payload.

    `read_at` is a wall-clock (`time.time()`) timestamp. This module never
    sets it - see the module docstring's purity note - so it defaults to
    `None` here; the caller (serial_io.py's `SerialReader`) attaches the
    real value via `dataclasses.replace()` once a `Message` is produced.
    """

    cycle: int
    text: str
    dev_id: int
    to_master: bool
    dest_id: int = 0
    raw: bytes = b""
    read_at: float | None = None


type ProtocolEvent = PollTick | Message


class CresnetProtocol:
    """Byte-at-a-time Cresnet bus parser.

    One instance represents a monitoring session's parse state — roughly
    the `m_state`/`m_bDestId`/`m_bSendId`/`m_bPollId`/`m_iMsgCnt`/
    `m_lstMessage` fields of the original `MainForm`. Call `feed()` for
    each byte read from the serial port.
    """

    def __init__(self) -> None:
        self._state = _State.SEARCHING
        self._dest_id = 0
        self._send_id = 0
        self._poll_id = 0
        self._msg_size = 0
        self._frame_size_byte = 0
        self._message: list[int] = []
        self.msg_count = 0

    def feed(self, byte: int) -> ProtocolEvent | None:
        """Process one byte (0-255); return an event if one was produced."""
        match self._state:
            case _State.SEARCHING:
                if byte == 0:
                    self._state = _State.READY
                return None

            case _State.READY:
                if MIN_MSG_ADDR <= byte < MAX_MSG_ADDR:
                    self._state = _State.ADDRESSED
                    self._dest_id = byte
                    if byte != MASTER_ADDR:
                        self._send_id = byte
                elif byte != 0:
                    self._state = _State.SEARCHING
                # byte == 0 leaves state in READY
                return None

            case _State.ADDRESSED:
                if byte > MAX_MSG_SIZE:
                    self._state = _State.SEARCHING
                    return None
                if byte != 0:
                    self._msg_size = byte
                    # _msg_size counts down as payload bytes arrive; this
                    # copy survives to _finish_message() for `raw`.
                    self._frame_size_byte = byte
                    self._state = _State.PAYLOAD
                    return None
                return self._end_addressed_frame()

            case _State.PAYLOAD:
                self._message.append(byte)
                self._msg_size -= 1
                if self._msg_size == 0:
                    return self._finish_message()
                return None

        return None  # unreachable; keeps type checkers happy on the match

    def start(self) -> None:
        """Reset parse state for a fresh monitoring run.

        Mirrors the reset `btnStart_Click` does (`MainForm.cs:284-285`):
        drops in-flight state/buffer, but *not* the poll reference or
        counts — those persist across start/stop like the original.
        """
        self._state = _State.SEARCHING
        self._message = []
        self._msg_size = 0
        self._frame_size_byte = 0

    def clear_counts(self, *, keep_poll_reference: bool) -> None:
        """Reset the display counter, mirroring `btnClear_Click` (MainForm.cs:292-298).

        `keep_poll_reference=False` also drops the poll-cycle reference
        device id, matching the original's rule that the reference is only
        cleared while stopped.
        """
        self.msg_count = 0
        if not keep_poll_reference:
            self._poll_id = 0

    def _end_addressed_frame(self) -> PollTick | None:
        """Zero-size byte while Addressed: end of an empty (poll) frame."""
        self._state = _State.READY
        if self._dest_id == MASTER_ADDR:
            return None
        if self._poll_id == 0:
            self._poll_id = self._dest_id
        if self._dest_id != self._poll_id:
            return None
        self.msg_count += 1
        return PollTick(cycle=self.msg_count)

    def _finish_message(self) -> Message:
        dest = self._send_id if self._dest_id == MASTER_ADDR else self._dest_id
        text = " ".join(f"{b:02X}" for b in self._message)
        raw = bytes([self._dest_id, self._frame_size_byte, *self._message])
        event = Message(
            cycle=self.msg_count,
            text=text,
            dev_id=dest,
            to_master=self._dest_id == MASTER_ADDR,
            dest_id=self._dest_id,
            raw=raw,
        )
        self._message = []
        self._state = _State.READY
        return event
