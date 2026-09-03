"""Offline tests for the Crestron CIP bridge. No network, no Home Assistant.

The expected byte strings are not invented: they are the exact frames recorded in
the proven control-path transcripts, so a change to the codec that still
round-trips but no longer matches the wire will fail here.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

import pytest

# Import the three network- and HA-free modules without executing the package's
# __init__.py, which pulls in Home Assistant and voluptuous. Pre-registering a
# stand-in parent package gives the relative imports inside cip.py and bridge.py
# something to resolve against.
_PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "crestron_cip"
_parent = types.ModuleType("crestron_cip")
_parent.__path__ = [str(_PKG)]
sys.modules.setdefault("crestron_cip", _parent)

const = importlib.import_module("crestron_cip.const")
_bridge_mod = importlib.import_module("crestron_cip.bridge")
_cip_mod = importlib.import_module("crestron_cip.cip")

CONFIRM_ATTEMPTS = _bridge_mod.CONFIRM_ATTEMPTS
CrestronBridge = _bridge_mod.CrestronBridge
CrestronError = _bridge_mod.CrestronError
decode_digitals = _cip_mod.decode_digitals
digital_packet = _cip_mod.digital_packet
registration = _cip_mod.registration

# ---- codec ----------------------------------------------------------------


def test_press_bytes_match_the_recorded_frames():
    # From crestron-xpanel-control-path.md: pressing digital join 24.
    assert digital_packet(24, True).hex(" ") == "05 00 06 00 00 03 00 17 00"
    assert digital_packet(24, False).hex(" ") == "05 00 06 00 00 03 00 17 80"
    # From crestron-tsw-panel-control-path.md: the two live panel-slot tests.
    assert digital_packet(245, True).hex(" ") == "05 00 06 00 00 03 00 f4 00"
    assert digital_packet(241, True).hex(" ") == "05 00 06 00 00 03 00 f0 00"


def test_joins_above_255_use_the_high_byte_without_disturbing_the_state_bit():
    # d992 is the Dining zone-select join, well past one byte.
    packet = digital_packet(992, True)
    assert packet[7] == (991 & 0xFF)
    assert packet[8] == (991 >> 8) & 0x7F
    assert digital_packet(992, False)[8] == (((991 >> 8) & 0x7F) | 0x80)


def test_digital_round_trip():
    # The decoder is fed payload[4:], which is packet[7:]: three header bytes,
    # then the 00 00 <len> payload prefix, then the datatype byte.
    for join in (1, 24, 103, 245, 992, 1411):
        for pressed in (True, False):
            body = digital_packet(join, pressed)[7:]
            assert decode_digitals(body) == [(join, 1 if pressed else 0)]


def test_packed_digital_frame_yields_every_join():
    # The AADS packs several joins into one frame; reading only the first loses
    # the rest, which is a bug this project has already had once.
    body = b"".join(digital_packet(j, True)[7:] for j in (106, 108, 128))
    assert decode_digitals(body) == [(106, 1), (108, 1), (128, 1)]


def test_registration_packet_carries_the_ipid():
    assert registration(0x13)[8] == 0x13
    assert registration(0x03)[8] == 0x03


# ---- load table -----------------------------------------------------------


def test_table_covers_thirty_loads_and_forty_one_joins():
    assert len(const.LOADS) == 30
    mapped = [load for load in const.LOADS if load.join is not None]
    assert len(mapped) == 30  # all four Kitchen loads identified 2026-09-03
    # 41 worksheet load buttons. Island counts once here even though pressing
    # it on takes a second join (press_on): load.joins is feedback joins, and
    # press_on/press_off deliberately are not feedback joins.
    assert sum(len(load.joins) for load in const.LOADS) == 41


def test_no_canonical_join_is_one_the_alarm_keypad_shares():
    for load in const.LOADS:
        if load.link == const.LINK_AADS and load.join is not None:
            assert load.join not in const.FORBIDDEN_AADS_WRITE, load.key


def test_press_join_falls_back_to_the_canonical_join_for_ordinary_toggles():
    # 29 of the 30 loads, including three of the four Kitchen ones, are a
    # single toggle button: press_on/press_off are unset, so press_join()
    # returns the same join both ways.
    for key in ("office_pool_bath", "kitchen_range", "kitchen_pathway", "kitchen_cabinet"):
        load = const.LOADS_BY_KEY[key]
        assert load.press_on is None and load.press_off is None
        assert load.press_join(True) == load.press_join(False) == load.join


def test_island_presses_a_different_join_for_on_than_off():
    # Identified 2026-09-03: Island's channel has a raise button (27) and a
    # separate single-press fade-to-off button, and the off button (29) also
    # doubles as the on/off status join.
    island = const.LOADS_BY_KEY["kitchen_island"]
    assert island.join == 29
    assert island.press_join(True) == 27
    assert island.press_join(False) == 29
    assert island.level_join == 22
    assert set(island.press_joins) == {27, 29}


def test_alarm_aliases_are_still_accepted_for_feedback():
    # Powder and Outdoor Kitchen both carry an alias inside the forbidden range.
    # Receiving those is required; only writing them is refused.
    powder = const.LOADS_BY_KEY["dining_room_powder"]
    assert 142 in powder.aliases and 142 in const.FORBIDDEN_AADS_WRITE
    outdoor = const.LOADS_BY_KEY["outdoor_kitchen"]
    assert 144 in outdoor.aliases and 144 in const.FORBIDDEN_AADS_WRITE


def test_validate_rejects_a_forbidden_canonical_join():
    bad = const.Load("boom", "Boom", const.LINK_AADS, 146)
    original = const.LOADS
    try:
        const.LOADS = (*original, bad)
        with pytest.raises(ValueError, match="alarm keypad"):
            const._validate()
    finally:
        const.LOADS = original


def test_validate_rejects_a_forbidden_press_on_even_when_join_is_clean():
    # A load whose canonical (feedback) join is outside the alarm range can
    # still smuggle a forbidden press in through press_on/press_off. The
    # collision check has to look at press_joins, not just `join`.
    bad = const.Load("boom", "Boom", const.LINK_AADS, 200, press_on=146)
    original = const.LOADS
    try:
        const.LOADS = (*original, bad)
        with pytest.raises(ValueError, match="alarm keypad"):
            const._validate()
    finally:
        const.LOADS = original


# ---- bridge behaviour -----------------------------------------------------


class FakeClient:
    """Stands in for a registered CIP session, recording what got pressed."""

    def __init__(self, *, connected=True, synced=True):
        self.connected = connected
        self.synced = synced
        self.digital: dict[int, int] = {}
        self.presses: list[int] = []
        self.reply: bool = True

    async def async_press(self, join, hold=0.0):
        self.presses.append(join)
        if self.reply:
            # Real hardware answers with feedback; the bridge is waiting on it.
            self.digital[join] = 0 if self.digital.get(join) else 1
            self._bridge._on_digital(self._link, join, self.digital[join])


def make_bridge():
    bridge = CrestronBridge({})
    for link in (const.LINK_AADS, const.LINK_MC2E):
        fake = FakeClient()
        fake._bridge, fake._link = bridge, link
        bridge._clients[link] = fake
    return bridge


def test_turn_on_presses_once_and_turn_on_again_presses_not_at_all():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert aads.presses == [245]
    assert bridge.is_on("office_pool_bath") is True

    # Idempotence is the whole point of the toggle logic: a second turn_on must
    # not press again, or it would turn the light off.
    asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert aads.presses == [245]
    assert bridge.is_on("office_pool_bath") is True


def test_turn_off_on_an_already_off_load_does_nothing():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    asyncio.run(bridge.async_turn_off("office_pool_bath"))
    assert aads.presses == []


def test_feedback_on_an_alias_moves_the_load():
    bridge = make_bridge()
    # Outdoor Kitchen is one load on five buttons. d247 moving is the same event
    # as d104 moving, and must not produce a second, disagreeing load.
    bridge._on_digital(const.LINK_AADS, 247, 1)
    assert bridge.is_on("outdoor_kitchen") is True

    aads = bridge._clients[const.LINK_AADS]
    asyncio.run(bridge.async_turn_on("outdoor_kitchen"))
    assert aads.presses == []


def test_press_targets_the_canonical_join_never_the_forbidden_alias():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    asyncio.run(bridge.async_turn_on("dining_room_powder"))
    assert aads.presses == [102]
    assert 142 not in aads.presses
    asyncio.run(bridge.async_turn_on("outdoor_kitchen"))
    assert 144 not in aads.presses


def test_a_load_with_no_join_mapped_refuses_rather_than_guess():
    # All thirty loads are mapped now (the Kitchen four, 2026-09-03), so this
    # exercises the refusal path with a synthetic unmapped load rather than a
    # real one, the same way the four Kitchen loads worked before identification.
    bridge = make_bridge()
    ghost = const.Load("ghost", "Ghost", const.LINK_MC2E, None)
    original = dict(const.LOADS_BY_KEY)
    try:
        const.LOADS_BY_KEY["ghost"] = ghost
        assert bridge.is_available("ghost") is False
        assert bridge.is_on("ghost") is None
        with pytest.raises(CrestronError, match="no join mapped"):
            asyncio.run(bridge.async_turn_on("ghost"))
    finally:
        const.LOADS_BY_KEY.clear()
        const.LOADS_BY_KEY.update(original)


def test_a_load_on_a_disconnected_link_refuses():
    bridge = make_bridge()
    bridge._clients[const.LINK_AADS].synced = False
    assert bridge.is_on("office_pool_bath") is None
    with pytest.raises(CrestronError, match="not connected"):
        asyncio.run(bridge.async_turn_on("office_pool_bath"))


def test_a_press_the_processor_never_confirms_retries_then_fails():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    aads.reply = False  # silent processor
    with pytest.raises(CrestronError, match="without the processor confirming"):
        asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert len(aads.presses) == CONFIRM_ATTEMPTS


def test_concurrent_turn_on_presses_once():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    async def race():
        await asyncio.gather(
            bridge.async_turn_on("office_pool_bath"),
            bridge.async_turn_on("office_pool_bath"),
        )

    asyncio.run(race())
    # Two callers, one physical button, one press. The second must see the state
    # the first produced rather than pressing again and undoing it.
    assert aads.presses == [245]
    assert bridge.is_on("office_pool_bath") is True


def test_toggle_refuses_when_state_is_unknown():
    bridge = make_bridge()
    bridge._clients[const.LINK_AADS].synced = False
    with pytest.raises(CrestronError, match="refusing to toggle blind"):
        asyncio.run(bridge.async_toggle("office_pool_bath"))


class FakeIslandClient(FakeClient):
    """Island's status arrives on a different join (29) than its on button
    (27), unlike every FakeClient scenario above where the pressed join is
    also the one that reports. Feedback lands on 29 regardless of which join
    was pressed, matching what the identification pass found on real
    hardware: pressing 27 brought the load on and 29 (not 27) went high.
    """

    async def async_press(self, join, hold=0.0):
        self.presses.append(join)
        if self.reply:
            value = 1 if join == 27 else 0
            self.digital[29] = value
            self._bridge._on_digital(self._link, 29, value)


def test_island_presses_the_on_join_and_confirms_via_the_status_join():
    bridge = make_bridge()
    mc2e = FakeIslandClient()
    mc2e._bridge, mc2e._link = bridge, const.LINK_MC2E
    bridge._clients[const.LINK_MC2E] = mc2e

    asyncio.run(bridge.async_turn_on("kitchen_island"))
    assert mc2e.presses == [27]
    assert bridge.is_on("kitchen_island") is True

    asyncio.run(bridge.async_turn_off("kitchen_island"))
    assert mc2e.presses == [27, 29]
    assert bridge.is_on("kitchen_island") is False

    # Idempotence holds the same way it does for a toggle load, even though
    # on and off go through different joins here.
    asyncio.run(bridge.async_turn_off("kitchen_island"))
    assert mc2e.presses == [27, 29]


# ---- service registration -------------------------------------------------


def test_services_are_not_registered_with_lambdas():
    """Guard the trap that made the first live deploy a silent no-op.

    Home Assistant picks how to invoke a service handler with
    asyncio.iscoroutinefunction(). A lambda fails that check even when it
    returns a coroutine, so HA runs it in an executor, discards the coroutine
    and reports success. The service call then returns HTTP 200 having done
    nothing; the only trace is a "coroutine was never awaited" RuntimeWarning.

    __init__.py can't be imported here because it pulls in Home Assistant, so
    this asserts on the source instead. Crude, but it catches the one shape
    that regressed, and the runtime cost of missing it is a deploy cycle.
    """
    source = (_PKG / "__init__.py").read_text()
    for line in source.splitlines():
        if "async_register" in line and "lambda" in line:
            raise AssertionError(f"service registered with a lambda handler: {line.strip()}")
    assert "def _service(" in source, "expected the async-def handler factory to still exist"
