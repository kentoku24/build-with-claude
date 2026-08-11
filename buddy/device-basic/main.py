"""Three-button launcher for the device-basic buddy bundle.

Menu keys: A = move up, B = move down, C = launch selected app.
Apps are the ``.py`` files in ``/flash/apps/``, discovered
dynamically at boot. Selection is driven by the three buttons
(A / B / C); the launched app runs at import time and exits via
``machine.reset()``, which reboots straight back to this launcher.

No WiFi at boot: ``wifi_event.py`` ships in this bundle (it is a
byte-for-byte requirement of the port), but this launcher never
calls it. The device is BLE-only and the ~3-9.5s WiFi splash would
delay the boot print past the capture window, so the connect logic
simply never runs here.

Chrome only: this launcher renders the menu in plain chrome on a
320x240 panel. ``burst_frames.py`` also ships in this bundle but is
consumed by the sibling ``ui_theme`` module (its burst animation),
not by this launcher.
"""

import os
import sys
import time

import M5


_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xFF0000

_LCD = M5.Lcd
_W = 320
_H = 240

_APPS_DIR = "/flash/apps"

_MAX_VISIBLE = 8
_MENU_Y0 = 28
_ROW_H = 24


# M5.begin() has usually already run in boot.py, but call it
# defensively in case we are re-entered via a soft reset that did
# not rerun boot.py. It is idempotent — a second call is a no-op.
try:
    M5.begin()
except Exception as e:
    print("launcher: M5.begin() warning:", e)


# Make peer modules at /flash/ importable so launched apps can
# `import buddy_ble` etc. without each one repeating the sys.path
# dance. Matches what claude_buddy.py already does defensively.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _discover_apps():
    """Return a sorted list of ``(display_name, module_basename)``.

    Module basename is the filename without extension (for import).
    Display name is the same but with underscores turned into spaces
    and title-cased — gives a slightly friendlier menu than raw
    filenames without forcing us to ship a separate metadata file.
    """
    try:
        files = sorted(
            f for f in os.listdir(_APPS_DIR) if f.endswith(".py")
        )
    except OSError as e:
        print("launcher: cannot list", _APPS_DIR, e)
        return []
    out = []
    for fname in files:
        mod = fname[:-3]
        # Skip private/helper modules — a .py dropped in for a helper
        # shouldn't land in the visible menu.
        if mod.startswith("_"):
            continue
        display = mod.replace("_", " ")
        out.append((display, mod))
    return out


def _draw_chrome(apps, cursor, scroll_top=0):
    """Full repaint of chrome + menu on the 320x240 panel."""
    _LCD.fillScreen(_BLACK)

    # Header.
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Claude Buddy Launcher", 6, 5)

    # Menu rows. Only _MAX_VISIBLE rows are shown at once; scroll_top
    # is the index of the first visible app.
    y = _MENU_Y0
    visible = apps[scroll_top:scroll_top + _MAX_VISIBLE]
    for i, (display, _mod) in enumerate(visible):
        abs_i = scroll_top + i
        if abs_i == cursor:
            _LCD.fillRect(4, y - 2, 312, 20, _ORANGE)
            _LCD.setTextColor(_BLACK, _ORANGE)
        else:
            _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString(display, 10, y)
        y += _ROW_H

    # Hint strip.
    _LCD.fillRect(0, 216, _W, 24, _DARK)
    _LCD.fillRect(0, 216, _W, 1, _ORANGE)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "A up  B down  C launch"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, 220)


def _launch(mod_name):
    """Import the module, which runs its entrypoint at import time.

    On clean exit the app calls ``machine.reset()`` which brings us
    back here. On exception, show a minimal crash screen, wait for
    any button, then return to the menu.
    """
    _LCD.fillScreen(_BLACK)
    try:
        __import__(mod_name)
    except Exception as e:
        _LCD.fillScreen(_BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString("App crashed:", 6, 10)
        _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString(mod_name, 6, 26)
        _LCD.drawString(str(e)[:44], 6, 44)
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString("any button to return", 6, _H - 14)
        print("launcher: {} failed: {}".format(mod_name, e))
        # Drop the half-imported module from sys.modules so a second
        # selection of the same app re-runs its body. Idempotent —
        # KeyError just means the failure happened before the partial
        # entry was installed.
        try:
            del sys.modules[mod_name]
        except KeyError:
            pass
        while True:
            M5.update()
            if (M5.BtnA.wasPressed() or M5.BtnB.wasPressed()
                    or M5.BtnC.wasPressed()):
                return
            time.sleep_ms(40)


def main():
    apps = _discover_apps()
    # Unconditional boot print on the success path, before any
    # potentially-blocking call — the device-verification capture
    # window depends on this token.
    print("launcher: discovered %d apps" % len(apps))
    if not apps:
        _LCD.fillScreen(_BLACK)
        _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString("No apps in " + _APPS_DIR, 6, 40)
        while True:
            time.sleep_ms(500)

    cursor = 0
    scroll_top = 0
    _draw_chrome(apps, cursor, scroll_top)

    while True:
        M5.update()
        if M5.BtnA.wasPressed():
            cursor = (cursor - 1) % len(apps)
            if cursor < scroll_top:
                scroll_top = cursor
            elif cursor >= scroll_top + _MAX_VISIBLE:
                scroll_top = max(0, len(apps) - _MAX_VISIBLE)
            _draw_chrome(apps, cursor, scroll_top)
        elif M5.BtnB.wasPressed():
            cursor = (cursor + 1) % len(apps)
            if cursor >= scroll_top + _MAX_VISIBLE:
                scroll_top = cursor - _MAX_VISIBLE + 1
            elif cursor < scroll_top:
                scroll_top = 0
            _draw_chrome(apps, cursor, scroll_top)
        elif M5.BtnC.wasPressed():
            _, mod_name = apps[cursor]
            _launch(mod_name)
            # If _launch returns (error path), redraw the menu.
            _draw_chrome(apps, cursor, scroll_top)
            time.sleep_ms(300)

        time.sleep_ms(40)


main()
