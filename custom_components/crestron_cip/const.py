"""Static configuration for the Crestron CIP lighting bridge.

The load table is the authority for which join drives which light. It comes from
the TSW-752 panel project retrieved over CTP and the AADS's own serial dump, both
recorded in the pdehlke/homeassistant repo at
docs/crestron/crestron-load-room-worksheet.md and
docs/crestron/crestron-tsw-panel-control-path.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DOMAIN = "crestron_cip"

CIP_PORT = 41794

# Link identifiers. Two processors are needed, not one, and the split is forced
# by the alarm collision below rather than chosen.
LINK_AADS = "aads"
LINK_MC2E = "mc2e"

# d91 is the panel project's Lights subsystem-entry button, the one a person taps
# on the home page before any load button is on screen. The AADS gates the whole
# lighting subsystem on it, per slot: until a registered slot presses it, the
# processor sends that slot its menu and nothing else, reports no lighting joins
# at all, and silently ignores every load join pressed at it.
#
# Nothing had to send it for the first five weeks this bridge ran, because the
# AADS program had been up continuously since before panel 13 was unplugged and
# still held that panel latched inside the subsystem. A house power cut on
# 2026-09-15 restarted the program, cleared the latch, and every AADS load went
# dead while the Cresnet keypads and the MC2E's own Kitchen slot kept working.
# Diagnosis and the live proof are in the pdehlke/homeassistant repo at
# docs/crestron/crestron-lights-subsystem-gating.md.
#
# d93 is the same kind of button for the Alarm subsystem. Never press that one:
# it is in FORBIDDEN_AADS_WRITE below for exactly that reason.
LIGHTS_ENTRY_JOIN = 91

# d75 is the same kind of button for the A/V subsystem, confirmed live on
# 2026-09-17 by two full round trips on slot 0x14. One slot can carry both
# lighting and audio by taking turns, which is why the bridge does not need a
# second sacrificed touch panel; what it cannot do is hold both at once, because
# the A/V pages reuse d101-d109, d201-d204 and d151-d157 for entirely different
# things. See docs/crestron/crestron-subsystem-time-slicing.md in the
# pdehlke/homeassistant repo.
AV_ENTRY_JOIN = 75

SUBSYSTEM_LIGHTS = "lights"
SUBSYSTEM_AV = "av"

ENTRY_JOINS = {SUBSYSTEM_LIGHTS: LIGHTS_ENTRY_JOIN, SUBSYSTEM_AV: AV_ENTRY_JOIN}

# How long the link has to stay quiet before an entry dump counts as complete.
#
# Measured 2026-09-22 by mac/poc_subsystem_timing.py, over five live entries on
# slot 0x14 and two more on 0x12. No entry produces the end-of-query marker the
# registration dump ends with, so quiet is the only available signal. The binding
# constraint is the largest gap between two consecutive frames inside one dump:
#
#   Lights   0.144s, 0.155s, 0.166s on 0x14; 0.246s on 0x12
#   A/V      0.352s, 0.349s on 0x14;         0.419s on 0x12
#
# Each threshold is twice the worst gap seen for that subsystem across both
# slots. The A/V dump's larger gap reproduced on the second slot, so it is a
# property of the processor rather than of one panel.
#
# Below the measured gap, an entry would be declared complete in the middle of
# its own dump and the subsystem would be rebuilt from partial state, which
# reads exactly like a quiet house: confident, complete-looking and wrong.
ENTRY_QUIET_SECONDS = {SUBSYSTEM_LIGHTS: 0.50, SUBSYSTEM_AV: 0.85}

# An entry dump arrives in bursts, and the gap between two bursts can exceed the
# quiet threshold above. On 2026-09-22 a Lights entry on the bridge's own slot
# went quiet for longer than the threshold after its first frame and the
# collection window closed 0.47s in, on a dump whose last frame lands at about
# 0.53s. So the window
# has a floor as well as a quiet test: it cannot close before the dump has had
# time to finish, whatever the silence in the middle of it looks like.
ENTRY_MIN_SECONDS = 0.9

# Entry dumps land in 0.514s to 0.529s and the slowest dump ever measured on
# this processor is the 0.964s registration dump, so this is generous.
ENTRY_TIMEOUT = 3.0

# Re-sending the registration-time update request mid-session returns the full
# state rather than the partial one an entry dump gives, and ends with the
# explicit end-of-query marker, so it is waited on exactly rather than by quiet.
# Measured 2026-09-22 at about 0.86s for 149 frames, three times running.
REPOLL_TIMEOUT = 5.0

# A failed entry produces no traffic, which leaves the link quiet enough to
# qualify for another bring-up immediately. Rate-limit the retry rather than
# spin on a processor that is not answering.
BRINGUP_RETRY_SECONDS = 5.0

# How long the slot may sit in a non-default subsystem with nothing to do before
# it is returned to the default one. While the slot is away, lighting state is
# frozen at whatever the last Lights dump said and a light changed at a wall
# panel is invisible, which is tolerable for a second and wrong for an hour.
# Longer than any one operation is allowed to hold the slot lock, or a running
# A/V walk would pay a pointless round trip between every zone.
IDLE_RETURN_SECONDS = 5.0

# The AADS runs a courtesy action when a panel slot enters the Lights subsystem:
# it turns on a light in that panel's own room. Slot 0x14, the Guest Suite panel,
# turns on East Hall, which pde had observed for years whenever that panel woke
# from standby. Slot 0x13 was the Office panel, and it turns on North Sink.
#
# That made the bridge switch a light on every reconnect, every Home Assistant
# restart and every return from A/V, because all three enter Lights. Measured
# and reproduced three times on 2026-09-22, at 1.1s after the entry press.
#
# So the bridge moved to slot 0x12, the Kitchen panel, which was taken offline
# for the purpose. Entering Lights there turns nothing on, tested both by hand at
# the panel and over CIP before the move. The fix is the slot, not code: nothing
# here can stop the AADS program running its own entry logic. Full write-up in
# the pdehlke/homeassistant repo at
# docs/crestron/crestron-subsystem-time-slicing.md.
# The AADS moved from 192.168.4.61 to 192.168.4.65 on 2026-09-23. Its DHCP lease
# was not reserved, so it renewed onto a new address overnight and the bridge's
# CIP connect began failing with EHOSTUNREACH. Every AADS-backed load and all six
# audio zones went dead while the three MC2E Kitchen loads kept working, because
# they are a separate connection to a separate processor. pde reserved .65 to the
# AADS's MAC the same morning, so this address is now fixed.
#
# Note what this outage did NOT touch: the slot. The bridge still registers as
# IP-ID 0x12, and the alarm-write guard keys on the link name rather than the
# host (see bridge.py), so neither was involved. The symptom looked like a slot
# regression and was not.
DEFAULTS = {
    LINK_AADS: {
        "host": "192.168.4.65",
        "ipid": 0x12,
        "subsystems": ENTRY_JOINS,
        "default_subsystem": SUBSYSTEM_LIGHTS,
    },
    LINK_MC2E: {
        "host": "192.168.4.59",
        "ipid": 0x03,
        "subsystems": {},
        "default_subsystem": None,
    },
}

# The DSC alarm keypad page (5-SEC / ALARM-DSC-pg01-main) reuses this join range
# on the AADS: d130-d141 are the keypad digits and Arm ToHome, and d146/d147/d148
# are Fire, Medical and Panic. d93 enters the alarm subsystem from the home page.
#
# Every page in the panel project has DigitalJoinOffset 0, so this is genuine
# reuse of one join space, not an artifact. The AADS is believed to disambiguate
# on the subsystem-entry join, but that gating is inferred from the panel project
# and has never been confirmed against the AADS program's logic. Treat the whole
# range as unwritable.
#
# Receiving one of these joins as feedback is fine and expected: several lights
# carry an alias inside the range. Only writing is refused.
FORBIDDEN_AADS_WRITE = frozenset(range(130, 149)) | {93}


# ---- A/V ------------------------------------------------------------------
#
# The same TSW-752 panel project that gave up the lighting joins also carries
# room-by-room audio for the AADS's six zones, on the same CIP route. Map and
# live evidence in the pdehlke/homeassistant repo at
# docs/crestron/crestron-av-zone-control-path.md.
#
# Living Room is deliberately absent and cannot be added: it is driven by an
# external Integra receiver wired into the AADS as one fixed line input, so
# Crestron can move that feed around the house but cannot change what it plays.


@dataclass(frozen=True)
class Zone:
    """One audio zone and the join that moves the slot's cursor onto it.

    `name` is what the processor reports on s11 once the cursor lands, and is
    checked rather than assumed, because a cursor move that silently failed
    would otherwise have every later read attributed to the wrong room.
    """

    key: str
    name: str
    select_join: int


ZONES: tuple[Zone, ...] = (
    Zone("kitchen", "Kitchen", 951),
    Zone("outdoor_kitchen", "Outdoor Kitchen", 952),
    Zone("master_bed", "Master Bed", 953),
    Zone("master_bath", "Master Bath", 954),
    Zone("studio", "Studio", 955),
    Zone("courtyard", "Courtyard", 956),
)
ZONES_BY_KEY: dict[str, Zone] = {zone.key: zone for zone in ZONES}

# Source numbering is regular: source N presses d(50+N), publishes its name on
# s(100+N), and raises d10N1 when it is the selected source. Confirmed live for
# N=1 and N=5. The project defines d51-d74, but the live six-tile subpage only
# wires d51-d56, so only those are reachable from this panel layout.
AV_SOURCES = range(1, 7)
AV_NO_SOURCE_JOIN = 1001


def source_press_join(source: int) -> int:
    return 50 + source


def source_feedback_join(source: int) -> int:
    return 1000 + source * 10 + 1


def source_name_serial(source: int) -> int:
    return 100 + source


VOLUME_UP_JOIN = 44
VOLUME_DOWN_JOIN = 45
MUTE_JOIN = 48
MUTE_FEEDBACK_JOIN = 46
ZONE_POWER_OFF_JOIN = 42
ALL_ZONES_OFF_JOIN = 40

ZONE_NAME_SERIAL = 11
SOURCE_NAME_SERIAL = 16
VOLUME_ANALOG = 11

# The AADS works internally in percent with 65535 as 100: selecting Tuner 1
# applied a preset of exactly 26214, which is 40.000% of full scale. So percent
# is the honest unit for a service to take, not the raw join and not a rescaling
# onto the audible span.
VOLUME_FULL_SCALE = 65535

# a11 accepts no direct write. Both analog joins in all 52 pages are feedback
# only, and there is no way to set a level numerically from a physical panel
# either, so setting one means holding d44 or d45 and converging against a11.
# Measured at about 6570 units per second of hold, linear, consistent in both
# directions.
VOLUME_RAMP_UNITS_PER_SECOND = 6570.0

# One minimum-length press moves about 800 units, so a tolerance below that
# would oscillate around the target forever. This is a little over one press.
VOLUME_TOLERANCE = 1000
VOLUME_MAX_SEGMENTS = 12

# Per pde, the speakers are inaudible below roughly 80% of full scale on
# AirPlay. Anything under this is accepted and acted on, and warned about, since
# asking for it is more likely a unit mix-up than an intention.
VOLUME_AUDIBLE_FLOOR_PERCENT = 80.0

# A cursor move blanks the per-zone joins for about 60ms before repopulating
# them. Anything read inside that window reports a dead zone with full
# confidence, which is the same silent failure the lighting subsystem gate
# produced on 2026-09-15. This is the margin over that, not the measurement.
CURSOR_SETTLE_SECONDS = 0.40
CURSOR_CONFIRM_TIMEOUT = 3.0

# No single operation may hold the slot lock for longer than this. A lighting
# write queued behind an A/V one waits at most this plus one entry to Lights, so
# worst-case added lighting latency stays around two seconds. It is also the cap
# on a single volume ramp segment, which is why a nine-second ramp is chopped
# into pieces that each give the slot back.
MAX_HOLD_SECONDS = 1.0

AV_WRITE_JOINS = frozenset(
    {VOLUME_UP_JOIN, VOLUME_DOWN_JOIN, MUTE_JOIN, ZONE_POWER_OFF_JOIN, ALL_ZONES_OFF_JOIN}
    | {zone.select_join for zone in ZONES}
    | {source_press_join(n) for n in AV_SOURCES}
)


@dataclass(frozen=True)
class Load:
    """One physical lighting load and the joins that address it.

    Every AADS load and most MC2E loads are a single toggle button: `join`
    both reports state and is the one thing ever pressed, in either
    direction. That is the default, and `press_on`/`press_off` are left
    unset.

    A dimmer channel can differ. Island (MC2E `0x72` ch2, identified
    2026-09-03) has separate on and off buttons in the panel project rather
    than one toggle, so `join` stays the status/feedback join (what `is_on()`
    reads) while `press_on`/`press_off` say what to press for each direction.
    `press_join()` is the one place that resolves what a command actually
    presses; nothing else should read `press_on`/`press_off` directly. Every
    MC2E module the Kitchen slot reaches (`0x70`-`0x73`, `0x75`, `0x76`) is a
    dimmer, so expect more loads to need this shape once the Phase 2 dimmer
    pass identifies them, not just Island.

    `level_join` records the analog join carrying a dimmer's brightness,
    where known. The bridge does not act on it yet: brightness is out of
    scope until Phase 2. It is captured now so that pass does not have to
    re-derive an identification this project already did.
    """

    key: str
    name: str
    link: str
    join: int | None
    aliases: tuple[int, ...] = field(default=())
    press_on: int | None = None
    press_off: int | None = None
    level_join: int | None = None

    @property
    def joins(self) -> tuple[int, ...]:
        """Every join that reports this load's state, canonical one first."""
        return () if self.join is None else (self.join, *self.aliases)

    @property
    def press_joins(self) -> tuple[int, ...]:
        """Every join this load could ever have pressed, for safety checks."""
        return tuple({j for j in (self.join, self.press_on, self.press_off) if j is not None})

    def press_join(self, want_on: bool) -> int:
        """Which join to press to reach the requested state.

        Toggle loads have no press_on/press_off, so this falls back to `join`
        for both directions, same as every load behaved before this field
        existed.
        """
        if want_on and self.press_on is not None:
            return self.press_on
        if not want_on and self.press_off is not None:
            return self.press_off
        if self.join is None:
            raise ValueError(f"{self.key}: no join mapped, cannot press")
        return self.join


