# HANDOFF — end of 2026-09-01

Point-in-time record. Everything below is either evidence or explicitly labelled
as unproven. Read "Start here", "What we can control today" and "The open
question", then dip into the rest as needed.

---

## Start here

The goal is to control this house's Crestron lighting from Home Assistant.

**The Cresnet injection path is dead.** We can transmit onto the bus, proven, but
the CLX modules do not act on commands that don't arrive in their slot in the
master's poll round. Do not spend more time on it. Details under "Dead ends".

**The IP path works, and reaches exactly one room.** The MC2E's program contains
an XPanel at `Slot-05.IP-ID-03` that nothing occupies. We can register on it,
receive a full state dump plus live per-light state including brightness, and
the processor accepts button presses from us and drives the dimmers in response.
Lights were physically turned on and off this way, confirmed by the owner.

**But every load it can reach is in the Kitchen.** All 75 press joins were
scanned. Joins 21-35 drive five Kitchen loads; joins 36-95 drive no dimmer at
all. The `.dsc` describes this panel as `101-Kitchen` and that is literally what
it is. The path is proven and it does not generalise to the rest of the house.

---

## What we can control today

Five Kitchen loads, individually and as a group, over IP, with no Cresnet
timing and no programmer. Confirmed by pressing them and looking at the room.

| join | drives | effect |
|---|---|---|
| 21 | `0x71` ch4 | on, instant |
| 22 / 23 | `0x71` ch3 | raise / lower |
| 25 | `0x75` ch0 | on |
| 26 | `0x72` ch3 | on |
| 27 / 28 / 29 | `0x72` ch2 | raise / lower / off |
| 30 | all five | full (`FF`) |
| 31 | all five | **off** (`00`) — the one-press room clear |
| 32 / 33 / 34 | all five | 75% / 50% / 25% |
| 35 | all five | toggle |

The five loads are Island, Range, Kitchen Pathway, Powder and Cabinet, per the
owner. `0x71` ch3 is almost certainly Powder: it is the only channel in both the
Kitchen group and the Great Room keypad's Living Pathway pair.

Joins 36-95 drive nothing. Many of them flip feedback pairs exactly 31 apart
(`d53`/`d84`, `d54`/`d85`, ...), which looks like page selection on a panel
whose lighting controls only ever addressed one room.

## The open question

**How to reach the other thirteen rooms.** Their loads are wired to keypads and
touch panels, not to this XPanel slot. Two candidate routes, neither explored:

1. Another free IP-ID with a wider join map. The `.dsc` shows only `IP-ID-03`
   and `IP-ID-05` on the MC2E, so this may require a slot that does not exist.
2. The EISC at `Slot-05.IP-ID-05`, which carries whole-house joins (`d58` Entry
   Center, `d99` Sink Area, `d103` Pool Bath were confirmed on 2026-08-31). It
   is occupied by the AADS, and displacing it breaks audio and the ST-IO. That
   is the known blocker and it has not moved.

Note that the second route is the one with the whole-house join map already
partly built. It is worth understanding exactly what breaks before dismissing it.

## What is proven

**We can transmit on Cresnet.** The MC2E logged our probe bytes verbatim:

```
Warning: Unexpected packet encountered from Slot-01.ID-6A (Type: Old Shaft Encoder,
  [6A][5A][06][5A][5A][5A][5A][5A][5A][71][00][5A][06][5A][5A][5A][5A][5A][5A][71]...)
```

`0x5A` has never appeared in 937,118 bytes of captured bus traffic. Independently,
the roll call's conformance fell from 100.0% to 80.2% while we transmitted and
recovered to 98.9% after. Two witnesses, same answer.

**The CLX modules do not obey out-of-turn commands.** A clean, byte-perfect,
verbatim-replayed `1D` command addressed to `0x71` produced no light and no
reaction. Sustained injection makes all seven modules send Update Requests and
re-initialise, which is a fault response, not obedience.

**The processor does not override us.** It issued no `1D` of its own in either
injection run. The hypothesis that it corrects our writes is wrong.

**The XPanel slot `IP-ID-03` is free, live, and bidirectional.** On connect the
processor sends a 42-join state dump plus analogs and serials. It reports
lighting changes live. And it accepts our presses: `CRX ... Digital Join 24 is
High` is the processor receiving a button press from us.

**The XPanel reports brightness.** When the Great Room keypad turned Living
Pathway on, analog join 21 went to 50069 = `0xC395`, whose high byte is exactly
the `0xC3` level the Cresnet bus carries for that light.

