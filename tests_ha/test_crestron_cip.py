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


def test_table_covers_thirty_five_loads_and_forty_seven_joins():
    assert len(const.LOADS) == 35
    mapped = [load for load in const.LOADS if load.join is not None]
    assert len(mapped) == 35  # all four Kitchen loads identified 2026-09-03
    # 42 worksheet load buttons (41 plus Holiday, reclassified from scene to
    # load 2026-09-06), minus one: d145, Kitchen's own "Pathway" button, sits
    # inside the forbidden alarm range and is no longer referenced by anything
    # now that kitchen_pathway presses its safe alias (d103) directly instead
    # (issue #22). d103 itself is not the gap; it is kitchen_pathway's own
    # canonical join. Island counts once here even though pressing it on takes
    # a second join (press_on): load.joins is feedback joins, and
    # press_on/press_off deliberately are not feedback joins. The join total
    # was 41 with the load count at 29: folding outside_home_perimeter into
    # entry_door (2026-09-06) merged two Loads' joins onto one, it did not
    # remove either d183 or d246 from the table. 2026-09-10 added six Patio
    # scene buttons (Path, Night, Fiesta, Patio All On, Club, Pool), each a
    # single-join toggle with no alias, so both counts rise by exactly six.
    assert sum(len(load.joins) for load in const.LOADS) == 47


def test_kitchen_perimeter_is_gone_not_aliased():
    # It was never a real load, just d103 sharing a name with the Kitchen's
    # actual Pathway light. No alias could express it: aliases are same-link
    # joins, and this pair used to span AADS (103) and MC2E (kitchen_pathway's
    # old join, 25). d103 is now kitchen_pathway's own canonical join instead
    # (issue #22), not an alias and not a second entity.
    assert "kitchen_perimeter" not in const.LOADS_BY_KEY
    pathway = const.LOADS_BY_KEY["kitchen_pathway"]
    assert pathway.link == const.LINK_AADS
    assert pathway.join == 103
    for load in const.LOADS:
        if load.key != "kitchen_pathway":
            assert 103 not in load.joins


def test_kitchen_pathway_no_longer_needs_the_mc2e():
    # Retired 2026-09-06 (issue #22): unlike Range, Island and Cabinet, whose
    # only AADS joins collide with the alarm range and have no safe alias
    # anywhere in the panel project, Pathway's d145 does have one (d103), so
    # it moved to _AADS_LOADS and dropped its MC2E join entirely.
    mc2e_keys = {load.key for load in const.LOADS if load.link == const.LINK_MC2E}
    assert mc2e_keys == {"kitchen_range", "kitchen_island", "kitchen_cabinet"}


def test_holiday_is_a_real_outside_load_not_a_dead_scene_button():
    # d221 ("Holiday" on the Modes page) was categorised as a scene in the
    # 2026-09-02 worksheet pass. pde traced it physically 2026-09-06 and found
    # it switches the outdoor eave receptacles used for holiday lights, an
    # ordinary toggle like any other AADS load (issue #19). Security, Vacation
    # and Party, the other three Modes buttons issue #19 grouped it with,
    # remain unconfirmed and are deliberately absent from this table.
    holiday = const.LOADS_BY_KEY["outside_holiday"]
    assert holiday.link == const.LINK_AADS
    assert holiday.join == 221
    assert holiday.press_on is None and holiday.press_off is None
    assert 221 not in const.FORBIDDEN_AADS_WRITE
    for key in ("outside_security", "outside_vacation", "outside_party"):
        assert key not in const.LOADS_BY_KEY