# Thirty-two loads reachable through the freed TSW-752 panel slot on the AADS (twenty-six
# ordinary loads plus the six Patio-page scene-button macros added 2026-09-10, below).
#
# Where a load appears on several zone pages, the canonical join is the one
# chosen to press and the aliases only ever report. Outdoor Kitchen is one load
# on five buttons (proven 2026-09-02: pressing d104 drove d144, d187, d206 and
# d247 high in the same instant). Powder is one load on three.
#
# Where a load's join would fall inside FORBIDDEN_AADS_WRITE, the canonical join
# is deliberately an alias outside it: Powder presses d102 rather than d142, and
# Outdoor Kitchen presses d104 rather than d144.
#
# d103, labeled "Perimeter" on the Dining page, is not a load of its own.
# Reported 2026-09-05 and confirmed by pde: it drives the same physical fixture
# as Kitchen's own Pathway light, not a separate Kitchen Perimeter light. There
# used to be a kitchen_perimeter Load here for it; removed rather than turned
# into an alias, because Load.aliases only covers joins on the same link, and
# this pair spanned AADS (103) and MC2E (kitchen_pathway's old join, 25).
#
# Retired the MC2E side of that pair 2026-09-06 (issue #22): d103 sits outside
# FORBIDDEN_AADS_WRITE and was already proven live to toggle the real fixture
# (that's how the Kitchen Perimeter mixup was caught in the first place), so
# kitchen_pathway now presses d103 directly and needs no MC2E join at all.
#
# d221 ("Holiday" on the Modes page, LIGHT-pg01-zn07) was categorised as a
# scene button, not a load, in the original 2026-09-02 worksheet pass. pde
# traced it physically on 2026-09-06 and found it switches a real fixture, the
# outdoor eave receptacles used for holiday lights, not a macro over other
# loads. Reclassified as an ordinary Outside load on that basis (issue #19).
# Security, Vacation and Party, the other three Modes buttons issue #19
# originally grouped with Holiday, are unresolved and stay untracked: pde saw
# no visible effect from any of them at a physical panel, and it is still open
# whether they do nothing in this installation, do something not visually
# obvious, or have non-independent feedback the way Goodbye and Good Night do.
#
# The Patio page's six scene buttons (d201-d205, d207; d206 "Outdoor Kitchen"
# is the page's one ordinary load, already above) got a CIP-only trace on
# 2026-09-10 that undercounted what they actually do. pde confirmed by direct,
# on-site observation that each is a real, working combination of fixtures,
# several of which (the courtyard's four corners, the patio sconces, the south
# pathway) never show up on any join the panel reports back over CIP at all --
# the same class of gap outside_home_perimeter below is the precedent for.
# pde's call: wire each button up as one opaque macro Load rather than chase
# down every individual fixture inside it, since he expects to use these as
# whole scenes in future automations rather than address their contents
# separately. Each one's `join` is its own indicator, which behaves as genuine
# on/off feedback -- it clears on Area Off or when a different button in the
# same mutually-exclusive scene-selector group takes over -- so these press
# the same way any other toggle Load does, no press_on/press_off split needed.
# Full history in the pdehlke/homeassistant repo's
# crestron-load-room-worksheet.md, Patio section.
#
# outside_home_perimeter (d183, alias d246) never was a distinct fixture. pde
# found the real Foyer keypad button for Home Perimeter on 2026-09-06, and it
# does not touch either digital join at all: watching the crestron_cip bridge's
# own raw join trace while it was pressed showed nothing but the Goodbye/Good
# Night "everything's off" tell moving. Pressing d183/d246 directly, separately,
# showed the same garage dimmer LED lighting up as pressing Door (d181) does.
# So this Load was a second name for entry_door, not a fixture of its own; its
# joins are folded into entry_door as aliases below. The real Home Perimeter
# turned out to live entirely outside the join space either CIP connection can
# reach at all: Cresnet device 0x74 (a CLX-4HSW4, "reports Digital Joins
# instead" per crestron-migration.md, unlike every dimmer module), Digital
# Join 3, confirmed by name over the MC2E's own console (SDEBUG), not
# something a Load here can express. See homeassistant issue #23 for the full
# trace and why that join cannot be wired up without new SIMPL program logic.
#
# Powder needs press_on split from its canonical join. Reported 2026-09-05 and
# confirmed by pde: pressing d102 to go on lights the feedback join but not the
# real fixture, while the Living Rm (d127) and Kitchen (d142) Powder buttons both
# turn it on, dimmed. d142 is forbidden to write (Kitchen zone page, alarm range),
# so d127 is the only working candidate; d102 stays canonical for feedback and for
# turning off, which was never broken. Same asymmetric-join shape as Island, one
# load short of Phase 2 rather than a table error. See
# crestron-ha-bridge.md#powder-needed-its-own-on-join-like-island for the diagnosis.
_AADS_LOADS: tuple[Load, ...] = (
    Load("dining_room_table", "Table", LINK_AADS, 101),
    Load("dining_room_powder", "Powder", LINK_AADS, 102, (127, 142), press_on=127),
    Load("dining_room_north", "North", LINK_AADS, 105),
    Load("dining_room_south", "South", LINK_AADS, 107),
    Load("living_room_pathway", "Pathway", LINK_AADS, 121),
    Load("living_room_west_seating", "West Seating", LINK_AADS, 122),
    Load("living_room_ambient", "Ambient", LINK_AADS, 123),
    Load("living_room_east_seating", "East Seating", LINK_AADS, 124),
    Load("living_room_perimeter", "Perimeter", LINK_AADS, 125),
    Load("outdoor_kitchen", "Outdoor Kitchen", LINK_AADS, 104, (144, 187, 206, 247)),
    Load("courtyard_patio_south", "Patio South", LINK_AADS, 126, (166, 186)),
    Load("courtyard_patio_north", "Patio North", LINK_AADS, 164, (188,)),
    Load("courtyard_path", "Path", LINK_AADS, 201),
    Load("courtyard_night", "Night", LINK_AADS, 202),
    Load("courtyard_fiesta", "Fiesta", LINK_AADS, 203),
    Load("courtyard_patio_all_on", "Patio (All On)", LINK_AADS, 204),
    Load("courtyard_club", "Club", LINK_AADS, 205),
    Load("courtyard_pool", "Pool", LINK_AADS, 207),
    Load("primary_suite_bed_perimeter", "Bed Perimeter", LINK_AADS, 161),
    Load("primary_suite_hallway", "Hallway", LINK_AADS, 162),
    Load("primary_suite_bed_diagonal", "Bed Diagonal", LINK_AADS, 163),
    Load("primary_suite_bath_perimeter", "Bath Perimeter", LINK_AADS, 165),
    Load("primary_suite_bath_diagonal", "Bath Diagonal", LINK_AADS, 167),
    Load("entry_door", "Door", LINK_AADS, 181, (183, 246)),
    Load("entry_center", "Entry Center", LINK_AADS, 182),
    Load("entry_perimeter", "Entry Perimeter", LINK_AADS, 184),
    Load("outside_garage_sconces", "Garage Sconces", LINK_AADS, 185, (244,)),
    Load("outside_holiday", "Holiday", LINK_AADS, 221),
    Load("office_north_sink", "North Sink", LINK_AADS, 241),
    Load("office_pool_bath", "Pool Bath", LINK_AADS, 245),
    Load("guest_suite_east_hall", "East Hall", LINK_AADS, 243),
    Load("kitchen_pathway", "Pathway", LINK_AADS, 103),
)

