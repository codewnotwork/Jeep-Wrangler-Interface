# Jeep Wrangler Interface

OBD-II monitoring and diagnostics for a **2010 Jeep Wrangler JK (2-door, 3.8L V6)**
through an ELM327-compatible adapter.

## Hardware

Any ELM327-compatible adapter works: USB, Bluetooth (serial), or Wi-Fi. A genuine
or OBDLink-class adapter is more reliable than cheap clones. The 2010 JK's PCM speaks
ISO 15765-4 CAN (11-bit, 500 kbaud), which the tool detects automatically.

The OBD port is under the dash, left of the steering column.

## Install

```bash
pip install -e .            # or: pip install -e .[dev] for tests
```

Requires Python 3.10+ and `pyserial`.

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
- `elm327.py`: adapter setup, response parsing (incl. multi-frame CAN), Mode 01/03/04/07/09/0A
- `pids.py`: PID definitions, decoders, unit conversion
- `dtc.py`: trouble code decoding and descriptions (generic + common JK codes)
- `simulator.py`: fake ELM327 + JK PCM that speaks the real text protocol
- `logger.py`, `cli.py`: CSV logging and the `wrangler-obd` command

## Roadmap

1. **Core** (this release): driver, live PIDs, DTCs, logging, CLI, simulator
2. **Dashboard**: local web UI with gauges, charts and a DTC panel
3. **Diagnostics**: freeze frame, readiness monitors, alerts (overheat, low voltage, trims)
4. **JK extras**: Chrysler Mode 22 PIDs such as transmission temperature
