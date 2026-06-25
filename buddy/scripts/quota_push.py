"""Push real Claude usage quota to the Cardputer buddy over BLE.

The buddy's "5h / Week / 3rd" bars need real quota figures, but the
device is BLE-only and Claude.app's Hardware Buddy heartbeat carries no
quota (see buddy/references/protocol.md). This companion fills that gap:
it reads the quota from **codexbar** and writes, per heartbeat, a bar
length and a bar colour for each window to the device's Nordic-UART RX
characteristic, so the bars track the real account quota.

Backend: `codexbar --provider anthropic --format json`. Its first array
entry has `usage.{primary, secondary}.usedPercent` (the bar length) and
`pace.{primary, secondary}.stage` (which we turn into a colour here,
host-side):

    primary   -> five_h_util + five_h_color  (5-hour window)
    secondary -> week_util   + week_color    (7-day, all)

The **3rd bar is a generic (name, value) slot** — not a fixed window.
By default it tracks a codexbar *extra-rate window* (`usage.extraRateWindows`,
e.g. "Daily Routines", which replaced the old Sonnet window in the GUI),
sending its title as the bar's name and its usedPercent as the value:

    extraRateWindows[*] -> bar3_label + bar3_util + bar3_color

`--bar3-id` picks which extra window by id, `--bar3-label` overrides the
displayed name, and `--bar3-value N` forces a static value for the bar,
independent of codexbar's *data* (codexbar is still queried for the 5h/Week
bars, so it must be installed either way). Older codexbar builds with a
Sonnet `usage.tertiary` window are used as a last-resort fallback.

The full codexbar response shape (usage + pace, stage enum, guards) is
documented in buddy/references/codexbar-pace.md — pace only exists in
steipete/CodexBar#1722, so that file is our record of what we parse here.

(Mapping verified against the labeled usage API: primary==five_hour,
secondary==seven_day.) `<name>_util` gives the device its bar length
(*remaining* = 100 - util). `<name>_color` is an RGB int the device
paints directly — resolved here from the codexbar pace stage via
`_STAGE_COLORS` (green=reserve … red=deficit), with a remaining-%
fallback where there's no stage (the 3rd bar, or a window too early for
codexbar to emit pace). Keeping the colour map in this script means you
can retune colours without re-flashing the device.

For the 5h / Week windows we additionally send the even-burn baseline so
the device can draw CodexBar's expected-pace marker — a small tick on the
bar at the `expectedUsedPercent` position:

    five_h_expected + five_h_expected_color   (pace.primary)
    week_expected   + week_expected_color     (pace.secondary)

`<name>_expected` is `pace.<window>.expectedUsedPercent` (the % you'd be
at on an even burn). `<name>_expected_color` is the tick colour, resolved
here from the stage sign: green when behind the baseline (in reserve),
red when ahead (in deficit), yellow when on pace. Both keys are omitted
when codexbar emits no pace for that window (see the Guards in
codexbar-pace.md). The generic 3rd bar carries no pace -> no tick.

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
    python quota_push.py --bar3-id claude-routines   # pick the 3rd-bar extra window
    python quota_push.py --bar3-label Routines       # rename the 3rd bar
    python quota_push.py --bar3-label Focus --bar3-value 42  # static arbitrary bar

Run it from **Terminal.app**, not from inside another app's shell —
macOS Bluetooth permission is per-binary, so an indirectly-launched
Python aborts with SIGABRT (exit 134). `--dry-run` does no BLE and is
exempt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys

NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
# Which codexbar binary to shell out to. `pace` (the bar-colour + expected
# tick source) only exists in builds carrying steipete/CodexBar#1722, which
# isn't in the released CLI yet — set CODEXBAR_BIN to a from-source build of
# that PR (e.g. .../CodexBar/.build/release/CodexBarCLI) to get it. Defaults
# to plain `codexbar` on PATH (no pace -> bars colour by remaining-% only,
# no expected tick).
CODEXBAR_BIN = os.environ.get("CODEXBAR_BIN", "codexbar")
CODEXBAR_CMD = [CODEXBAR_BIN, "--provider", "anthropic", "--format", "json"]
DEFAULT_NAME_PREFIX = "Claude_"
DEFAULT_INTERVAL = 60

# Bar colour (RGB int 0xRRGGBB) per codexbar pace stage. The stage encodes
# consumption pace vs an even burn: *Behind = under pace (reserve, safe),
# onTrack = on pace, *Ahead = over pace (deficit, runs out early). We ramp
# green->red. This map lives host-side on purpose: tweak it and restart the
# script — no device re-flash needed (the device just paints what we send).
_STAGE_COLORS = {
    "farBehind":      0x00FF00,  # green  — deep reserve
    "behind":         0x55FF00,
    "slightlyBehind": 0xAAFF00,
    "onTrack":        0xFFFF00,  # yellow — exactly on pace
    "slightlyAhead":  0xFFAA00,
    "ahead":          0xFF5500,
    "farAhead":       0xFF0000,  # red    — heavy deficit
}


def _color_for(util, stage):
    """Resolve a bar colour (RGB int) or None. Prefer the pace stage; else
    a remaining-% scale (the 3rd bar has no pace, and codexbar omits pace
    early in a window). None only when there's no utilization at all."""
    if stage in _STAGE_COLORS:
        return _STAGE_COLORS[stage]
    if util is None:
        return None
    remaining = 100 - util
    return 0x00FF00 if remaining > 50 else (0xFFFF00 if remaining > 20 else 0xFF0000)