def test_home_perimeter_was_a_second_name_for_the_door_not_a_fixture():
    # outside_home_perimeter (d183, alias d246) never drove a distinct load.
    # pde traced the real Foyer keypad button for Home Perimeter on 2026-09-06
    # and found it lights the same garage dimmer LED as Door (d181), and
    # separately confirmed pressing d183/d246 does too. Folded into entry_door
    # as aliases rather than kept as a second Load, same treatment Kitchen
    # Perimeter got for the same reason (homeassistant issue #23).
    # The load that button actually operates is not expressible here at all:
    # it lives on Cresnet device 0x74 (a CLX-4HSW4), Digital Join 3, outside
    # the join space either CIP connection can reach.
    assert "outside_home_perimeter" not in const.LOADS_BY_KEY
    door = const.LOADS_BY_KEY["entry_door"]
    assert door.link == const.LINK_AADS
    assert door.join == 181
    assert set(door.joins) == {181, 183, 246}
    for load in const.LOADS:
        if load.key != "entry_door":
            assert 183 not in load.joins
            assert 246 not in load.joins


def test_patio_scene_buttons_are_opaque_macro_loads():
    # d201-d205 and d207 (Path, Night, Fiesta, Patio All On, Club, Pool) were
    # scene buttons in the 2026-09-02 worksheet pass. A CIP-only trace on
    # 2026-09-10 undercounted what they do; pde confirmed by direct, on-site
    # observation that each is real and drives several fixtures, some of
    # which never surface on any join CIP can see at all (same class of gap
    # as outside_home_perimeter above). pde's call: wire each up as one
    # opaque macro rather than chase down every fixture inside it, since he
    # plans to use these as whole scenes in future automations rather than
    # address their contents separately.
    scenes = {
        "courtyard_path": 201,
        "courtyard_night": 202,
        "courtyard_fiesta": 203,
        "courtyard_patio_all_on": 204,
        "courtyard_club": 205,
        "courtyard_pool": 207,
    }
    for key, join in scenes.items():
        load = const.LOADS_BY_KEY[key]
        assert load.link == const.LINK_AADS
        assert load.join == join
        assert load.press_on is None and load.press_off is None
        assert load.press_join(True) == load.press_join(False) == join
        assert join not in const.FORBIDDEN_AADS_WRITE
    # courtyard_pool (d207, the Patio page's "Pool" scene) and office_pool_bath
    # (d245, the Others page's "Pool Bath" load) are unrelated fixtures with
    # similar names; guard against them ever getting collapsed into one join.
    assert const.LOADS_BY_KEY["courtyard_pool"].join != const.LOADS_BY_KEY["office_pool_bath"].join


def test_no_canonical_join_is_one_the_alarm_keypad_shares():
    for load in const.LOADS:
        if load.link == const.LINK_AADS and load.join is not None:
            assert load.join not in const.FORBIDDEN_AADS_WRITE, load.key


def test_press_join_falls_back_to_the_canonical_join_for_ordinary_toggles():
    # 33 of the 35 loads, including three of the four Kitchen ones, are a
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
    """Stands in for a registered CIP session, recording what got pressed.

    Models the subsystem latch as well as the presses, because half of what the
    bridge does now is decide which subsystem to be in before it presses
    anything. `async_press` asserts it was called in the subsystem the slot is
    actually in, so a bridge that forgot to switch fails here rather than
    quietly pressing the AppleTV menu.
    """

    def __init__(self, *, connected=True, synced=True, subsystems=None, default_subsystem=None):
        self.connected = connected
        self.synced = synced
        self.subsystems = dict(subsystems) if subsystems else {}
        self.default_subsystem = default_subsystem
        self.current_subsystem = default_subsystem if self.subsystems else None
        self.digital: dict[int, int] = {}
        self.presses: list[int] = []
        self.entries: list[str] = []
        self.reply: bool = True
        self.entry_replies: bool = True

    async def async_enter(self, subsystem):
        if subsystem is None or not self.subsystems:
            return True
        if self.current_subsystem == subsystem:
            return True
        self.entries.append(subsystem)
        if not self.entry_replies:
            self.current_subsystem = None
            return False
        self.current_subsystem = subsystem
        return True

    def invalidate_subsystem(self):
        self.current_subsystem = None

    async def async_press(self, join, subsystem, hold=0.0):
        assert subsystem == self.current_subsystem, (
            f"pressed d{join} for {subsystem} while the slot was in {self.current_subsystem}"
        )
        self.presses.append(join)
        if self.reply:
            # Real hardware answers with feedback; the bridge is waiting on it.
            self.digital[join] = 0 if self.digital.get(join) else 1
            self._bridge._on_digital(self._link, join, self.digital[join], subsystem)