# Three Kitchen loads whose only AADS joins (d141, d143, d147) sit inside the
# alarm range and have no safe alias anywhere in the panel project. They are
# reachable instead through the MC2E XPanel at IP-ID 0x03, whose retrieved
# program contains no alarm, security or access control of any kind.
#
# Identified live 2026-09-03 (issue #18) by pressing each candidate join on
# IP-ID 0x03 and watching which Kitchen light responded. Cabinet and Range are
# ordinary toggles, same as every AADS load. Island is not: its channel
# (0x72 ch2) has a separate raise button (join 27) and a separate single-press
# fade-to-off button (join 29), with 29 doubling as the on/off status join, so
# it takes press_on/press_off rather than a bare join. A brief tap of 27 was
# enough to bring it on to a low, nonzero level (`a22` rose from 0), and the
# raise/lower joins (27/28) plus the fade timing on 29 mean this channel is
# genuinely dimmable; brightness stays out of scope here per issue #18, but
# level_join is recorded so Phase 2 does not have to re-derive it.
# `0x71` ch3 (raise 22 / lower 23) was left untouched: it is Powder by
# elimination, already driven from the AADS at d102, and out of scope.
#
# A fourth Kitchen load, Pathway, was identified on this same slot (join 25)
# but has since moved to _AADS_LOADS: unlike Range, Island and Cabinet, its
# join (d145) has a safe alias elsewhere in the panel project (d103), so it
# needs no MC2E join at all. See _AADS_LOADS's own comment and issue #22.
_MC2E_LOADS: tuple[Load, ...] = (
    Load("kitchen_range", "Range", LINK_MC2E, 26),
    Load("kitchen_island", "Island", LINK_MC2E, 29, press_on=27, level_join=22),
    Load("kitchen_cabinet", "Cabinet", LINK_MC2E, 21),
)