# Stage buckets for the expected-pace tick. The CLI never colours the tick
# itself — we derive a binary reserve/deficit (+ on-pace) signal from the
# stage sign here so the device draws green/red/yellow without knowing the
# enum. Mirrors the codexbar-pace.md table: *Behind = under the baseline
# (reserve), *Ahead = over it (deficit), onTrack = on pace.
_RESERVE_STAGES = ("farBehind", "behind", "slightlyBehind")
_DEFICIT_STAGES = ("slightlyAhead", "ahead", "farAhead")


def _line_color_for(stage):
    """Colour (RGB int) of the expected-pace tick, or None if the stage
    can't be classified (no pace -> no tick). Green = in reserve, red = in
    deficit, yellow = on pace."""
    if stage in _RESERVE_STAGES:
        return 0x00FF00  # green — under the baseline, in reserve
    if stage in _DEFICIT_STAGES:
        return 0xFF0000  # red   — over the baseline, in deficit
    if stage == "onTrack":
        return 0xFFFF00  # yellow — on pace
    return None


# Remembers --bar3-id values we've already warned about, so a persistent
# misconfiguration warns once per process instead of every push cycle.
_warned_bar3_ids = set()


def _resolve_bar3(usage, bar3_id=None, bar3_label=None, bar3_value=None):
    """Resolve the configurable 3rd bar to (label, used%|None) or None.

    The 3rd bar is generic — any (name, value). Resolution order:

      1. `bar3_value` set  -> a static value for the bar, independent of
         codexbar's *data* (codexbar is still queried for the 5h/Week bars).
         Pair it with --bar3-label for a name.
      2. a codexbar *extra-rate window* (`usage.extraRateWindows`) -> the one
         whose id == `bar3_id`, else the first. This is the default and is
         where "Daily Routines" lives (it replaced the Sonnet window in the
         GUI). Label = its title; value = its window.usedPercent.
      3. legacy `usage.tertiary` (Sonnet) -> last-resort fallback for older
         codexbar builds that still expose it.

    `bar3_label`, when given, always overrides the displayed name. Returns
    None when no source yields a value (the device then shows the 3rd bar
    as "--").
    """
    if bar3_value is not None:
        return (bar3_label or "Bar 3", max(0, min(100, int(bar3_value))))

    extra = usage.get("extraRateWindows") or []
    chosen = None
    if bar3_id is not None:
        # An explicit id either matches a window or it doesn't — we never
        # silently substitute extra[0] for a typo'd id. A miss warns once
        # (it'd otherwise repeat every push cycle) and falls through below.
        chosen = next((w for w in extra if w.get("id") == bar3_id), None)
        if chosen is None and bar3_id not in _warned_bar3_ids:
            _warned_bar3_ids.add(bar3_id)
            ids = ", ".join(w.get("id", "?") for w in extra) or "(none available)"
            print("quota_push: --bar3-id %r not in codexbar extra windows [%s]; "
                  "falling back to tertiary/none" % (bar3_id, ids),
                  file=sys.stderr)
    elif extra:
        chosen = extra[0]
    if chosen is not None:
        win = chosen.get("window") or {}
        up = win.get("usedPercent")
        label = bar3_label or chosen.get("title") or chosen.get("id") or "Bar 3"
        # An explicitly/auto-chosen window with no usedPercent returns
        # (label, None) rather than falling through to tertiary: keep the
        # chosen bar's identity and let the device show "--", instead of
        # silently relabelling the bar "Sonnet".
        return (label, None if up is None else int(round(up)))

    tert = (usage.get("tertiary") or {}).get("usedPercent")
    if tert is not None:
        return (bar3_label or "Sonnet", int(round(tert)))

    return None


