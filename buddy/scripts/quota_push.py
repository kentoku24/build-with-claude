"""Push real Claude usage quota to the Cardputer buddy over BLE.

The buddy's "5h / Week / Sonnet" bars need real quota figures, but the
device is BLE-only and Claude.app's Hardware Buddy heartbeat carries no
quota (see buddy/references/protocol.md). This companion fills that gap:
it reads the quota from **codexbar** and writes heartbeats containing
utilization and pace-stage fields to the device's Nordic-UART RX
characteristic, so the bars track the real account quota.

Backend: `codexbar --provider anthropic --format json`. Its first array
entry has `usage.{primary, secondary, tertiary}.usedPercent` (the bar
length) and `pace.{primary, secondary}.stage` (the bar colour):

    primary   -> five_h_util  + five_h_stage  (5-hour window)
    secondary -> week_util    + week_stage    (7-day, all)
    tertiary  -> sonnet_util                  (7-day, Sonnet; NO pace)

(Mapping verified against the labeled usage API: primary==five_hour,
secondary==seven_day, tertiary==seven_day_sonnet.) The device renders
*remaining* = 100 - utilization for the bar length, and colours each bar
by its codexbar pace `stage` (green=reserve … red=deficit), falling back
to a remaining-% colour where no stage exists (Sonnet, or a window too
early for codexbar to emit pace).

### Connection model (important)

This script is the BLE **central**, exactly like Claude.app. A buddy
accepts one central at a time, so **while this is connected, Claude.app
cannot be** — you get the live quota readout but not prompt-approval.
Run it when you want quota on the desk display; quit it (Ctrl-C) and
reconnect from Claude.app for the approval workflow.

### Requirements

    pip install bleak                # BLE central for macOS/Linux/Windows
    codexbar on PATH, authenticated  # `codexbar --provider anthropic --format json`

### Usage

    python quota_push.py                  # scan for a Claude_* buddy, push forever
    python quota_push.py --interval 30    # poll/push every 30 s (default 60)
    python quota_push.py --address <UUID> # skip the scan, connect directly
    python quota_push.py --once           # connect, push one sample, exit
    python quota_push.py --dry-run        # fetch+print quota only, no BLE

Run it from **Terminal.app**, not from inside another app's shell —
macOS Bluetooth permission is per-binary, so an indirectly-launched
Python aborts with SIGABRT (exit 134). `--dry-run` does no BLE and is
exempt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CODEXBAR_CMD = ["codexbar", "--provider", "anthropic", "--format", "json"]
DEFAULT_NAME_PREFIX = "Claude_"
DEFAULT_INTERVAL = 60


def fetch_quota():
    """Build the heartbeat dict from codexbar.

    Maps usage.primary/secondary/tertiary.usedPercent to the
    five_h_util / week_util / sonnet_util fields, and the codexbar **pace
    stage** (pace.primary/secondary.stage) to five_h_stage / week_stage.
    The device colours each bar by its stage (green=reserve … red=deficit)
    and falls back to a remaining-% colour where no stage is present
    (Sonnet has no pace; codexbar also omits pace early in a window).

    Returns e.g. {"five_h_util": 13, "week_util": 13, "sonnet_util": 6,
    "five_h_stage": "farBehind", "week_stage": "slightlyAhead"}. Absent
    values are simply omitted.
    """
    try:
        out = subprocess.run(
            CODEXBAR_CMD, capture_output=True, text=True, check=True, timeout=20,
        ).stdout
    except FileNotFoundError:
        raise SystemExit(
            "codexbar not found on PATH. Install it (e.g. `brew install codexbar`) "
            "and make sure `codexbar --provider anthropic --format json` works."
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("codexbar timed out.")
    except subprocess.CalledProcessError as e:
        raise SystemExit("codexbar failed (exit %s): %s" % (e.returncode, (e.stderr or "").strip()))

    data = json.loads(out)
    if not data:
        raise RuntimeError("codexbar returned no entries")
    entry = data[0]
    usage = entry.get("usage") or {}
    pace = entry.get("pace") or {}

    hb = {}

    def _used(section, key):
        block = usage.get(section) or {}
        u = block.get("usedPercent")
        if u is not None:
            hb[key] = int(round(u))

    def _stage(section, key):
        block = pace.get(section) or {}
        s = block.get("stage")
        if s:
            hb[key] = s

    _used("primary", "five_h_util")
    _used("secondary", "week_util")
    _used("tertiary", "sonnet_util")
    _stage("primary", "five_h_stage")   # tertiary (Sonnet) has no pace
    _stage("secondary", "week_stage")
    return hb


def _heartbeat_line(hb: dict) -> bytes:
    return (json.dumps(hb) + "\n").encode("utf-8")


async def _find_device(name_prefix: str, timeout: float = 8.0):
    from bleak import BleakScanner
    print("scanning %.0fs for a '%s*' buddy..." % (timeout, name_prefix))
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if d.name and d.name.startswith(name_prefix):
            return d
    return None


async def _run(address, name_prefix, interval, once):
    from bleak import BleakClient

    target = address
    if target is None:
        dev = await _find_device(name_prefix)
        if dev is None:
            raise SystemExit(
                "No '%s*' buddy found. Is the Claude Buddy app running on "
                "the device, and not already connected to Claude.app?"
                % name_prefix
            )
        print("found %s (%s)" % (dev.name, dev.address))
        target = dev

    async with BleakClient(target) as client:
        print("connected; pushing quota every %ds (Ctrl-C to stop)" % interval)
        while True:
            try:
                hb = fetch_quota()
                await client.write_gatt_char(NUS_RX_UUID, _heartbeat_line(hb), response=False)
                print("pushed %s" % hb)
            except Exception as e:  # keep the link up across transient errors
                print("push error: %s" % e)
            if once:
                return
            if not client.is_connected:
                print("device disconnected")
                return
            await asyncio.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="seconds between quota pushes (default %d)" % DEFAULT_INTERVAL)
    ap.add_argument("--name-prefix", default=DEFAULT_NAME_PREFIX,
                    help="advertising-name prefix to match (default %s)" % DEFAULT_NAME_PREFIX)
    ap.add_argument("--address", default=None,
                    help="connect to this BLE address/UUID, skipping the scan")
    ap.add_argument("--once", action="store_true",
                    help="push a single sample then exit (smoke test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print quota only; no BLE")
    args = ap.parse_args(argv)

    if args.dry_run:
        hb = fetch_quota()

        def line(util_key, stage_key, label):
            u = hb.get(util_key)
            rem = "--" if u is None else "%d%%" % (100 - u)
            stage = hb.get(stage_key, "n/a")
            return "%s: %s remaining (stage=%s)" % (label, rem, stage)

        print(line("five_h_util", "five_h_stage", "5h"))
        print(line("week_util", "week_stage", "Week"))
        print(line("sonnet_util", "_none", "Sonnet"))  # tertiary has no pace
        return 0

    try:
        asyncio.run(_run(args.address, args.name_prefix, args.interval, args.once))
    except KeyboardInterrupt:
        print("\nstopped")
    except ImportError:
        raise SystemExit("bleak is required for BLE: pip install bleak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
