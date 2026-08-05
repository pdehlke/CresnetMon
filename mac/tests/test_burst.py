"""Tests for cresnetmon.burst.BurstGrouper using synthetic timestamps - no
real sleeps, fully deterministic."""

from cresnetmon.burst import BurstGrouper
from cresnetmon.protocol import Message, PollTick

MSG_A = Message(cycle=1, text="11 22", dev_id=0x05, to_master=True)
MSG_B = Message(cycle=1, text="AA BB", dev_id=0x07, to_master=False)


def test_poll_tick_alone_never_opens_a_window() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    grouper.feed(PollTick(cycle=1), now=0.0)

    assert grouper.is_open is False
    assert grouper.check(now=10.0) is None


def test_check_with_nothing_ever_fed_returns_none() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    assert grouper.check(now=100.0) is None
    assert grouper.is_open is False


def test_single_message_opens_window_and_closes_after_silence() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    grouper.feed(MSG_A, now=10.0)
    assert grouper.is_open is True
    assert grouper.check(now=10.3) is None  # still within the silence window

    burst = grouper.check(now=10.5)

    assert burst is not None
    assert burst.messages == (MSG_A,)
    assert burst.opened_at == 10.0
    assert burst.closed_at == 10.5
    assert grouper.is_open is False


def test_multiple_messages_within_window_join_one_burst() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    grouper.feed(MSG_A, now=10.0)
    grouper.feed(MSG_B, now=10.2)
    assert grouper.check(now=10.4) is None  # 0.2s since MSG_B, still quiet enough

    burst = grouper.check(now=10.7)  # 0.5s since MSG_B

    assert burst is not None
    assert burst.messages == (MSG_A, MSG_B)
    assert burst.opened_at == 10.0
    assert burst.closed_at == 10.7


def test_poll_tick_between_messages_is_ignored_for_timing_and_content() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    grouper.feed(MSG_A, now=10.0)
    grouper.feed(PollTick(cycle=2), now=10.4)  # would reset silence if it counted
    burst = grouper.check(now=10.5)  # 0.5s since MSG_A, tick doesn't extend it

    assert burst is not None
    assert burst.messages == (MSG_A,)


def test_reset_discards_open_window() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)
    grouper.feed(MSG_A, now=10.0)

    grouper.reset()

    assert grouper.is_open is False
    assert grouper.check(now=100.0) is None  # old timestamps don't resurrect it


def test_sequential_bursts_are_independent() -> None:
    grouper = BurstGrouper(silence_seconds=0.5)

    grouper.feed(MSG_A, now=10.0)
    first = grouper.check(now=10.5)
    assert first is not None and first.messages == (MSG_A,)

    grouper.feed(MSG_B, now=20.0)
    assert grouper.is_open is True
    second = grouper.check(now=20.5)

    assert second is not None
    assert second.messages == (MSG_B,)
    assert second.opened_at == 20.0
