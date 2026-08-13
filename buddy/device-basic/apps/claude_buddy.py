"""Claude Buddy for the M5Stack Basic.

This is a port of the Cardputer's `claude_buddy.py` to a device with
three face buttons instead of a QWERTY matrix keyboard, the 320x240
UI from `buddy_ui_basic`, and a real IP5306 battery gauge read through
`_battery_ip5306._read_battery`. The wire protocol, BLE stack,
persistent state, and character-receive logic are unchanged — we reuse
`buddy_ble`, `buddy_protocol`, `buddy_state`, and `buddy_chars`
byte-for-byte from the Cardputer build. Only the I/O layer (input →
UI) is Basic-specific.

### Install layout

UIFlow 2.0's launcher shows any `*.py` inside `/flash/apps/` in its
"App List" menu. The peer modules go alongside this file in the same
directory, and we prepend `/flash/apps/` to sys.path on entry so
`import buddy_ble` etc. resolves. This keeps the whole bundle
self-contained in one folder — no touching /flash/ root, no clobbering
UIFlow's own main.py/boot.py.

### Input mapping

The Basic has three face buttons, so we map them directly:

  BtnA   → approve once / confirm unpair
  BtnB   → deny / cancel unpair
  BtnC   → quit back to the UIFlow App List

When an unpair confirmation is pending, BtnA/BtnB answer it directly
("yes, wipe me" / "no, keep me"). Otherwise they answer the currently-
displayed permission prompt; when no prompt is pending they flash a
toast so the operator can tell the press registered. The mapping is
shown in the UI's hint strip.

### Return-to-menu

UIFlow 2.0 has no return-to-launcher API; when a user app's `run()`
ends, the launcher does not repaint and the screen stays frozen on
whatever the app drew last. The established workaround (see
`hello_cardputer.py`) is to soft-reboot via `machine.reset()` on exit,
which lands the user back at the launcher automatically. We do that
here, in the `finally` block, *after* tearing BLE down cleanly.
"""

import sys

# Make our peer modules importable *before* the first `import buddy_ble`
# below, otherwise we ImportError at load time and the launcher has no
# graceful way to show it.
#
# UIFlow 2.0's default sys.path on this build is roughly:
#   ['', '.frozen', '/lib', '/system', '/flash/libs']
# Notably /flash itself is NOT on the path, even though that's where
# boot.py and main.py live. We put the buddy_* peer modules at /flash/
# root (to keep them out of the App List, which scans /flash/apps/),
# and claude_buddy.py lives in /flash/apps/. Prepend both so imports
# resolve regardless of which layout a future install lands on.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import time

import M5
import machine

import buddy_ble
import buddy_chars
import buddy_protocol
import buddy_state
import buddy_ui_basic as buddy_ui
from _battery_ip5306 import _read_battery


