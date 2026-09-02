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

DEFAULTS = {
    LINK_AADS: {"host": "192.168.4.61", "ipid": 0x13},
    LINK_MC2E: {"host": "192.168.4.59", "ipid": 0x03},
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


@dataclass(frozen=True)
class Load:
    """One physical lighting load and the joins that address it."""

    key: str
    name: str
    link: str
    join: int | None
    aliases: tuple[int, ...] = field(default=())

    @property
    def joins(self) -> tuple[int, ...]:
        """Every join that reports this load's state, canonical one first."""
        return () if self.join is None else (self.join, *self.aliases)


# Twenty-six loads reachable through the freed TSW-752 panel slot on the AADS.
#
# Where a load appears on several zone pages, the canonical join is the one
# chosen to press and the aliases only ever report. Outdoor Kitchen is one load
# on five buttons (proven 2026-09-02: pressing d104 drove d144, d187, d206 and
# d247 high in the same instant). Powder is one load on three.
#
# Where a load's join would fall inside FORBIDDEN_AADS_WRITE, the canonical join
# is deliberately an alias outside it: Powder presses d102 rather than d142, and
# Outdoor Kitchen presses d104 rather than d144. Kitchen Perimeter is d103, which
# lives on the Dining page and was never in the range at all.
_AADS_LOADS: tuple[Load, ...] = (
    Load("dining_room_table", "Table", LINK_AADS, 101),
    Load("dining_room_powder", "Powder", LINK_AADS, 102, (127, 142)),
    Load("dining_room_north", "North", LINK_AADS, 105),
    Load("dining_room_south", "South", LINK_AADS, 107),
    Load("living_room_pathway", "Pathway", LINK_AADS, 121),
    Load("living_room_west_seating", "West Seating", LINK_AADS, 122),
    Load("living_room_ambient", "Ambient", LINK_AADS, 123),
    Load("living_room_east_seating", "East Seating", LINK_AADS, 124),
    Load("living_room_perimeter", "Perimeter", LINK_AADS, 125),
    Load("kitchen_perimeter", "Perimeter", LINK_AADS, 103),
    Load("outdoor_kitchen", "Outdoor Kitchen", LINK_AADS, 104, (144, 187, 206, 247)),
    Load("courtyard_patio_south", "Patio South", LINK_AADS, 126, (166, 186)),
    Load("courtyard_patio_north", "Patio North", LINK_AADS, 164, (188,)),
    Load("primary_suite_bed_perimeter", "Bed Perimeter", LINK_AADS, 161),
    Load("primary_suite_hallway", "Hallway", LINK_AADS, 162),
    Load("primary_suite_bed_diagonal", "Bed Diagonal", LINK_AADS, 163),
    Load("primary_suite_bath_perimeter", "Bath Perimeter", LINK_AADS, 165),
    Load("primary_suite_bath_diagonal", "Bath Diagonal", LINK_AADS, 167),
    Load("entry_door", "Door", LINK_AADS, 181),
    Load("entry_center", "Entry Center", LINK_AADS, 182),
    Load("entry_perimeter", "Entry Perimeter", LINK_AADS, 184),
    Load("outside_home_perimeter", "Home Perimeter", LINK_AADS, 183, (246,)),
    Load("outside_garage_sconces", "Garage Sconces", LINK_AADS, 185, (244,)),
    Load("office_north_sink", "North Sink", LINK_AADS, 241),
    Load("office_pool_bath", "Pool Bath", LINK_AADS, 245),
    Load("guest_suite_east_hall", "East Hall", LINK_AADS, 243),
)

# The four Kitchen loads whose only AADS joins (d141, d143, d145, d147) sit
# inside the alarm range. They are reachable instead through the MC2E XPanel at
# IP-ID 0x03, whose retrieved program contains no alarm, security or access
# control of any kind.
#
# join is None because which XPanel join drives which of these four is not yet
# established: the press map records channels (0x71 ch4, 0x75 ch0, 0x72 ch3,
# 0x72 ch2) but ties only 0x71 ch3 to a name. Until the identification pass fills
# these in, the loads are declared and reported unavailable rather than guessed.
_MC2E_LOADS: tuple[Load, ...] = (
    Load("kitchen_range", "Range", LINK_MC2E, None),
    Load("kitchen_island", "Island", LINK_MC2E, None),
    Load("kitchen_pathway", "Pathway", LINK_MC2E, None),
    Load("kitchen_cabinet", "Cabinet", LINK_MC2E, None),
)

LOADS: tuple[Load, ...] = _AADS_LOADS + _MC2E_LOADS
LOADS_BY_KEY: dict[str, Load] = {load.key: load for load in LOADS}


def _validate() -> None:
    """Fail at import rather than at press time if the table is unsafe.

    A canonical join inside the alarm range would otherwise sit dormant in the
    table until someone turned that light on.
    """
    seen: dict[tuple[str, int], str] = {}
    for load in LOADS:
        if load.link == LINK_AADS and load.join in FORBIDDEN_AADS_WRITE:
            raise ValueError(f"{load.key} presses d{load.join}, which the DSC alarm keypad shares")
        for join in load.joins:
            owner = seen.setdefault((load.link, join), load.key)
            if owner != load.key:
                raise ValueError(f"join {join} on {load.link} claimed by {owner} and {load.key}")


_validate()