**There is no alarm in this processor.** The compiled program contains zero
occurrences of alarm, Apex, zone, motion, siren, passcode, panic or intrusion.
`G-Security` is a *lighting scene*, letter G in a set of eight running
`A-Welcome`, `B-Good Bye`, `C-House On`, `D-House Off`, `E-Good Morning`,
`F-Good Night`, `G-Security`, `H-Entertain`. The worst case of pressing an
unknown join on this processor is that lights change. This removes the
constraint that shaped most of the day's caution.

---

## Dead ends — do not repeat these

**Cresnet injection.** Covered above. The hardware is fine; the protocol is the
obstacle. Four separate runs, all instrumented.

**The "processor overrides us" theory.** Disproved: no `1D` from the processor.

**The "our adapter can't transmit" theory.** Disproved by the processor's log.

**The echo test.** Worthless on this hardware. The SH-U14 is FTDI-based, and on
FTDI's RS-485 reference design local echo is off by default: CBUS4 is TXDEN,
which disables the receiver while the driver is active. Not hearing our own
transmission tells us nothing.
<https://ftdichip.com/wp-content/uploads/2023/07/DS_USB_RS485_CABLES.pdf>

**`XGETFILE` over telnet.** Blocked: "Command Blocked from this console type".
Use CTP on port 41795 instead.

**`REPORTCRESNET`.** Returns nothing on this firmware over telnet, even with a
120-second wait. The `.dsc` file gives the same inventory.

---

## Corrections to earlier records

**IP-ID-03 was wrongly cleared on 2026-08-31.** It was tested and found "silent
through bus-confirmed lighting changes", and that conclusion was carried forward
for a day. It was a false negative caused by three decoder bugs in
`cip_xpanel.py` (multi-join digital, multi-join analog, and serial datatype
`0x15`) that were fixed *after* that test. The slot is not silent. Re-test
anything that was cleared with the old decoder.

**`captures/20260901T111751.jsonl` has a wrong device label.** It records
`0x6F` / Kitchen; the press was on `0x6A`, the Great Room keypad. See
`captures/20260901T111751.CORRECTION.md`. The frames are unaffected.

> `captures/` is gitignored, so every capture and evidence JSON referenced in
> this document exists only on the machine that recorded it. The correction note
> above is force-added; the data files are not. Today's evidence files are
> `20260901T155511-joinpress.json`, `20260901T154938-joinwatch.json`, and the
> `-witness`/`-override` pair, all on pde's laptop.

**`poc_inject.py` and `living_pathway.py` docstrings claimed the processor
inserts `1D` commands "asynchronously".** It does not. The command *replaces*
the device's poll in the round-robin:

```
normal:   ... 6f 00 | 70 00 | 02 00 | 72 00 ...
command:  ... 6f 00 | 70 06 1d 00 00 00 04 c3 | 73 00 | 02 00 | 71 06 1d ... 
```

**The MC2E rebooted 2026-09-01 at ~11:02 local**, during the CNTBLOCK install.
Its clock runs about 2h10m ahead of the Mac's, so console timestamps look wrong.
This explains the quieter bus in the 11:17 capture (`0x02` traffic down 80%).

---

## Protocol facts established

**Cresnet framing:** `<dest> <size> <payload>`. No checksum, no delimiter. A poll
is `<addr> 00`. The master polls `0x62`-`0x6F` at 24.2/s and `0x70`-`0x76` at
2.2/s. The `0x62` roll call is metronomic to two decimal places across days, and
makes an excellent liveness gate and corruption detector.

**Cresnet opcodes:** `03` update request / all clear, `14` analog joins, `1C`
re-initialisation carrying a channel map, `1D` set channel level.

**`1D` frame:** `<dest> <size> 1D 00 <fade hi> <fade lo> <channel> <level>`, with
the channel/level pair repeating for multi-channel loads (`size` 0x06 for one
channel, 0x08 for two). The fade field is 16 bits and varies with the button:

| button | fade field | meaning |
|---|---|---|
| keypad, and XPanel "on" | `00 00` | instant |
| XPanel preset | `00 C8` | 200, about two seconds |
| XPanel raise | `01 F4` | 500, slow ramp |
| XPanel lower | `00 18` | 24, fast fade |

This was got wrong three times in one day: first read as a single byte, then
hard-coded to `00` in a frame matcher, then hard-coded to size `0x06`. Each
error silently hid real frames from the bus analysis.

**`1C` channel map** gives per-module channel configuration. The flag reads as
*this channel dims* rather than switches, and holds for every channel with
independent evidence. Full table is in the homeassistant repo's
`crestron-migration.md`.

**CIP digital join, both directions:** datatype `0x00`, then the 0-based join low
byte, then a byte whose top bit is *set* for low and clear for high. Outbound
packet for join 24 press: `05 00 06 00 00 03 00 17 00`, release `... 17 80`.

**The master only listens during a device's reply window.** It attributed our
injected bytes to `ID-6A` and `ID-63`, whichever device it had just polled, and
logged only 2 of 261 bursts.

