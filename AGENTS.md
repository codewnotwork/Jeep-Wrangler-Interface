# Agent playbook

This file is for an AI coding agent (Claude Code, Codex, or similar) helping the owner
diagnose a **2010 Jeep Wrangler JK, 2-door, 3.8L V6, 42RLE automatic** with this tool.
The owner is not a programmer. Run the commands for them, read the output files, and
explain what you find in plain language.

## Ground rules

- Everything here except `clear` only reads from the truck. Never run `clear` unless the
  owner asks for it: it erases the freeze frame, readiness monitors and Mode 06 results,
  which are evidence. Save a `report` first.
- Never ask the owner to watch a laptop while driving. Start a recording, drive, stop, read.
- `scan` and `pedal` are for a parked truck with the key on and the engine off.
- This tool cannot change the engine tune, and nothing here should be used to defeat
  emissions controls.
- Say what you measured and what you are inferring. Simulator output (`--sim`) is invented
  data and must never be presented as a reading from the truck.

## Setup (Windows)

1. `pip install -e .` in this folder (Python 3.10+).
2. Plug the OBDLink EX into the port under the dash, left of the steering column, and
   into the laptop. Turn the key to ON.
3. `wrangler-obd ports` lists COM ports. Use the adapter's port below in place of `COM3`.
4. `wrangler-obd --port COM3 info` should show the VIN and "ISO 15765-4 CAN (11 bit, 500 kbaud)".

If it cannot connect: check the key is on, try another USB port, and pass `--baud 115200`.

## The app

The owner prefers not to type commands. Point them to `Wrangler Diagnostics.pyw`, which
runs every step below from buttons and saves each run in its own folder under
`Documents/jeep-data`, with a "Make ZIP to send" button. Use the commands below only when
you are running things for them yourself.

## Commands and what to read

| Goal | Command | Read |
|---|---|---|
| Full health check | `wrangler-obd --port COM3 report --out reports/<date> --duration 300 --notes "<symptom>"` | `report.md`, then `report.json` |
| Idle stumble or roughness | `wrangler-obd --port COM3 idle --out reports/<date>-idle --notes "<conditions>"` | `idle-report.md` |
| Codes only | `wrangler-obd --port COM3 dtc` | terminal |
| Why a code set | `wrangler-obd --port COM3 freeze` | terminal |
| Emissions readiness | `wrangler-obd --port COM3 readiness` | terminal |
| Misfire counts, catalyst, O2, EVAP tests | `wrangler-obd --port COM3 tests` | terminal |
| Pedal to throttle curve | `wrangler-obd --port COM3 pedal --out reports/<date>-pedal` | `pedal-map.md` |
| Find hidden data (trans temp) | `wrangler-obd --port COM3 scan --out scans/<label>.json --label "<conditions>"` | the JSON |

Report numbers are metric. Convert for the owner (°F, mph, psi).

## Playbook: stumble at idle

1. Baseline: engine fully warm (coolant above 85 °C), Park, A/C off, stereo off. Run `idle`.
2. Repeat, changing one thing per run and saying so in `--notes`: in Drive with the brake
   held; A/C on; stereo on at volume (the owner has a 750 W amplifier, so watch voltage).
3. In each `idle-report.md` look at, in order:
   - **PCM misfire counters.** New counts on one cylinder point at that cylinder's plug,
     coil pack tower, wire or injector. Counts spread over cylinders point at something shared.
   - **Rhythm.** Evenly spaced dips suggest something cycling (EVAP purge, A/C clutch, fan).
   - **Bank.** Bank 1 holds cylinder 1 (the odd cylinders), bank 2 the even ones. One
     upstream O2 sensor going lean at each dip, or long term trims that differ between banks.
   - **EVAP purge and EGR.** Purge stepping at each dip points at the purge valve or a
     saturated canister. Any commanded EGR at idle, or dips with EGR closed that clear up
     when the EGR valve is unplugged, points at a leaking EGR valve (common on the 3.8L).
   - **Fuel trims.** Lean at idle but normal at cruise (compare with a `report` drive)
     suggests a vacuum leak. Lean everywhere suggests fuel delivery or the MAP sensor.
   - **Voltage** sagging at the dips suggests electrical load or charging.
4. A dip shorter than the sample interval can be missed. If the owner feels a stumble the
   log does not show, trust the misfire counters and say that the sampling may be too slow.
5. Give the owner a ranked list of likely causes with the evidence for each and the next
   cheap check, not a parts list.

## Playbook: finding transmission temperature

`report` already tries three known variants (see `extras.py`, from the OBDb signal set) and
lists any that answer under "Manufacturer readings". If one answers, confirm it: it should
read near ambient after sitting overnight and 70-95 °C after a drive. Then set
`confirmed=True` on that entry and delete the variants that did not answer.

If none answers, find it on the truck:

1. Cold (sat overnight), key on, engine off: `scan --out scans/cold.json --label cold`.
2. After a 30 minute drive, key on, engine off: `scan --out scans/hot.json --label hot --compare scans/cold.json`.
3. The temperature is an item that moved from near ambient to roughly 70-95 °C. Try the
   usual encodings on the changed items: one byte minus 40; two bytes divided by 10, 16 or 64;
   and the same in °F.
4. If service 21 shows nothing plausible, repeat with `--service 22` (slow: up to an hour,
   so consider a battery charger) or `--module engine`.
5. Once found, add it to the code as a named reading and record in the README which
   identifier it was and how it was confirmed.

## Playbook: throttle response

A throttle controller box sits between the pedal and the PCM and makes the pedal signal
rise faster, so the same foot movement asks for more throttle. It does not add power or
change the tune. This tool can measure the effect but cannot create it: `pedal` records the
pedal-to-throttle curve. Record it stock, and again with any change, and compare the tables.
Changing the curve itself needs either that kind of inline hardware or a PCM tune written
with a dedicated flash tool, both outside this project.

## Playbook: fuel economy

The owner's main goal is better fuel economy. `report --duration` gives an estimated mpg
split by speed band. Before comparing anything:

1. Set `--speed-factor` from a GPS check at a steady 60 mph (the truck is on 33" tires and
   the computer may be set up for a smaller size).
2. Set `--fuel-factor` from one tank: pump gallons divided by the sum of estimated gallons.
3. Compare like with like: same route, warm engine, similar weather. Look at the share of
   fuel burned while stopped, fuel trims, coolant temperature reaching 90 °C (a thermostat
   stuck open costs mileage), and upstream O2 sensor switching.

## Code layout

See "Layout" in `README.md`. Tests: `pip install -e .[dev]` then `pytest -q`. Every feature
must work against the simulator (`--sim`) so it can be tested without the truck.