def make_bridge():
    bridge = CrestronBridge({})
    for link in (const.LINK_AADS, const.LINK_MC2E):
        settings = const.DEFAULTS[link]
        fake = FakeClient(
            subsystems=settings["subsystems"],
            default_subsystem=settings["default_subsystem"],
        )
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
    bridge._on_digital(const.LINK_AADS, 247, 1, const.SUBSYSTEM_LIGHTS)
    assert bridge.is_on("outdoor_kitchen") is True

    aads = bridge._clients[const.LINK_AADS]
    asyncio.run(bridge.async_turn_on("outdoor_kitchen"))
    assert aads.presses == []


def test_press_targets_the_canonical_join_never_the_forbidden_alias():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    asyncio.run(bridge.async_turn_on("outdoor_kitchen"))
    assert aads.presses == [104]
    assert 144 not in aads.presses


def test_powder_presses_a_different_join_for_on_than_off():
    # Reported 2026-09-05 and confirmed by pde: d102 confirms feedback but does
    # not turn on the real fixture, while the Living Rm (d127) and Kitchen
    # (d142) buttons both do, dimmed. d142 is forbidden to write, so d127 is
    # the only usable on-join. Off was never broken and stays on d102, same as
    # before press_on existed.
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    asyncio.run(bridge.async_turn_on("dining_room_powder"))
    assert aads.presses == [127]
    assert 142 not in aads.presses

    asyncio.run(bridge.async_turn_off("dining_room_powder"))
    assert aads.presses == [127, 102]


def test_a_load_with_no_join_mapped_refuses_rather_than_guess():
    # All twenty-nine loads are mapped now (the Kitchen four, 2026-09-03), so this
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

    async def async_press(self, join, subsystem, hold=0.0):
        self.presses.append(join)
        if self.reply:
            value = 1 if join == 27 else 0
            self.digital[29] = value
            self._bridge._on_digital(self._link, 29, value, subsystem)


def test_island_presses_the_on_join_and_confirms_via_the_status_join():
    bridge = make_bridge()
    mc2e = FakeIslandClient(subsystems={}, default_subsystem=None)
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


# ---- subsystem time-slicing -----------------------------------------------
#
# A slot on the AADS holds exactly one subsystem at a time and the join space is
# reused across them, so `d101` is Dining Room Table in Lights and the AppleTV
# menu in A/V. Everything below is about the bridge never confusing the two, in
# either direction: it must not write a lighting join from A/V, and it must not
# let an A/V join move a light. Measurements and design in the
# pdehlke/homeassistant repo, docs/crestron/crestron-subsystem-time-slicing.md.


@pytest.fixture(autouse=True)
def _fast_entries(monkeypatch):
    """Keep the real thresholds out of the test clock without hiding them.

    The measured values are asserted by their own test below; these only make
    the waiting cheap, since an entry is detected by a quiet window and the real
    one is a third of a second.
    """
    monkeypatch.setattr(_cip_mod, "ENTRY_QUIET_SECONDS", dict.fromkeys(const.ENTRY_JOINS, 0.02))
    monkeypatch.setattr(_cip_mod, "ENTRY_MIN_SECONDS", 0.01)
    monkeypatch.setattr(_cip_mod, "ENTRY_TIMEOUT", 0.2)
    monkeypatch.setattr(_cip_mod, "REPOLL_TIMEOUT", 0.2)


