# Jeep Wrangler Interface

OBD-II monitoring and diagnostics for a **2010 Jeep Wrangler JK (2-door, 3.8L V6)**
through an ELM327-compatible adapter.

## Hardware

Any ELM327-compatible adapter works: USB, Bluetooth (serial), or Wi-Fi. A genuine
or OBDLink-class adapter is more reliable than cheap clones. The 2010 JK's PCM speaks
ISO 15765-4 CAN (11-bit, 500 kbaud), which the tool detects automatically.

The OBD port is under the dash, left of the steering column.

## The easy way: the app

Double-click **`Wrangler Diagnostics.pyw`** in this folder. It opens in your web browser.
No typing needed. If Windows asks which program to open the `.pyw` file with, double-click
`Wrangler Diagnostics.bat` instead. To put it on the desktop, right-click the `.pyw` file
and choose Send to > Desktop (create shortcut). If the app fails to start, the `.pyw` shows
the error in a pop-up and saves it to `Documents\jeep-data\last-error.txt`. The `.bat` runs
the app in a window that stays open, so any error stays on screen.

Press **Connect**, then use the **Dashboard** tab:

- **Start monitoring** records every reading the truck answers (all supported standard
  readings plus the Chrysler extras such as transmission temperature, when the truck
  reports them) until you press **Stop and save**.
- **Tiles**: pick a reading from the list and press Add tile. Each tile shows the current
  value, the low and high, and a trend line. Drag tiles to rearrange them, press the x to
  remove one. Your layout is remembered. The window selector sets how far back the trend
  lines go (1 to 60 minutes).
- **Markers**: type what you just changed ("A/C on", "cleaned throttle body") and press
  Add marker. It shows as a line on every trend and the saved report compares the
  readings before and after it.
- **Likely causes**: while recording, the app checks combinations of readings against
  known fault patterns for the 3.8L (vacuum leak, fuel delivery, thermostat, charging,
  lazy oxygen sensor, catalyst, rough idle and more) and ranks what fits, each with the
  evidence and the next thing to check. It is a pointer, not a verdict. It needs about
  20 seconds of warm idle, and a few minutes of steady cruising to tell a vacuum leak
  from a fuel delivery problem.
- **Notes** (optional): anything worth knowing about the recording, such as "stereo on" or "A/C on".
  You can edit it while recording; it is saved with the report when you press Stop and save.
- **Tire size**: enter the diameter of the tires on the truck and the size the truck is set up for
  (stock 2010 Sahara 255/70R18 is about 32.1 in). Speed, distance and mpg are corrected for the
  difference. If the speedometer already matches a GPS, enter the same size in both.
- **Stop and save** writes a folder under `Documents\jeep-data` holding `report.md` and
  `report.json` (written for an AI model or a mechanic to read: codes, readiness and on-board
  test results, statistics for every reading, likely causes, before/after each marker), `log.csv` (every sample) and
  `markers.json`.

**Updates** happen by themselves: each time you open the app with an internet connection, it
checks GitHub for a newer version and installs it before the window opens (your recordings and
settings are not touched). Offline, it starts as usual. You can turn this off, or check by hand,
under "App version" on the Dashboard tab. This needs the GitHub repository to be public.

The **Guided steps** tab keeps the one-button procedures (pedal map, record a drive, idle
checks, transmission temperature scan, make a ZIP to send).

The app runs only on this computer (it is a small local web server, nothing is sent
anywhere) and closes by itself a couple of minutes after its browser tab is closed. The
first time, run `pip install -e .` in this folder once so `pyserial` is installed.

## In-Jeep logger (Raspberry Pi)

A Raspberry Pi (3 Model A+, Zero 2 W or Zero W) with a LiFePO4wered/Pi+ power board can live in the Jeep,
record every drive by itself and send it to the PC over home WiFi. The app's
**In-Jeep logger** tab installs it on the Pi with one button and lists the drives
that arrive. Shopping list and setup steps: [docs/raspberry-pi.md](docs/raspberry-pi.md).

## Install

```bash
pip install -e .            # or: pip install -e .[dev] for tests
```

Requires Python 3.10+ and `pyserial`. The serial baud rate is auto-detected; pass `--baud` to force one.

## Usage

Pick the adapter with `--port` (USB/Bluetooth), `--tcp` (Wi-Fi), or `--sim` (built-in
simulator, no truck needed).

