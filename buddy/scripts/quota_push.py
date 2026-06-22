"""Push real Claude usage quota to the Cardputer buddy over BLE.

The buddy's "5h / Week / Sonnet" bars need real quota figures, but the
device is BLE-only and Claude.app's Hardware Buddy heartbeat carries no
quota (see buddy/references/protocol.md). This companion fills that gap:
it reads the quota from **codexbar** and writes heartbeats containing
`five_h_util` / `week_util` / `sonnet_util` to the device's Nordic-UART
RX characteristic, so the bars track the real account quota.

Backend: `codexbar --provider anthropic --format json`. Its first array
entry has `usage.{primary, secondary, tertiary}.usedPercent`:

    primary   -> five_h_util   (5-hour window,  windowMinutes 300)
    secondary -> week_util     (7-day, all,     windowMinutes 10080)
    tertiary  -> sonnet_util   (7-day, Sonnet,  windowMinutes 10080)

(Mapping verified against the labeled usage API: primary==five_hour,
secondary==seven_day, tertiary==seven_day_sonnet.) The device renders
*remaining* = 100 - utilization for each.

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


def fetch_utilization():
    """Return (five_h, week, sonnet) utilization % as ints 0..100; any None.

    Reads codexbar's JSON and maps usage.primary/secondary/tertiary
    usedPercent to the 5h / weekly-all / weekly-Sonnet windows.
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
    usage = data[0].get("usage") or {}

    def _used(section):
        block = usage.get(section) or {}
        u = block.get("usedPercent")
        return None if u is None else int(round(u))

    return _used("primary"), _used("secondary"), _used("tertiary")


def _heartbeat_line(five, week, sonnet) -> bytes:
    hb = {}
    if five is not None:
        hb["five_h_util"] = five
    if week is not None:
        hb["week_util"] = week
    if sonnet is not None:
        hb["sonnet_util"] = sonnet
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
                five, week, sonnet = fetch_utilization()
                await client.write_gatt_char(
                    NUS_RX_UUID, _heartbeat_line(five, week, sonnet), response=False
                )
                print("pushed five_h_util=%s week_util=%s sonnet_util=%s" % (five, week, sonnet))
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
        five, week, sonnet = fetch_utilization()

        def rem(u):
            return "--" if u is None else 100 - u

        print("five_h_util=%s week_util=%s sonnet_util=%s  ->  "
              "5h remaining=%s%%  week remaining=%s%%  sonnet remaining=%s%%" % (
                  five, week, sonnet, rem(five), rem(week), rem(sonnet)))
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