def _client(subsystems=None, default_subsystem=const.SUBSYSTEM_LIGHTS):
    """A real CipClient with the socket removed and dumps that can be scripted.

    `_send` is stubbed rather than `_press`, so the alarm-range check inside
    `_press` stays in the path of every test here. `dumps` maps an entry join to
    the joins that entry reports back; an entry join with no dump models a
    processor that ignores the press, which is the 2026-09-15 failure.
    """
    client = _cip_mod.CipClient(
        name="test",
        host="127.0.0.1",
        port=const.CIP_PORT,
        ipid=0x13,
        on_digital=lambda join, value, subsystem: client.seen.append((join, value, subsystem)),
        subsystems=const.ENTRY_JOINS if subsystems is None else subsystems,
        default_subsystem=default_subsystem,
        forbidden=const.FORBIDDEN_AADS_WRITE,
    )
    client.connected = True
    client.seen = []
    client.presses = []
    client.dumps = {}
    # What the mid-session update request returns, which on real hardware is the
    # full state rather than the entry dump's partial one. None models a
    # processor that does not answer it.
    client.repoll_dump = {}

    async def _send(packet):
        if packet != _cip_mod.UPDATE_REQUEST or client.repoll_dump is None:
            return
        if client._collecting is not None:
            client._collecting.update(client.repoll_dump)
        client._end_of_query.set()

    real_press = client._press

    async def _press(join, hold=0.0):
        await real_press(join, hold=0.0)
        client.presses.append(join)
        dump = client.dumps.get(join)
        if dump is not None and client._collecting is not None:
            client._collecting.update(dump)
            client._last_data_rx = asyncio.get_running_loop().time()

    client._send = _send
    client._press = _press
    return client


def test_entering_a_subsystem_applies_its_dump():
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1, 105: 1}

    async def scenario():
        assert await client.async_enter(const.SUBSYSTEM_LIGHTS) is True
        assert client.presses == [const.LIGHTS_ENTRY_JOIN]
        assert client.current_subsystem == const.SUBSYSTEM_LIGHTS
        assert client.digital == {101: 1, 105: 1}
        assert sorted(client.seen) == [
            (101, 1, const.SUBSYSTEM_LIGHTS),
            (105, 1, const.SUBSYSTEM_LIGHTS),
        ]

    asyncio.run(scenario())


def test_a_join_missing_from_the_entry_dump_is_left_alone_not_reported_off():
    """The live regression of 2026-09-22, and the reason entry merges.

    An earlier version treated a join absent from the entry dump as off, on the
    theory that the dump re-asserts every high join. Deployed, that reported
    North Sink off while the light was physically on: d241 was not in that
    entry's dump and never followed. A dump says nothing about what it omits.

    The accepted cost is the other side of the same coin. A load switched off at
    a wall panel while the slot was away in A/V keeps reading on until a later
    frame corrects it. Stale beats wrong, especially when the wrong direction is
    a lit load reading off, which is the signature of the 2026-09-15 outage.
    """
    client = _client()
    client.dumps = {
        const.LIGHTS_ENTRY_JOIN: {101: 1, 243: 1},
        const.AV_ENTRY_JOIN: {1251: 1},
    }

    async def scenario():
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        assert client.digital == {101: 1, 243: 1}

        await client.async_enter(const.SUBSYSTEM_AV)
        client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}
        client.seen.clear()

        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        assert client.digital == {101: 1, 243: 1}, "a partial dump reported a lit load off"
        assert client.seen == []

    asyncio.run(scenario())


def test_the_collection_window_cannot_close_before_the_dump_has_had_time(monkeypatch):
    """The second live fault of 2026-09-22, which made entities move twice.

    An entry dump arrives in bursts and the gap between them can exceed the
    quiet threshold. One Lights entry closed its window 0.47s in, on a dump
    whose last frame lands at about 0.53s; everything after that arrived outside
    the collection and moved entities a second time. The floor is what keeps the
    window open across an intra-dump gap.
    """
    monkeypatch.setattr(_cip_mod, "ENTRY_MIN_SECONDS", 0.30)
    monkeypatch.setattr(_cip_mod, "ENTRY_TIMEOUT", 2.0)
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {243: 1}
    collecting_when_late_burst_landed = []

    async def late_burst():
        await asyncio.sleep(0.15)
        collecting_when_late_burst_landed.append(client._collecting is not None)
        if client._collecting is not None:
            client._collecting[241] = 1
            client._last_data_rx = asyncio.get_running_loop().time()

    async def scenario():
        task = asyncio.create_task(late_burst())
        assert await client.async_enter(const.SUBSYSTEM_LIGHTS) is True
        await task
        assert collecting_when_late_burst_landed == [True], "window closed mid-dump"
        assert client.digital == {243: 1, 241: 1}

    asyncio.run(scenario())