def run():
    # Per-step prints so a hard fault during init (NimBLE Guru
    # Meditation, LCD driver crash, etc.) leaves a breadcrumb on the
    # serial console pointing at which step faulted. C-level crashes
    # bypass the launcher's try/except and reboot the chip, so the
    # last print before reboot is the only diagnostic we get.
    print("claude_buddy: run() start")

    # Power WiFi down before bringing up BLE. ESP32 shares a single
    # 2.4 GHz radio between WiFi and BLE, with software coexistence
    # arbitrating between them. The launcher (main.py) connects to
    # the event WiFi at boot, which leaves the radio actively
    # servicing beacons/keepalives by the time we get here.
    # `bluetooth.BLE().active(True)` in `_ensure_stack` cold-starts
    # the NimBLE controller, and in busy RF environments — many
    # nearby BLE peers, lots of WiFi traffic — that init
    # intermittently faults at the C layer and reboots the chip with
    # no Python-catchable error. We saw this consistently with the
    # crash log ending at `pre_active= False`. Buddy is BLE-only, so
    # taking WiFi down for the duration of the app is harmless; the
    # launcher reconnects on the next reboot via main.py.
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if sta.active():
            try:
                sta.disconnect()
            except OSError:
                pass
            sta.active(False)
        print("claude_buddy: wifi off")
    except Exception as e:
        # Defensive — if `network` isn't importable on this build, or
        # the WLAN object behaves unexpectedly, we'd rather continue
        # and risk the original coexistence crash than fail the app
        # outright. The print is enough to investigate later.
        print("claude_buddy: wifi disable warning:", e)
    # Drain the radio scheduler so WiFi tx queues finish before BLE
    # init takes over the controller. 1000 ms (was 200 ms) is the
    # value that finally got us past intermittent NimBLE
    # active(True) C-faults on a busy show floor — ESP32's WiFi
    # tear-down is more leisurely than its connect path, and the
    # 200 ms we tried first wasn't enough to fully release the
    # radio before BLE asks for it.
    time.sleep_ms(1000)

    ui = buddy_ui.BuddyUI()
    print("claude_buddy: ui ready")
    state = buddy_state.BuddyState()
    print("claude_buddy: state ready")
    ui.update_identity(state.name, state.owner)

    buddy_chars.sweep_partials()
    chars = buddy_chars.CharReceiver()
    print("claude_buddy: chars ready")

    # Protocol needs a handle on the BLE object (for disconnect /
    # forget_bonds), and BLE needs the on_line callback which needs the
    # protocol. Same indirection trick as the Basic: stash the protocol
    # in a 1-slot dict that the callback reads at event time.
    proto_holder = {"p": None}

    def on_line(raw):
        p = proto_holder["p"]
        if p is not None:
            p.on_line(raw)

    # BLE callbacks dispatch from micropython.schedule context, which
    # runs between bytecodes on the main thread. That means a
    # callback can land *inside* a Python-level UI routine that's
    # mid-way through a sequence of SPI ops to the LCD, interleaving
    # writes and leaving the panel in an inconsistent state. We avoid
    # that by having callbacks only mutate plain Python state and
    # letting the main loop drain it into UI calls. send_hello stays
    # in the callback because it's BLE-only — no LCD bus contention.
    pending_state = [None]
    pending_passkey = [None]

    def on_passkey(pk):
        pending_passkey[0] = pk

    def on_state_change(s):
        # The stripped UIFlow 2.0 BLE build doesn't fire
        # _IRQ_ENCRYPTION_UPDATE, so "connected" is terminal. Remap
        # it to "encrypted" so the UI advances past the PAIR... badge
        # and the protocol starts emitting its hello. Queries
        # buddy_ble.pairing_supported() (module-level state) rather
        # than an instance attribute on `ble`, so this is correct even
        # if a central connects mid-BuddyBLE.__init__, before the
        # `ble = BuddyBLE(...)` assignment below has completed.
        effective = s
        if s == "connected" and not buddy_ble.pairing_supported():
            effective = "encrypted"
        print("claude_buddy: state", s, "->", effective)
        pending_state[0] = effective
        if effective == "encrypted":
            p = proto_holder["p"]
            if p is not None:
                p.send_hello()

    # Run a full GC pass before NimBLE init. The controller
    # allocates several large chunks during active(True) — bonding
    # store, advertising buffers, host/controller queues — and a
    # fragmented MicroPython heap at this point has been observed
    # to push allocation onto a path that C-faults instead of
    # raising MemoryError. Cheap insurance to call gc.collect() here
    # since we have no other allocation pressure between launcher
    # exit and BLE init.
    import gc
    gc.collect()
    print("claude_buddy: gc done, free=", gc.mem_free())
    print("claude_buddy: constructing BuddyBLE")
    ble = buddy_ble.BuddyBLE(
        on_line=on_line,
        on_passkey=on_passkey,
        on_state=on_state_change,
    )
    print("claude_buddy: BuddyBLE returned")

    proto = buddy_protocol.BuddyProtocol(
        state=state,
        ui=ui,
        chars=chars,
        ble=ble,
        battery_reader=_read_battery,
    )
    proto_holder["p"] = proto

    ui.update_footer(state.stats(), _read_battery())
    print("Claude Buddy up as", ble.advertised_name)

    # Buttons: debounce 400 ms before polling so the press used to pick
    # this app from App List doesn't count as an intent. Same pattern
    # hello_cardputer.py uses — M5.update() must run each loop for
    # wasPressed() to observe fresh button state.
    time.sleep_ms(400)

    last_footer_ms = time.ticks_ms()
    last_toast_ms = 0
    footer_interval = 3000
    toast_dwell_ms = 1500

    try:
        while True:
            # Drain BLE-callback-deferred UI work in main-loop context
            # so LCD writes don't interleave with the periodic footer
            # paint or the prompt rendering kicked off by protocol
            # events. set_connection in particular repaints the whole
            # header strip, which is several SPI transactions long.
            new_state = pending_state[0]
            if new_state is not None:
                pending_state[0] = None
                ui.set_connection(new_state)
                if new_state == "encrypted":
                    ui.clear_passkey()
            new_pk = pending_passkey[0]
            if new_pk is not None:
                pending_passkey[0] = None
                ui.show_passkey(new_pk)
            proto.drain_ui()

            M5.update()

            # An active unpair confirmation outranks any permission
            # prompt: pressing A here means "yes, wipe me", not "yes,
            # approve the pending tool call". The unpair_pending()
            # check is also where the protocol layer rolls over the
            # 30s timeout, so we want it called every loop iteration
            # regardless of what button (if any) was pressed.
            unpair_active = proto.unpair_pending()

            if M5.BtnA.wasPressed():
                if unpair_active:
                    proto.confirm_unpair()
                elif not proto.send_permission("once"):
                    ui.flash_toast("A: no prompt", buddy_ui.GRAY_DIM)
                    ui.update_footer(state.stats(), _read_battery())
                last_toast_ms = time.ticks_ms()
            if M5.BtnB.wasPressed():
                if unpair_active:
                    proto.cancel_unpair()
                elif not proto.send_permission("deny"):
                    ui.flash_toast("B: no prompt", buddy_ui.GRAY_DIM)
                last_toast_ms = time.ticks_ms()
            if M5.BtnC.wasPressed():
                # Break out so the `finally` block tears BLE down
                # cleanly before we reboot back to the launcher. If
                # an unpair is pending, leaving without confirming
                # cancels it on the device side; the host already has
                # an "ok:false,pending:true" ack and a subsequent
                # disconnect will tell it the request didn't go
                # through.
                return

            now = time.ticks_ms()
            if time.ticks_diff(now, last_footer_ms) >= footer_interval:
                state.tick_nap()
                ui.update_footer(state.stats(), _read_battery())
                last_footer_ms = now
            if last_toast_ms and time.ticks_diff(now, last_toast_ms) >= toast_dwell_ms:
                ui.restore_button_hints()
                last_toast_ms = 0

            # Sweep the quota-bar glint. Self-throttled and a no-op outside the
            # connected steady state, so it's safe to call unconditionally here
            # in main-loop context (not a BLE callback — no SPI interleave risk).
            ui.tick_anim()

            # 40 ms matches buddy_app.py — fast enough for responsive
            # button handling, slow enough that the BLE IRQ gets plenty
            # of room.
            time.sleep_ms(40)
    finally:
        # Mirror buddy_app.py's teardown ordering: BLE first so a late
        # async disconnect event can't repaint Buddy chrome on top of
        # the launcher (cf. the comment in BuddyBLE.deinit), then wipe
        # the screen to black, then hand control back to UIFlow.
        try:
            ble.deinit()
        except Exception as e:
            print("claude_buddy: deinit warning:", e)
        try:
            M5.Lcd.fillScreen(buddy_ui.BLACK)
        except Exception as e:
            print("claude_buddy: screen-clear warning:", e)
        # UIFlow has no launcher-return API; machine.reset() is the
        # only way back to App List. Same pattern hello_cardputer.py
        # uses. Brief pause so any trailing BLE log doesn't get
        # truncated mid-line on the USB console.
        time.sleep_ms(200)
        machine.reset()


# UIFlow 2.4.x's App List has been observed to invoke apps both as
# __main__ (file run directly) and via import (when picked through
# the menu vs. the file system, depending on version). The previous
# if/else with both arms calling run() was just dispatching to
# itself; collapse it so the empirical behavior is the documented
# behavior. The trade-off is that anyone who imports this module
# from CPython for inspection will trigger a BLE init — but the
# imports above (M5, bluetooth) already only resolve on-device, so
# that path isn't a real use case.
run()