LOADS: tuple[Load, ...] = _AADS_LOADS + _MC2E_LOADS
LOADS_BY_KEY: dict[str, Load] = {load.key: load for load in LOADS}


def _validate() -> None:
    """Fail at import rather than at press time if the table is unsafe.

    A join inside the alarm range would otherwise sit dormant in the table
    until someone turned that light on. Checked against every join a load
    could press (`press_joins`), not just its canonical `join`: a load with
    distinct press_on/press_off, like Island, could otherwise smuggle a
    forbidden press in through one of those without this catching it.
    """
    for subsystem, join in ENTRY_JOINS.items():
        if join in FORBIDDEN_AADS_WRITE:
            raise ValueError(f"the {subsystem} subsystem-entry join is one the DSC alarm shares")
    if SUBSYSTEM_LIGHTS not in ENTRY_JOINS or ENTRY_JOINS[SUBSYSTEM_LIGHTS] != LIGHTS_ENTRY_JOIN:
        raise ValueError("the lights subsystem must enter on LIGHTS_ENTRY_JOIN")
    for link, settings in DEFAULTS.items():
        default = settings["default_subsystem"]
        if default is not None and default not in settings["subsystems"]:
            raise ValueError(f"{link}'s default subsystem {default!r} has no entry join")
    missing = set(ENTRY_QUIET_SECONDS) ^ set(ENTRY_JOINS)
    if missing:
        raise ValueError(f"no measured entry quiet threshold for {sorted(missing)}")

    forbidden = sorted(AV_WRITE_JOINS & FORBIDDEN_AADS_WRITE)
    if forbidden:
        raise ValueError(f"A/V would press {forbidden}, which the DSC alarm keypad shares")
    zone_joins = {zone.select_join for zone in ZONES}
    if len(zone_joins) != len(ZONES):
        raise ValueError("two zones share a select join")
    for zone in ZONES:
        if not zone.name.strip():
            raise ValueError(f"zone {zone.key} has no name to confirm a cursor move against")

    seen: dict[tuple[str, int], str] = {}
    for load in LOADS:
        if load.link == LINK_AADS and any(j in FORBIDDEN_AADS_WRITE for j in load.press_joins):
            raise ValueError(f"{load.key} presses a join the DSC alarm keypad shares")
        for join in load.joins:
            owner = seen.setdefault((load.link, join), load.key)
            if owner != load.key:
                raise ValueError(f"join {join} on {load.link} claimed by {owner} and {load.key}")
        for join in (load.press_on, load.press_off):
            if join is None or join == load.join:
                continue
            owner = seen.setdefault((load.link, join), load.key)
            if owner != load.key:
                raise ValueError(f"join {join} on {load.link} claimed by {owner} and {load.key}")


_validate()