def test_a_dump_that_re_asserts_the_same_values_reports_nothing():
    client = _client()
    client.dumps = {
        const.LIGHTS_ENTRY_JOIN: {101: 1, 105: 1},
        const.AV_ENTRY_JOIN: {1251: 1},
    }

    async def scenario():
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        await client.async_enter(const.SUBSYSTEM_AV)
        client.seen.clear()
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        assert client.seen == []
        assert client.digital == {101: 1, 105: 1}

    asyncio.run(scenario())


def test_an_av_join_never_lands_in_the_lighting_bucket():
    """d101 is Dining Room Table in Lights and the AppleTV menu in A/V."""
    client = _client()
    client.dumps = {
        const.LIGHTS_ENTRY_JOIN: {105: 1},
        const.AV_ENTRY_JOIN: {101: 1},
    }

    async def scenario():
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        await client.async_enter(const.SUBSYSTEM_AV)
        assert client.digital == {105: 1}, "an A/V join reached the lighting bucket"
        assert client.digital_for(const.SUBSYSTEM_AV) == {101: 1}
        assert (101, 1, const.SUBSYSTEM_AV) in client.seen

    asyncio.run(scenario())


def test_an_entry_that_gets_no_answer_fails_rather_than_pretending():
    """An entry that reported nothing is indistinguishable from a dark house."""
    client = _client()

    async def scenario():
        assert await client.async_enter(const.SUBSYSTEM_LIGHTS) is False
        assert client.presses == [const.LIGHTS_ENTRY_JOIN]
        assert client.current_subsystem is None

    asyncio.run(scenario())


def test_bring_up_syncs_only_behind_the_subsystem_dump():
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}

    async def scenario():
        await client._bring_up()
        assert client.synced is True
        assert client.current_subsystem == const.SUBSYSTEM_LIGHTS
        assert client.digital == {101: 1}

    asyncio.run(scenario())


def test_bring_up_leaves_the_link_unsynced_when_the_subsystem_does_not_open():
    """Syncing on the menu dump alone is what made every load read off."""
    client = _client()

    async def scenario():
        await client._bring_up()
        assert client.synced is False
        assert client.current_subsystem is None

    asyncio.run(scenario())


def test_bring_up_re_polls_because_the_entry_dump_under_reports():
    """The wrong reading pde caught on 2026-09-22, and why bring-up re-polls.

    An entry dump is partial: one Lights entry omitted d241 while the light was
    physically on. At bring-up the bucket starts empty, so absence really does
    mean off there and a partial dump makes a lit load read off for the life of
    the session. The update request returns the full state and ends with an
    explicit end-of-query marker, so bring-up asks for it.
    """
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}
    client.repoll_dump = {101: 1, 241: 1}

    async def scenario():
        await client._bring_up()
        assert client.synced is True
        assert client.digital == {101: 1, 241: 1}, "the re-poll's fuller state was not merged"

    asyncio.run(scenario())


def test_bring_up_still_syncs_when_the_re_poll_goes_unanswered():
    """The entry dump is incomplete, not wrong, so it is better than nothing."""
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}
    client.repoll_dump = None

    async def scenario():
        await client._bring_up()
        assert client.synced is True
        assert client.digital == {101: 1}

    asyncio.run(scenario())


def test_an_ungated_link_does_not_re_poll():
    """The re-poll is the cure for a partial entry dump, and there is no entry here.

    An ungated link's registration dump is already the full one. The MC2E also
    answers no second update request: before this, every startup spent the whole
    REPOLL_TIMEOUT waiting for an end-of-query that never came, then carried on
    with what it already had.
    """
    client = _client(subsystems={}, default_subsystem=None)
    client.repoll_dump = {21: 1}

    async def scenario():
        await client._bring_up()
        assert client.synced is True
        assert client.presses == []
        assert client.digital == {}, "an ungated link re-polled anyway"

    asyncio.run(scenario())


