"""Command line interface: ``wrangler-obd <command> [options]``."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import alerts, report as report_mod
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
    summary, log_name = None, None
    if args.duration:
        log_name = "log.csv"
        supported = elm.supported_pids()
        keys = [k for k in PIDS if PIDS[k].pid in supported and k not in report_mod.SLOW_PIDS]
        samples: list[dict[str, float]] = []
        print(f"Recording {len(keys)} PIDs for {args.duration:.0f}s. Ctrl+C to stop early.")
        start = time.monotonic()
        with open(os.path.join(args.out, log_name), "w", newline="", encoding="utf-8") as fh:
            try:
                record(elm, CsvLogger(fh, keys), interval=args.interval, duration=args.duration, on_sample=samples.append)
            except KeyboardInterrupt:
                pass
        summary = report_mod.summarize_log(samples, time.monotonic() - start)
    data = report_mod.build_report(elm, summary, log_name, notes=args.notes or "")
    for name, text in (("report.json", report_mod.to_json(data)), ("report.md", report_mod.to_markdown(data))):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"Saved report.md and report.json" + (" and log.csv" if log_name else "") + f" in {args.out}")
    for f in data["findings"]:
        print(f"  {f['severity']}: {f['message']}")


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
    p = sub.add_parser("clear", help="clear trouble codes and turn off the check engine light")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p = sub.add_parser("log", help="record PIDs to a CSV file")
    p.add_argument("--out", required=True, help="CSV file to write")
    p.add_argument("--pids", help="comma-separated PID keys (default: core engine set)")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    return parser


COMMANDS = {
    "freeze": cmd_freeze, "readiness": cmd_readiness, "tests": cmd_tests, "report": cmd_report,
    "info": cmd_info, "pids": cmd_pids, "live": cmd_live, "dtc": cmd_dtc, "clear": cmd_clear, "log": cmd_log,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