def _read_codexbar(bar3_id=None, bar3_label=None, bar3_value=None):
    """Run codexbar and return a dict of the three bars.

    `five_h` / `week` -> (used%|None, stage|None, expected%|None) for the
    primary / secondary windows; `expected` is pace.<window>.expectedUsedPercent,
    the even-burn baseline the device marks with a tick (None when codexbar
    emits no pace). `bar3` -> (label, used%|None) or None for the generic 3rd
    bar (see _resolve_bar3 for how its source is chosen)."""
    try:
        out = subprocess.run(
            CODEXBAR_CMD, capture_output=True, text=True, check=True, timeout=20,
        ).stdout
    except FileNotFoundError:
        raise SystemExit(
            "codexbar binary not found: %r. Install it (e.g. `brew install codexbar`) "
            "or set CODEXBAR_BIN to a build, and make sure "
            "`%s --provider anthropic --format json` works." % (CODEXBAR_BIN, CODEXBAR_BIN)
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

    def used(section):
        u = (usage.get(section) or {}).get("usedPercent")
        return None if u is None else int(round(u))

    def stage(section):
        return (pace.get(section) or {}).get("stage")

    def expected(section):
        e = (pace.get(section) or {}).get("expectedUsedPercent")
        return None if e is None else int(round(e))

    return {
        "five_h": (used("primary"), stage("primary"), expected("primary")),
        "week": (used("secondary"), stage("secondary"), expected("secondary")),
        "bar3": _resolve_bar3(usage, bar3_id, bar3_label, bar3_value),
    }


def fetch_quota(bar3_id=None, bar3_label=None, bar3_value=None):
    """Build the heartbeat dict: per window, `<name>_util` (bar length =
    100-util) and `<name>_color` (RGB int the device paints directly).
    Colour resolution happens here, host-side, so it's tunable without a
    device re-flash. Windows with no utilization are omitted.

    For 5h / Week, also `<name>_expected` (the even-burn baseline %) and
    `<name>_expected_color` (the tick colour) when codexbar emits pace.

    The generic 3rd bar adds `bar3_label` (its arbitrary name) alongside
    `bar3_util` / `bar3_color`; it carries no pace, so no expected tick.

    e.g. {"five_h_util": 14, "five_h_color": 65280, "five_h_expected": 27,
          "five_h_expected_color": 65280, "week_util": 13,
          "week_color": 16755200, "bar3_label": "Daily Routines",
          "bar3_util": 0, "bar3_color": 65280}
    """
    raw = _read_codexbar(bar3_id, bar3_label, bar3_value)
    hb = {}
    for name in ("five_h", "week"):
        util, stage, expected = raw[name]
        if util is None:
            continue
        hb[name + "_util"] = util
        color = _color_for(util, stage)
        if color is not None:
            hb[name + "_color"] = color
        # Expected-pace tick: only when codexbar gave us a baseline and a
        # classifiable stage (5h / Week only).
        if expected is not None:
            line_color = _line_color_for(stage)
            if line_color is not None:
                hb[name + "_expected"] = expected
                hb[name + "_expected_color"] = line_color
    # The generic 3rd bar: a host-named (label, value) slot with no pace, so
    # its colour uses the remaining-% fallback (_color_for with stage=None).
    bar3 = raw.get("bar3")
    if bar3 is not None:
        label, util = bar3
        if util is not None:
            hb["bar3_label"] = label
            hb["bar3_util"] = util
            color = _color_for(util, None)
            if color is not None:
                hb["bar3_color"] = color
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


async def _run(address, name_prefix, interval, once,
               bar3_id=None, bar3_label=None, bar3_value=None):
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
                hb = fetch_quota(bar3_id, bar3_label, bar3_value)
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
    ap.add_argument("--bar3-id", default=None,
                    help="codexbar extraRateWindows id for the 3rd bar "
                         "(default: first extra window, e.g. Daily Routines)")
    ap.add_argument("--bar3-label", default=None,
                    help="override the 3rd bar's displayed name (arbitrary)")
    ap.add_argument("--bar3-value", type=int, default=None,
                    help="force a static 0..100 value for the 3rd bar, "
                         "independent of codexbar's data (codexbar is still "
                         "queried for 5h/Week; pair with --bar3-label)")
    args = ap.parse_args(argv)

    if args.dry_run:
        raw = _read_codexbar(args.bar3_id, args.bar3_label, args.bar3_value)
        for name, label in (("five_h", "5h"), ("week", "Week")):
            util, stage, expected = raw[name]
            rem = "--" if util is None else "%d%%" % (100 - util)
            color = _color_for(util, stage)
            color_s = "n/a" if color is None else "0x%06X" % color
            if expected is None:
                exp_s = "--"
            else:
                lc = _line_color_for(stage)
                exp_s = "%d%% (%s)" % (
                    expected, "n/a" if lc is None else "0x%06X" % lc)
            print("%-14s: %s remaining  stage=%-13s color=%s  expected=%s" % (
                label, rem, stage or "n/a", color_s, exp_s))
        # Mirror fetch_quota exactly: a 3rd bar with no value is *not* pushed,
        # so report it as "not pushed" rather than printing a phantom row the
        # device would never receive.
        bar3 = raw.get("bar3")
        b_util = None if bar3 is None else bar3[1]
        if b_util is None:
            why = "no source" if bar3 is None else "%r has no value" % bar3[0]
            print("%-14s: -- (not pushed; %s)" % ("3rd bar", why))
        else:
            color = _color_for(b_util, None)
            color_s = "n/a" if color is None else "0x%06X" % color
            print("%-14s: %d%% remaining  stage=%-13s color=%s  expected=%s" % (
                bar3[0], 100 - b_util, "n/a", color_s, "--"))
        return 0

    try:
        asyncio.run(_run(args.address, args.name_prefix, args.interval, args.once,
                         args.bar3_id, args.bar3_label, args.bar3_value))
    except KeyboardInterrupt:
        print("\nstopped")
    except ImportError:
        raise SystemExit("bleak is required for BLE: pip install bleak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