def test_the_mc2e_link_has_no_subsystem_to_enter():
    """The MC2E XPanel slot is ungated, which is why the Kitchen kept working."""
    client = _client(subsystems={}, default_subsystem=None)

    async def scenario():
        assert await client.async_enter(None) is True
        await client._bring_up()
        assert client.synced is True
        assert client.presses == []

    asyncio.run(scenario())


def test_pressing_a_join_for_the_wrong_subsystem_is_refused():
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}

    async def scenario():
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        with pytest.raises(_cip_mod.CrestronError, match="refusing to press"):
            await client.async_press(101, const.SUBSYSTEM_AV)

    asyncio.run(scenario())


def test_the_alarm_range_is_refused_at_the_wire_in_either_subsystem():
    client = _client()
    client.dumps[const.LIGHTS_ENTRY_JOIN] = {101: 1}

    async def scenario():
        await client.async_enter(const.SUBSYSTEM_LIGHTS)
        for join in (146, 147, 148, 93):
            with pytest.raises(_cip_mod.CrestronError, match="DSC alarm"):
                await client.async_press(join, const.SUBSYSTEM_LIGHTS)
            with pytest.raises(_cip_mod.CrestronError, match="DSC alarm"):
                await client._press(join)

    asyncio.run(scenario())


def test_every_press_passes_the_forbidden_check():
    """Structural, not a sample: the check is on the only path to the wire.

    Three write surfaces now reach this client, load presses, entry presses and
    whatever step 2 adds for A/V, so the guarantee has to be that there is one
    door rather than that each caller remembers to knock.
    """
    source = (_PKG / "cip.py").read_text()
    press = source[source.index("async def _press(") : source.index("async def async_press(")]
    assert "self.forbidden" in press, "the raw press stopped checking the alarm range"

    public = source[source.index("async def async_press(") :]
    assert "await self._press(" in public, "async_press stopped going through the checked press"
    assert "digital_packet" not in public, "async_press builds its own frames, bypassing the check"


# ---- the shared slot, from the bridge's side ------------------------------


def test_a_lighting_write_while_the_slot_is_in_av_enters_lights_first():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    aads.current_subsystem = const.SUBSYSTEM_AV

    asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert aads.entries == [const.SUBSYSTEM_LIGHTS]
    assert aads.presses == [245]
    assert bridge.is_on("office_pool_bath") is True


def test_a_lighting_write_already_in_lights_presses_no_entry_join():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert aads.entries == []
    assert aads.presses == [245]


def test_the_mc2e_link_is_never_asked_to_enter_anything():
    bridge = make_bridge()
    mc2e = bridge._clients[const.LINK_MC2E]

    asyncio.run(bridge.async_turn_on("kitchen_cabinet"))
    assert mc2e.entries == []
    assert mc2e.presses == [21]


def test_a_failed_entry_refuses_rather_than_pressing_the_load_join():
    """Pressing into a slot that is not in the subsystem is the failure to avoid."""
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    aads.current_subsystem = const.SUBSYSTEM_AV
    aads.entry_replies = False

    with pytest.raises(CrestronError, match="could not enter"):
        asyncio.run(bridge.async_turn_on("office_pool_bath"))
    assert aads.presses == []


def test_an_unconfirmed_press_forgets_the_subsystem_so_the_retry_re_enters():
    """Issue #25, which this model answers with one assignment.

    If the AADS ever drops a slot out of Lights without the session dropping,
    the old bridge had no way back: every command failed forever until something
    unrelated forced a reconnect. Forgetting the subsystem on an unconfirmed
    press makes the next attempt re-press the entry join and rebuild state.
    """
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    aads.reply = False

    with pytest.raises(CrestronError, match="without the processor confirming"):
        asyncio.run(bridge.async_turn_on("office_pool_bath"))

    assert len(aads.presses) == CONFIRM_ATTEMPTS
    # The first attempt was already in Lights, so only the retries re-enter.
    assert aads.entries == [const.SUBSYSTEM_LIGHTS] * (CONFIRM_ATTEMPTS - 1)


