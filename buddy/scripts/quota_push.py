"""Push the real Claude usage quota to the Cardputer buddy over BLE.

The buddy's "5h remaining" / "Week remaining" bars need real quota
figures, but the device is BLE-only and **Claude.app's Hardware Buddy
heartbeat carries no quota** (confirmed by live capture — see
buddy/references/protocol.md). This companion fills that gap: it polls
the Claude usage API and writes heartbeats containing `five_h_util` /
`week_util` to the device's Nordic-UART RX characteristic, so the bars
track the real account quota.

    five_h_util = five_hour.utilization   (0..100, "used")
    week_util   = seven_day.utilization   (0..100, "used")

The device renders *remaining* = 100 - utilization.

### Connection model (important)

This script is the BLE **central**, exactly like Claude.app. A buddy
accepts one central at a time, so **while this is connected, Claude.app
cannot be** — you get the live quota readout but not prompt-approval.
Run it when you want quota on the desk display; quit it (Ctrl-C) and
reconnect from Claude.app for the approval workflow. (Simultaneous use
would need multi-connection support in the device firmware — a future
enhancement.)

### Requirements

    pip install bleak            # BLE central for macOS/Linux/Windows

macOS Keychain must hold the "Claude Code-credentials" entry (it does
once you've logged into Claude Code) — same source the `quota-check`
skill uses. The OAuth token is read fresh on every poll, so token
refreshes done by Claude Code are picked up automatically.

### Usage

    python quota_push.py                  # scan for a Claude_* buddy, push forever
    python quota_push.py --interval 30    # poll/push every 30 s (default 60)
    python quota_push.py --address <UUID> # skip the scan, connect directly
    python quota_push.py --once           # connect, push one sample, exit
    python quota_push.py --dry-run        # fetch+print quota only, no BLE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import urllib.request

NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_NAME_PREFIX = "Claude_"
DEFAULT_INTERVAL = 60


def _oauth_token() -> str:
    """Read the Claude Code OAuth access token from the macOS Keychain.

    Mirrors quota-check/get_quota.sh: the keychain item is a JSON blob
    whose claudeAiOauth.accessToken we want. Read every call so a token
    refreshed by Claude Code is used without restarting this script.
    """
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit(
            "Could not read '%s' from the Keychain. Log into Claude Code "
            "first (this is macOS-only; on Linux/Windows adapt _oauth_token)."
            % KEYCHAIN_SERVICE
        )
    try:
        return json.loads(raw).get("claudeAiOauth", {}).get("accessToken", "")
    except json.JSONDecodeError:
        raise SystemExit("Keychain entry is not the expected JSON shape.")


def fetch_utilization():
    """Return (five_h_util, week_util) as ints 0..100, either may be None."""
    token = _oauth_token()
    if not token:
        raise SystemExit("No access token in the Keychain entry.")
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.0.32",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError("usage API error: %s" % data.get("error"))

    def _util(section):
        block = data.get(section) or {}
        u = block.get("utilization")
        return None if u is None else int(round(u))

    return _util("five_hour"), _util("seven_day")


def _heartbeat_line(five, week) -> bytes:
    hb = {}
    if five is not None:
        hb["five_h_util"] = five
    if week is not None:
        hb["week_util"] = week
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
                five, week = fetch_utilization()
                await client.write_gatt_char(
                    NUS_RX_UUID, _heartbeat_line(five, week), response=False
                )
                print("pushed five_h_util=%s week_util=%s" % (five, week))
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
        five, week = fetch_utilization()
        print("five_h_util=%s  week_util=%s  ->  5h remaining=%s%%  week remaining=%s%%" % (
            five, week,
            "--" if five is None else 100 - five,
            "--" if week is None else 100 - week,
        ))
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