```bash
wrangler-obd --sim info                       # adapter, protocol, VIN, battery, CEL
wrangler-obd --port /dev/ttyUSB0 pids         # PIDs your PCM supports
wrangler-obd --tcp 192.168.0.10:35000 live    # live terminal dashboard
wrangler-obd --port COM3 live --pids rpm,coolant_temp,ltft_b1,ltft_b2
wrangler-obd --port /dev/rfcomm0 dtc          # stored, pending, permanent codes
wrangler-obd --port /dev/ttyUSB0 clear        # clear codes (engine off, key on)
wrangler-obd --port /dev/ttyUSB0 log --out drive.csv --duration 600
wrangler-obd --port COM3 freeze               # sensor values saved when a code set
wrangler-obd --port COM3 readiness            # emissions monitors complete or not
wrangler-obd --port COM3 tests                # Mode 06: misfire counts, catalyst, O2, EVAP
wrangler-obd --port COM3 report --out jeep-report --duration 300 --notes "rough idle when cold"
```

Values display in US units by default; add `--metric` for °C, km/h and kPa.
CSV logs are always in metric (SAE native units) so they're unambiguous.

### Live PIDs

| key | description |
|---|---|
| `rpm`, `speed`, `engine_load`, `throttle`, `map`, `maf` | engine and airflow |
| `coolant_temp`, `intake_temp`, `ambient_temp` | temperatures |
| `stft_b1`, `ltft_b1`, `stft_b2`, `ltft_b2` | fuel trims, both banks |
| `o2_b1s1`, `o2_b1s2`, `o2_b2s1`, `o2_b2s2` | O2 sensor voltages |
| `timing_advance`, `module_voltage`, `fuel_level`, `baro`, `run_time` | misc |

### Diagnostic report (for you or an LLM)

`report` writes a folder with three files:

- `report.md`: a readable summary you can paste or upload into a chat with an LLM. It
  opens with a short prompt, then findings, trouble codes, freeze frame, readiness
  monitors, Mode 06 test results and a snapshot of every reading.
- `report.json`: the same data in a stable structure (`"schema": "wrangler-obd-report/1"`).
- `log.csv`: the raw recording, only when `--duration` is given.

With `--duration`, the summary also includes statistics split into warm idle and warm
cruise (a lean fuel trim only at idle suggests a vacuum leak, lean everywhere suggests
fuel delivery or the MAP sensor) and how actively the upstream O2 sensors switch.
For the most useful report, start with a warm engine, let it idle for a minute, then
drive at a steady speed for a few minutes. Report numbers are always metric.

`live` and `report` flag hot coolant, low or high system voltage and fuel trims that are
far from zero. Thresholds are in `alerts.py`.

### Fuel economy

Any `report` run with `--duration` includes an estimated mpg for the drive, split by speed
(stopped, under 30, 30 to 55, over 55 mph) with the share of fuel burned in each.

```bash
wrangler-obd --port COM3 report --out commute --duration 1800 --speed-factor 1.03
```

The 3.8L has no air flow sensor, so fuel use is worked out from manifold pressure, RPM
and intake temperature. Treat the absolute number as approximate until you calibrate it;
comparisons between drives are sound from the start. Two corrections:

- **Speed.** If the tires are a different size than the computer is set up for, the
  reported speed and distance are off. Hold a steady 60 mph on the speedometer, read a
  phone GPS, and pass `--speed-factor` as GPS speed divided by 60. Or pass
  `--tire 33 --tire-calibrated 32` with both diameters in inches.
- **Fuel.** Fill the tank, log every drive, fill again, then pass `--fuel-factor` as the
  gallons the pump shows divided by the gallons the reports added up to.

### Idle check

```bash
wrangler-obd --port COM3 idle --out idle-baseline --notes "Park, A/C off, stereo off"
```

For a stumble at idle that is too small to see on the tach. It samples RPM, short term
fuel trims, upstream O2 sensors, manifold pressure, spark timing and voltage several
times a second for three minutes, along with commanded EVAP purge and EGR, then reports each RPM dip with what the other readings
did at that moment, whether the dips come at a regular rhythm, and how many new misfires
the PCM counted per cylinder during the run. Output is `idle-report.md`,
`idle-report.json` and `idle-log.csv`.