def test_two_operations_wanting_different_subsystems_serialize():
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    async def race():
        await asyncio.gather(
            bridge.async_enter_subsystem(const.LINK_AADS, const.SUBSYSTEM_AV),
            bridge.async_turn_on("office_pool_bath"),
        )

    asyncio.run(race())
    # FakeClient.async_press asserts it was called in the subsystem the slot is
    # actually in, so the press landing at all is the real assertion here.
    assert aads.entries == [const.SUBSYSTEM_AV, const.SUBSYSTEM_LIGHTS]
    assert aads.presses == [245]


def test_enter_subsystem_reports_where_the_slot_ended_up():
    bridge = make_bridge()

    result = asyncio.run(bridge.async_enter_subsystem(const.LINK_AADS, const.SUBSYSTEM_AV))
    assert result == {"link": const.LINK_AADS, "subsystem": const.SUBSYSTEM_AV}
    assert bridge.subsystem(const.LINK_AADS) == const.SUBSYSTEM_AV


def test_enter_subsystem_refuses_a_link_that_has_none():
    bridge = make_bridge()
    with pytest.raises(CrestronError, match="no 'av' subsystem"):
        asyncio.run(bridge.async_enter_subsystem(const.LINK_MC2E, const.SUBSYSTEM_AV))


def test_an_idle_slot_returns_to_the_default_subsystem(monkeypatch):
    """Otherwise nothing ever switches back and lighting feedback stays frozen."""
    monkeypatch.setattr(_bridge_mod, "IDLE_WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(_bridge_mod, "IDLE_RETURN_SECONDS", 0.0)
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]
    aads.current_subsystem = const.SUBSYSTEM_AV

    async def scenario():
        task = asyncio.create_task(bridge._idle_watch())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if aads.current_subsystem == const.SUBSYSTEM_LIGHTS:
                break
        task.cancel()
        assert aads.current_subsystem == const.SUBSYSTEM_LIGHTS
        assert aads.entries == [const.SUBSYSTEM_LIGHTS]

    asyncio.run(scenario())


