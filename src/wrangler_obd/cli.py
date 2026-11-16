"""Command line interface: ``wrangler-obd <command> [options]``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import alerts, economy, idle as idle_mod, pedal as pedal_mod, report as report_mod, scan as scan_mod
from . import dtc as dtc_mod
from .elm327 import Elm327, ElmError
from .logger import CsvLogger, record
from .pids import DEFAULT_LIVE, PIDS, PIDS_BY_NUMBER, to_imperial
from .simulator import SimulatedTransport
from .transport import SerialTransport, TcpTransport, TransportError


def _open(args: argparse.Namespace) -> Elm327:
    if args.sim:
        transport = SimulatedTransport()
    elif args.tcp:
        host, _, port = args.tcp.partition(":")
        transport = TcpTransport(host, int(port or 35000))
    elif args.port:
        transport = SerialTransport(args.port, args.baud)
    else:
        raise SystemExit("choose an adapter: --port /dev/ttyUSB0, --tcp 192.168.0.10:35000, or --sim")
    elm = Elm327(transport, timeout=args.timeout)
    elm.connect()
    return elm


def _fmt(key: str, value: float, metric: bool) -> str:
    unit = PIDS[key].unit
    if not metric:
        value, unit = to_imperial(value, unit)
    return f"{value:8.1f} {unit}" if unit != "V" else f"{value:8.2f} {unit}"


def _keys(arg: str | None) -> list[str]:
    if not arg:
        return list(DEFAULT_LIVE)
    keys = [k.strip() for k in arg.split(",") if k.strip()]
    unknown = [k for k in keys if k not in PIDS]
    if unknown:
        raise SystemExit(f"unknown PID(s): {', '.join(unknown)}. Run 'wrangler-obd pids --all' to list them.")
    return keys


def cmd_info(elm: Elm327, args: argparse.Namespace) -> None:
    print(f"Adapter:   {elm.version}")
    print(f"Protocol:  {elm.protocol}")
    print(f"VIN:       {elm.vin() or 'not reported'}")
    volts = elm.battery_voltage()
    print(f"Battery:   {volts:.1f} V" if volts is not None else "Battery:   unknown")
    status = elm.monitor_status()
    if status:
        print(f"Check engine light: {'ON' if status['mil_on'] else 'off'} ({status['dtc_count']} stored code(s))")


def cmd_pids(elm: Elm327, args: argparse.Namespace) -> None:
    supported = elm.supported_pids()
    for pid in sorted(supported):
        p = PIDS_BY_NUMBER.get(pid)
        if p:
            print(f"  {pid:02X}  {p.key:<16} {p.name}")
        elif args.all:
            print(f"  {pid:02X}  {'(not decoded yet)':<16}")


def cmd_live(elm: Elm327, args: argparse.Namespace) -> None:
    keys = _keys(args.pids)
    supported = elm.supported_pids()
    keys = [k for k in keys if PIDS[k].pid in supported]
    tty = sys.stdout.isatty()
    try:
        while True:
            readings = elm.read_pids(keys)
            if tty:
                sys.stdout.write("\x1b[H\x1b[2J")
            print(time.strftime("%H:%M:%S"))
            for k in keys:
                r = readings.get(k)
                print(f"  {PIDS[k].name:<38} {_fmt(k, r.value, args.metric) if r else '     --'}")
            for alert in alerts.check({k: r.value for k, r in readings.items()}):
                print(f"  !! {alert.severity.upper()}: {alert.message}")
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_dtc(elm: Elm327, args: argparse.Namespace) -> None:
    report = elm.read_dtcs()
    if report.mil_on is not None:
        print(f"Check engine light: {'ON' if report.mil_on else 'off'}")
    for label, codes in (("Stored", report.stored), ("Pending", report.pending), ("Permanent", report.permanent)):
        print(f"{label} codes: {'none' if not codes else ''}")
        for code in codes:
            print(f"  {code}  {dtc_mod.describe(code)}")


def cmd_freeze(elm: Elm327, args: argparse.Namespace) -> None:
    frame = elm.freeze_frame()
    if frame is None:
        print("No freeze frame stored.")
        return
    print(f"Conditions when {frame.dtc} set ({dtc_mod.describe(frame.dtc)}):")
    for key, r in frame.readings.items():
        print(f"  {r.pid.name:<38} {_fmt(key, r.value, args.metric)}")


def cmd_readiness(elm: Elm327, args: argparse.Namespace) -> None:
    status = elm.monitor_status()
    if not status:
        print("The PCM did not report monitor status.")
        return
    for m in status["monitors"]:
        print(f"  {m['name']:<28} {'complete' if m['complete'] else 'NOT complete'}")
    pending = [m["name"] for m in status["monitors"] if not m["complete"]]
    print("Ready for an emissions test." if not pending else f"Not ready: {len(pending)} monitor(s) still to run.")
    since = elm.read_pids(["dist_since_clear", "warmups_since_clear"])
    if "dist_since_clear" in since:
        print(f"Since codes were last cleared:{_fmt('dist_since_clear', since['dist_since_clear'].value, args.metric)}", end="")
        print(f", {since['warmups_since_clear'].value:.0f} warm-ups" if "warmups_since_clear" in since else "")


def cmd_tests(elm: Elm327, args: argparse.Namespace) -> None:
    results = elm.test_results()
    if not results:
        print("No on-board test results reported (they reset when codes are cleared).")
        return
    for t in results:
        verdict = "pass" if t.passed else "FAIL"
        if t.open_ended:
            limits = ""
        else:
            limits = f"  (limits {t.minimum:g} to {t.maximum:g})"
            if t.margin is not None and t.margin < 0.1:
                verdict = "pass, close to limit"
        print(f"  {t.monitor:<34} {t.test:<46} {t.value:>10g} {t.unit:<8} {verdict}{limits}")


def cmd_report(elm: Elm327, args: argparse.Namespace) -> None:
    os.makedirs(args.out, exist_ok=True)
    summary, log_name, mpg = None, None, None
    speed_factor = args.speed_factor or (
        economy.tire_speed_factor(args.tire, args.tire_calibrated) if args.tire and args.tire_calibrated else 1.0
    )
    if args.duration:
        log_name = "log.csv"
        supported = elm.supported_pids()
        keys = [k for k in PIDS if PIDS[k].pid in supported and k not in report_mod.SLOW_PIDS]
        samples: list[dict[str, float]] = []
        timed: list[tuple[float, dict[str, float]]] = []

        def keep(values: dict[str, float]) -> None:
            samples.append(values)
            timed.append((time.monotonic() - start, values))

        print(f"Recording {len(keys)} PIDs for {args.duration:.0f}s. Ctrl+C to stop early.")
        start = time.monotonic()
        with open(os.path.join(args.out, log_name), "w", newline="", encoding="utf-8") as fh:
            try:
                record(elm, CsvLogger(fh, keys), interval=args.interval, duration=args.duration, on_sample=keep)
            except KeyboardInterrupt:
                pass
        summary = report_mod.summarize_log(timed, time.monotonic() - start)
        mpg = economy.summarize(timed, speed_factor, args.fuel_factor)
    data = report_mod.build_report(elm, summary, log_name, notes=args.notes or "", fuel_economy=mpg)
    for name, text in (("report.json", report_mod.to_json(data)), ("report.md", report_mod.to_markdown(data))):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"Saved report.md and report.json" + (" and log.csv" if log_name else "") + f" in {args.out}")
    if mpg and mpg["mpg"]:
        print(f"  Estimated {mpg['mpg']} mpg over {mpg['distance_miles']} miles.")
    for f in data["findings"]:
        print(f"  {f['severity']}: {f['message']}")


def cmd_idle(elm: Elm327, args: argparse.Namespace) -> None:
    os.makedirs(args.out, exist_ok=True)
    print("Idle check. For a clean baseline: engine fully warm, in Park, A/C and stereo off, hands off the pedals.")
    print(f"Sampling for {args.duration:.0f}s. Ctrl+C to stop early.")
    before = idle_mod.misfire_counts(elm)
    supported = elm.supported_pids()
    keys = [k for k in idle_mod.IDLE_PIDS if PIDS[k].pid in supported]
    with open(os.path.join(args.out, "idle-log.csv"), "w", newline="", encoding="utf-8") as fh:
        try:
            samples = idle_mod.capture(elm, args.duration, CsvLogger(fh, keys), keys)
        except KeyboardInterrupt:
            samples = []
            print("Stopped early; the log is saved but was not analysed. Run again for the full analysis.")
    baro = elm.read_pid("baro") if PIDS["baro"].pid in supported else None
    analysis = idle_mod.analyze(samples, before, idle_mod.misfire_counts(elm), baro.value if baro else None)
    data = report_mod.build_report(elm, None, "idle-log.csv", notes=args.notes or "", idle_check=analysis)
    for name, text in (("idle-report.json", report_mod.to_json(data)), ("idle-report.md", report_mod.to_markdown(data))):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"Saved idle-report.md, idle-report.json and idle-log.csv in {args.out}")
    for note in analysis["verdict"]:
        print(f"  - {note}")


def _hex(text: str) -> int:
    return int(text, 16)


def cmd_scan(elm: Elm327, args: argparse.Namespace) -> None:
    if args.service == "both":
        ranges = [(0x21, 0x00, 0xFF), (0x22, args.start, args.end)]
    elif args.service == "21":
        ranges = [(0x21, max(0, min(args.start, 0xFF)), min(args.end, 0xFF))]
    else:
        ranges = [(0x22, args.start, args.end)]
    status = elm.read_pids(["rpm", "speed"])
    if not args.sim and (status.get("rpm") and status["rpm"].value > 0 or status.get("speed") and status["speed"].value > 0):
        raise SystemExit("Scan only with the engine off: park, key on, engine off, then run it again.")
    total = sum(b - a + 1 for _, a, b in ranges)
    print(f"Asking the {args.module} module for {total} identifiers (read-only). Ctrl+C stops and keeps what was found.")
    started = time.monotonic()

    def progress(done: int, of: int, found: int) -> None:
        rate = done / max(time.monotonic() - started, 0.001)
        print(f"\r  {done}/{of} in this range, {found} answered, {rate:.0f} per second   ", end="", flush=True)

    result = scan_mod.run(elm, args.module, ranges, label=args.label or "", progress=progress)
    print()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    state = "" if result["complete"] else " (stopped early)"
    print(f"Saved {len(result['items'])} item(s) to {args.out}{state}")
    if args.compare:
        changed = scan_mod.compare(scan_mod.load(args.compare), result)
        print(f"{len(changed)} item(s) differ from {args.compare}:")
        for row in changed:
            numbers = f"   ({row['before_number']} -> {row['after_number']})" if "before_number" in row else ""
            print(f"  {row['item']}  {row['before']} -> {row['after']}{numbers}")


def cmd_pedal(elm: Elm327, args: argparse.Namespace) -> None:
    os.makedirs(args.out, exist_ok=True)
    supported = elm.supported_pids()
    keys = [k for k in pedal_mod.PEDAL_PIDS if PIDS[k].pid in supported]
    print("Pedal map. Parked, key on, engine off: press the pedal slowly to the floor over about")
    print(f"10 seconds, release it slowly, and repeat until the {args.duration:.0f}s are up.")
    with open(os.path.join(args.out, "pedal-log.csv"), "w", newline="", encoding="utf-8") as fh:
        try:
            samples = idle_mod.capture(elm, args.duration, CsvLogger(fh, keys), keys)
        except KeyboardInterrupt:
            samples = []
    result = pedal_mod.pedal_map([v for _, v in samples])
    with open(os.path.join(args.out, "pedal-map.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(args.out, "pedal-map.md"), "w", encoding="utf-8") as fh:
        fh.write(pedal_mod.to_markdown(result, args.notes or ""))
    print(f"Saved pedal-map.md, pedal-map.json and pedal-log.csv in {args.out}")
    print(result.get("note") or f"  {len(result['curve'])} rows in the pedal-to-throttle table.")


def cmd_ports() -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is required: pip install pyserial", file=sys.stderr)
        return 2
    ports = sorted(list_ports.comports())
    if not ports:
        print("No serial ports found. Plug in the adapter; on Windows it needs the FTDI driver to show up as a COM port.")
    for p in ports:
        print(f"  {p.device:<14} {p.description}")
    return 0


def cmd_clear(elm: Elm327, args: argparse.Namespace) -> None:
    if not args.yes:
        print("This clears stored codes, freeze frame data and readiness monitors.")
        print("The truck will show 'not ready' for emissions until it completes a drive cycle.")
        print("Do this with the engine OFF and the key ON.")
        if input("Type CLEAR to continue: ").strip() != "CLEAR":
            print("Cancelled.")
            return
    elm.clear_dtcs()
    print("Codes cleared. Permanent codes stay until the PCM sees the fault is fixed.")


def cmd_log(elm: Elm327, args: argparse.Namespace) -> None:
    keys = _keys(args.pids)
    supported = elm.supported_pids()
    keys = [k for k in keys if PIDS[k].pid in supported]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        logger = CsvLogger(fh, keys)
        print(f"Logging {len(keys)} PIDs to {args.out} every {args.interval}s. Ctrl+C to stop.")
        try:
            n = record(elm, logger, interval=args.interval, duration=args.duration)
        except KeyboardInterrupt:
            n = None
    print(f"Saved {args.out}" + (f" ({n} samples)" if n is not None else ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wrangler-obd", description="OBD-II monitor for the 2010 Jeep Wrangler JK")
    conn = parser.add_argument_group("adapter")
    conn.add_argument("--port", help="serial port of a USB/Bluetooth ELM327, e.g. /dev/ttyUSB0 or COM3")
    conn.add_argument("--baud", type=int, help="serial baud rate (default: auto-detect)")
    conn.add_argument("--tcp", help="Wi-Fi ELM327 host[:port], e.g. 192.168.0.10:35000")
    conn.add_argument("--sim", action="store_true", help="use the built-in simulator instead of a real adapter")
    conn.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for each reply")
    parser.add_argument("--metric", action="store_true", help="show °C, km/h, kPa instead of US units")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="adapter, protocol, VIN, battery and check engine light")
    p = sub.add_parser("pids", help="list PIDs the PCM supports")
    p.add_argument("--all", action="store_true", help="include supported PIDs this tool can't decode yet")
    p = sub.add_parser("live", help="live dashboard in the terminal")
    p.add_argument("--pids", help="comma-separated PID keys (default: core engine set)")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--once", action="store_true", help="print one sample and exit")
    sub.add_parser("dtc", help="read stored, pending and permanent trouble codes")
    sub.add_parser("freeze", help="freeze frame: sensor values saved when a code set")
    sub.add_parser("readiness", help="readiness monitors (emissions test status)")
    sub.add_parser("tests", help="on-board test results (Mode 06): misfire counts, catalyst, O2, EVAP")
    p = sub.add_parser("report", help="write a full diagnostic report (Markdown + JSON) for a person or an LLM")
    p.add_argument("--out", required=True, help="folder to write report.md, report.json and log.csv into")
    p.add_argument("--duration", type=float, help="also record live data for this many seconds and summarize it")
    p.add_argument("--interval", type=float, default=0.0, help="seconds between samples (default: as fast as possible)")
    p.add_argument("--notes", help="symptoms or context to include, e.g. 'rough idle when cold'")
    p.add_argument("--tire", type=float, help="actual tire diameter in inches, e.g. 33")
    p.add_argument("--tire-calibrated", type=float, help="tire diameter the truck's computer is set up for, in inches")
    p.add_argument("--speed-factor", type=float,
                   help="true speed divided by the speed the truck reports (from a GPS check); overrides --tire")
    p.add_argument("--fuel-factor", type=float, default=1.0,
                   help="gallons at the pump divided by gallons this tool estimated, from a fill-up check")
    p = sub.add_parser("idle", help="idle check: find small RPM stumbles and which cylinder or bank is behind them")
    p.add_argument("--out", required=True, help="folder to write idle-report.md, idle-report.json and idle-log.csv into")
    p.add_argument("--duration", type=float, default=180, help="seconds to sample (default 180)")
    p.add_argument("--notes", help="conditions for this run, e.g. 'in Drive, A/C on'")
    p = sub.add_parser("scan", help="find manufacturer data (e.g. transmission temperature) by asking a module for every item")
    p.add_argument("--out", required=True, help="JSON file to write")
    p.add_argument("--module", choices=sorted(scan_mod.MODULES), default="trans", help="module to ask (default: trans)")
    p.add_argument("--service", choices=["21", "22", "both"], default="21",
                   help="21 = 256 items, about a minute; 22 = up to 65536 items, can take an hour (default: 21)")
    p.add_argument("--start", type=_hex, default=0x0000, help="first identifier, hex (default 0000)")
    p.add_argument("--end", type=_hex, default=0xFFFF, help="last identifier, hex (default FFFF)")
    p.add_argument("--label", help="conditions, e.g. 'cold, sat overnight' or 'hot, after 30 min drive'")
    p.add_argument("--compare", help="an earlier scan file; print the items that changed since then")
    p = sub.add_parser("pedal", help="record how far the throttle opens for each pedal position")
    p.add_argument("--out", required=True, help="folder to write pedal-map.md, pedal-map.json and pedal-log.csv into")
    p.add_argument("--duration", type=float, default=45, help="seconds to sample (default 45)")
    p.add_argument("--notes", help="conditions, e.g. 'stock' or 'throttle controller on Sport'")
    sub.add_parser("ports", help="list serial (COM) ports to find the adapter")
    p = sub.add_parser("clear", help="clear trouble codes and turn off the check engine light")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p = sub.add_parser("log", help="record PIDs to a CSV file")
    p.add_argument("--out", required=True, help="CSV file to write")
    p.add_argument("--pids", help="comma-separated PID keys (default: core engine set)")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    return parser


COMMANDS = {
    "freeze": cmd_freeze, "readiness": cmd_readiness, "tests": cmd_tests, "report": cmd_report, "idle": cmd_idle, "scan": cmd_scan, "pedal": cmd_pedal,
    "info": cmd_info, "pids": cmd_pids, "live": cmd_live, "dtc": cmd_dtc, "clear": cmd_clear, "log": cmd_log,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ports":
        return cmd_ports()
    try:
        elm = _open(args)
    except (TransportError, ElmError) as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        return 2
    try:
        COMMANDS[args.command](elm, args)
    except (TransportError, ElmError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        elm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