---

## Tools

All in `mac/`, all ruff clean. None have tests; that is the obvious debt if any
graduates from proof-of-concept.

| file | what it does |
|---|---|
| `crestron_console.py` | telnet console client (port 23), read-only by intent |
| `ctp_getfile.py` | XMODEM file retrieval over CTP (port 41795) |
| `sdebug.py` | scoped SDEBUG capture with guaranteed teardown |
| `cip_xpanel.py` | CIP client, registers on any host/IP-ID, listen-only |
| `poc_joinwatch.py` | three passive streams while someone presses a keypad |
| `poc_joinpress.py` | **presses an XPanel join** — the only tool that writes over IP |
| `poc_joinscan.py` | scans a join range, recording what each one drives |
| `poc_witness.py` | asks the processor whether it hears our Cresnet writes |
| `poc_override.py` | tests whether the processor undoes our Cresnet writes |
| `poc_inject.py`, `living_pathway.py` | Cresnet injection, superseded, kept as record |

Two console gotchas: `XGETFILE` needs CTP not telnet, and the filename must be
**unquoted** (quoting gets "File(s) ... Not Found").

Every tool that arms SDEBUG restores the processor's original flags in a
`finally` block. Confirmed clean after every run today. If a session dies before
teardown, flags stay armed on the processor; clear them with `SDEBUG -S1` to
inspect and `-DOFF`/`-RXROFF`/`-TXROFF`/`-SUON`/`-OOFF` to restore.

---

## The house

| Cresnet ID | Device | Room |
|---|---|---|
| 62, 66 | CNX-B8 keypad | 201 Master Bed |
| 63 | CNX-B8 | 104 Outdoor Kitchen |
| 64, 6F | CNX-B8 | 101 Kitchen |
| 65 | CNX-B8 | 202 Master Bathroom |
| 67 | CNX-B8 | 103 Foyer |
| 6A | CNX-B8 | 105 Great Room |
| 6D | CNX-B8 | 203 Studio |
| 70, 71, 72 | CLX-1DIM8 | 106 Garage rack |
| 73, 75, 76 | CLX-1DIM4 | 106 Garage rack |
| 74 | CLX-4HSW4 | 106 Garage rack |

Known load mapping, all confirmed by simultaneous bus and EISC capture:

| Light | Module / channel | Keypad |
|---|---|---|
| Living Pathway | `0x70` ch4 **and** `0x71` ch3, level `C3` | Great Room `0x6A` |
| Entry Center | `0x71` ch1, `FF` | Foyer `0x67` |
| Sink Area | `0x71` ch6, `FF` | Studio `0x6D` |
| Pool Bath | `0x72` ch6, `FF` | touch panel |

XPanel joins known: **digital 24 and 35, analog 21** all move with Living
Pathway. The XPanel's press namespace is joins **21 through 95**, named
`press21`..`press95` in the program binary.

The program: `Gale Favela 11-14-08.bin`, D3 Pro 2.8.29, compiled 2011-08-23,
SIMPL Windows v3.02.04, on an MC2E running v4.001.1012 (Feb 2009) at
`192.168.4.59`. Retrieve it in about four seconds with:

```
uv run python ctp_getfile.py "Gale Favela 11-14-08.bin" gale.bin
strings -n 4 gale.bin > gale.strings.txt
```

It is deliberately **not committed** — see the note at the bottom of this file.

---

## Known defects in our own tools

- **`poc_joinscan.py` does not restore non-toggle buttons.** It presses each
  join twice on the assumption everything toggles. That holds for join 35 but
  not for the level presets (30-34), where a second press simply re-applies the
  preset. A scan therefore leaves each room lit at whatever the last preset it
  hit commands. After the 21-40 scan the Kitchen sat at 25% until cleared by
  hand. Fix by pressing the room's off-preset after each group button, or by
  recording state and restoring explicitly.
- `poc_joinwatch.py` still searches for `1D` frames with the keypad's
  `1d 00 00 00` form hard-coded, so it misses the XPanel's `1d 00 00 c8` form
  and reports "0 level commands" when frames are present. `poc_joinpress.py`
  had the same bug plus a hard-coded size; both are fixed there and the fix
  should be lifted into a shared helper.
- `cip_xpanel.py` has no tests despite having had three decoding bugs that
  produced a day-long false negative.

---

## A note on what is not in this repo

Both `pdehlke/CresnetMon` and `pdehlke/homeassistant` are **public**. The
compiled program binary and its extracted strings are the complete control
program for a private residence, including every room and load name. They are
not committed, and nothing here needs them: `ctp_getfile.py` re-fetches the
binary in four seconds and one `strings` call reproduces the rest.

If you want them versioned, put them somewhere private rather than here.