def test_a_slot_that_is_not_idle_is_left_alone(monkeypatch):
    monkeypatch.setattr(_bridge_mod, "IDLE_WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(_bridge_mod, "IDLE_RETURN_SECONDS", 30.0)
    bridge = make_bridge()
    aads = bridge._clients[const.LINK_AADS]

    async def scenario():
        await bridge.async_enter_subsystem(const.LINK_AADS, const.SUBSYSTEM_AV)
        task = asyncio.create_task(bridge._idle_watch())
        await asyncio.sleep(0.1)
        task.cancel()
        assert aads.current_subsystem == const.SUBSYSTEM_AV
        assert aads.entries == [const.SUBSYSTEM_AV]

    asyncio.run(scenario())


# ---- configuration --------------------------------------------------------


def test_the_bridge_holds_the_kitchen_slot_not_the_office_one():
    """Entering Lights makes the AADS light the panel's own room.

    Slot 0x13 was the Office panel and its entry turns on North Sink, so the
    bridge switched a light on every reconnect, restart and return from A/V.
    Reproduced three times on 2026-09-22. Slot 0x14 is the Guest Suite and does
    the same to East Hall, which pde had lived with for years. Slot 0x12, the
    Kitchen panel, turns nothing on, tested both by hand at the panel and over
    CIP, so the bridge moved there. No code can prevent this; only the slot
    choice can.
    """
    assert const.DEFAULTS[const.LINK_AADS]["ipid"] == 0x12


def test_only_the_aads_link_is_configured_with_subsystems():
    bridge = CrestronBridge({})
    aads = bridge._clients[const.LINK_AADS]
    mc2e = bridge._clients[const.LINK_MC2E]
    assert aads.subsystems == const.ENTRY_JOINS
    assert aads.default_subsystem == const.SUBSYSTEM_LIGHTS
    assert aads.forbidden == const.FORBIDDEN_AADS_WRITE
    assert mc2e.subsystems == {}
    assert mc2e.default_subsystem is None
    assert mc2e.forbidden == frozenset()


def test_the_entry_joins_are_not_ones_the_alarm_keypad_shares():
    """d93 is the entry button for the Alarm subsystem; d91 is Lights, d75 A/V."""
    assert const.ENTRY_JOINS == {const.SUBSYSTEM_LIGHTS: 91, const.SUBSYSTEM_AV: 75}
    assert 93 in const.FORBIDDEN_AADS_WRITE
    for subsystem, join in const.ENTRY_JOINS.items():
        assert join not in const.FORBIDDEN_AADS_WRITE, subsystem


def test_the_entry_quiet_thresholds_are_the_measured_ones():
    """Measured 2026-09-22 by mac/poc_subsystem_timing.py across two panel slots.

    No subsystem entry produces the end-of-query marker the registration dump
    ends with, so quiet is the only signal available and each threshold has to
    clear the largest gap inside a dump. Worst seen: Lights 0.166s on 0x14 and
    0.246s on 0x12; A/V 0.352s on 0x14 and 0.419s on 0x12. Each threshold is
    twice the worse of the two.

    Being wrong here is no longer a correctness bug, because entry merges rather
    than rebuilds, so a window that closes early just leaves the remaining frames
    to arrive by the ordinary path. It is still worth keeping honest: these
    numbers are the record of what the processor actually did.
    """
    assert const.ENTRY_QUIET_SECONDS == {const.SUBSYSTEM_LIGHTS: 0.50, const.SUBSYSTEM_AV: 0.85}
    assert const.ENTRY_TIMEOUT == 3.0


def test_an_analog_join_arriving_mid_entry_lands_in_the_subsystem_being_entered():
    """a11, the per-zone volume, arrives inside the entry window.

    `current_subsystem` is deliberately None while an entry is in flight, so
    bucketing analogs by it would file every A/V volume reading under unknown.
    Step 2 reads a11 and would find nothing there.
    """
    client = _client()
    client.dumps[const.AV_ENTRY_JOIN] = {1251: 1}

    async def scenario():
        entering = asyncio.create_task(client.async_enter(const.SUBSYSTEM_AV))
        # Land the analog while the entry is still collecting.
        await asyncio.sleep(0)
        assert client._collecting is not None
        await client._handle_data(b"\x00\x00\x08\x14" + b"\x00\x0a\xe6\x66")
        client.dumps[const.AV_ENTRY_JOIN] = {1251: 1}
        client._collecting.update({1251: 1})
        client._last_data_rx = asyncio.get_running_loop().time()
        assert await entering is True

        assert client.analog_for(const.SUBSYSTEM_AV) == {11: 58982}
        assert client.analog_for(None) == {}

    asyncio.run(scenario())


def test_a_press_cancelled_mid_hold_still_releases_the_join():
    """A held join is a press-and-hold, and holding is not a no-op on this system.

    It ramps a dimmer, and on a learnable scene button it overwrites the scene.
    Cancellation during the hold is a real path: closing a session cancels the
    bring-up task, and that task presses the subsystem-entry join.
    """
    written = []

    class FakeWriter:
        def write(self, packet):
            written.append(packet)

        async def drain(self):
            return None

    # Built bare rather than through _client(), whose press wrapper forces the
    # hold to zero and would finish before a cancellation could land.
    client = _cip_mod.CipClient(
        name="test",
        host="127.0.0.1",
        port=const.CIP_PORT,
        ipid=0x13,
        on_digital=lambda join, value, subsystem: None,
        subsystems=const.ENTRY_JOINS,
        default_subsystem=const.SUBSYSTEM_LIGHTS,
        forbidden=const.FORBIDDEN_AADS_WRITE,
    )
    client.connected = True
    client._writer = FakeWriter()

    async def scenario():
        press = asyncio.create_task(client._press(const.LIGHTS_ENTRY_JOIN, hold=5.0))
        await asyncio.sleep(0.05)
        press.cancel()
        with pytest.raises(asyncio.CancelledError):
            await press
        assert written == [
            digital_packet(const.LIGHTS_ENTRY_JOIN, True),
            digital_packet(const.LIGHTS_ENTRY_JOIN, False),
        ], "the join was left held down"

    asyncio.run(scenario())