Run it once as a baseline (fully warm, Park, everything off), then again while changing
one thing at a time (in Drive with the brake held, A/C on, stereo on) and compare.
A very brief dip can fall between samples, so the PCM's misfire counters are the more
reliable evidence when the two disagree.

### Transmission temperature and other manufacturer readings

Every `report` also tries a short list of Chrysler-specific requests and includes any the
truck answers under "Manufacturer readings": transmission fluid temperature (three known
variants), engine oil temperature and pressure, knock sensors and injector pulse widths.
The identifiers come from the community [OBDb Wrangler JK signal set](https://github.com/OBDb/Jeep-Wrangler-JK)
(CC BY-SA 4.0). That set spans every JK engine and year and its own test data shows a
2010 truck rejecting some of them, so each is marked unconfirmed until checked on this
truck. The list is in `extras.py`.

If none of the transmission variants answers, `scan` can find the reading on the truck
itself by asking the transmission module for every item it has (read-only):

```bash
wrangler-obd --port COM3 scan --out cold.json --label "cold, sat overnight"
# drive 30 minutes, park, key on, engine off
wrangler-obd --port COM3 scan --out hot.json --label "hot" --compare cold.json
```

The second run prints the items that changed. The one that went from about ambient to
operating temperature is the fluid temperature. Run scans parked, key on, engine off.

### Pedal map

```bash
wrangler-obd --port COM3 pedal --out pedal-stock --notes stock
```

Records how far the PCM opens the throttle for each pedal position (key on, engine off,
press the pedal slowly to the floor and release). Useful as a stock baseline to compare
against a throttle controller or a tune.

### Using an AI agent

`AGENTS.md` is a playbook for Claude Code, Codex or a similar agent: setup, which command
answers which question, and step-by-step procedures for an idle stumble, finding
transmission temperature and measuring throttle response.

### Windows setup

1. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
2. Plug in the OBDLink EX. Windows should install its driver and give it a COM port.
3. In the project folder run `pip install -e .`, then `wrangler-obd ports` to find the
   COM port, then `wrangler-obd --port COM3 info` (with your port) with the key on.

## Library use

```python
from wrangler_obd import Elm327, SerialTransport

elm = Elm327(SerialTransport("/dev/ttyUSB0"))
elm.connect()
print(elm.read_pid("rpm").value)
print(elm.read_dtcs())
```

## Layout

- `transport.py`: serial and TCP byte transports
- `elm327.py`: adapter setup, response parsing (incl. multi-frame CAN), Mode 01/02/03/04/06/07/09/0A
- `pids.py`: PID definitions, decoders, unit conversion
- `dtc.py`: trouble code decoding and descriptions (generic + common JK codes)
- `mode06.py`: on-board test result decoding (monitor names, unit scaling)
- `idle.py`: idle check (RPM dip detection, misfire counter change)
- `economy.py`: fuel use and mpg estimate from MAP, RPM and intake temperature
- `extras.py`: Chrysler-specific readings to try (from the OBDb signal set)
- `scan.py`: finds manufacturer data items (services 21 and 22) on one module
- `pedal.py`: pedal-to-throttle curve
- `app.py`, `app.html`: the point-and-click app (local web page)
- `updater.py`: checks GitHub for a newer version at launch and installs it
- `monitor.py`: background recorder behind the dashboard (all readings, markers, saved report)
- `troubleshoot.py`: live fault-pattern rules that rank likely causes
- `alerts.py`: thresholds for overheat, voltage and fuel trim warnings
- `report.py`: diagnostic report (JSON + Markdown) and log statistics
- `simulator.py`: fake ELM327 + JK PCM that speaks the real text protocol
- `logger.py`, `cli.py`: CSV logging and the `wrangler-obd` command
- `carlogger.py`: the in-Jeep logger that runs on the Raspberry Pi
- `sync.py`, `receiver.py`: sending drives from the Pi, and the receiver on the PC
- `pisetup.py`, `pi/`: installing the logger on the Pi from the app

## Roadmap

1. **Core** (this release): driver, live PIDs, DTCs, logging, CLI, simulator
2. **Dashboard**: local web UI with gauges, charts and a DTC panel
3. **Diagnostics**: freeze frame, readiness monitors, alerts (overheat, low voltage, trims)
4. **JK extras**: Chrysler Mode 22 PIDs such as transmission temperature
