"""Render the buddy's state to the 240x135 Cardputer-Adv LCD.

### Rendering API choice

We use **`M5.Lcd.drawString(text, x, y)`** and **`M5.Lcd.textWidth(text)`**
everywhere instead of `setCursor()+print()` + char-count estimates.
Two reasons:

1. **Cursor origin on this build is baseline, not top-left.**
   `setCursor(x, y)` sets (x, baseline_y). DejaVu9 has ~8 px of ascender
   above the baseline, so text we thought was landing at y=118 was
   actually rendering around y=110..120 — half above the hint strip's
   DARK background. `drawString` uses the driver's text datum, which
   defaults to TL_DATUM (top-left), so (x, y) is the top-left corner
   of the glyph cell. That matches how the rest of the layout math
   thinks about rectangles.

2. **Proportional font widths are not `_CHAR_W * len(text)`.**
   Measured on hardware with DejaVu9 at size 1:

        "Y once"            = 38 px  (not 6*6=36)
        "Q = back to menu"  = 103 px
        "100%"              = 31 px  (not 4*6=24 — this is why the
                                      '%' was wrapping to a second
                                      line when we did x = 240-6-24)
        "Claude Buddy"      = 79 px
        "Settings > Buddy"  = 101 px

   So we call `_LCD.textWidth(...)` for every centering or right-
   alignment calculation. It's a cheap call (pure lookup over the
   font's advance table) and eliminates the off-by-a-few-pixels
   rendering glitches that show up when a layout guesses wrong.

### Font selection

`M5.Lcd.FONTS.DejaVu9` — the smallest DejaVu variant (10 px tall).
The default font is ~16 px and too bulky; DejaVu12 fits body text
but pushes the 3-column hint strip to within 7 px of the right edge
and fits the idle help awkwardly. DejaVu9 gives us ~17 px of strip
right margin and enough vertical room that we can bump the passkey
to size 4 for cross-room readability.

### State-specific layouts

  Idle (advertising / disconnected) — DejaVu9, all size 1:
    y=0..20    header "Claude Buddy" + status badge
    y=28       "Waiting to pair..."
    y=48       "Open Claude, go to"
    y=66       "Settings > Buddy"
    y=84       "and pick this one"
    y=112..134 hint strip "Q = Exit" (centered)

  Connected with heartbeat (no identity band — reused for a 3rd bar):
    y=0..20    header ("Claude Buddy" + status)
    y=24       "5h" quota bar     (100 - five_h_util; expected-pace tick)
    y=48       "Week" quota bar   (100 - week_util; expected-pace tick)
    y=72       "Sonnet" quota bar (100 - sonnet_util; hidden while a prompt is up)
    y=74..108  prompt box (when a permission is pending)
    y=112..134 hint strip (Y once / N deny / Q exit columns)

  Passkey overlay (during BLE pairing, layered over main):
    y=28       "Pairing passkey:"
    y=44..84   6-digit code at setTextSize(4)
    y=96       "type it into Claude"
"""

import time

import M5

# Anthropic palette, inlined — byte-for-byte matches ui_theme.py.
ORANGE = 0xCC785C
CREAM = 0xF0EEE6
DARK = 0x1F1F1F
BLACK = 0x000000
WHITE = 0xFFFFFF
GRAY_DIM = 0x333333
GRAY_MID = 0x777777
GREEN = 0x00FF00
CYAN = 0x00FFFF
YELLOW = 0xFFFF00
RED = 0xFF0000

# Battery voltage-trend colouring for the "{pct}%" footer segment. The
# Cardputer is a plain-ADC board, so isCharging()/getBatteryCurrent() are
# non-functional (M5Unified returns constants); voltage is the only live
# battery signal. We compare it to a reference sampled _BATT_TREND_MS ago:
# a rise of at least _BATT_TREND_MV ⇒ charging (ORANGE), an equal fall ⇒
# draining (GREEN), neither ⇒ steady (WHITE). The deadband sits above the
# ~±8 mV ADC jitter so a flat battery stays white instead of flickering;
# a ~20 s response is plenty for a battery gauge.
_BATT_TREND_MS = 20000
_BATT_TREND_MV = 10

_LCD = M5.Lcd

_W = 240
_H = 135

# ---- quota-bar shimmer
#
# Each bar's filled portion stays solid (the host-sent pace colour); a soft
# highlight band glides across it, rests, then repeats — a "glint" that signals
# the link is live without the SPI cost of a full barber-pole repaint. tick_anim
# is called every main-loop iteration but self-throttles to _GLINT_FRAME_MS, and
# during the rest gap it short-circuits to nothing.
#
# The shine is a smooth bell: the band is sliced into thin _GLINT_SLICE-px
# columns whose lighten fraction follows a precomputed profile (_GLINT_PROFILE)
# — brightest at the centre, fading quadratically to the edges. The whole band
# is clipped to the filled width so it never paints over the gray remainder or
# a "--"/empty bar. Speed is _GLINT_STEP px per frame: the LCD can't sustain a
# high frame rate, so we move *fewer pixels per frame* (small step) rather than
# leaning on fps for a slow, smooth glide.
#
# Geometry mirrors _draw_bar exactly: bars start at x=6 and are _BAR_W wide.
_BAR_W = _W - 12             # 228 — must match _draw_bar's bar_w
_GLINT_W = 40                # highlight band width (px) — wider = gentler ramp
_GLINT_SLICE = 2             # band drawn as slices this wide (smaller = finer)
_GLINT_STEP = 3              # band travel per frame (px) — smaller = slower glide
_GLINT_FRAME_MS = 50         # min ms between frames (~20 fps; LCD-bound ceiling)
_GLINT_REST_MS = 800         # pause between sweeps (ms)
_GLINT_REVERSE = True        # True = right-to-left, nodding to example #6
_GLINT_CORE = 0.6            # peak lighten fraction at the band's centre
# Derived cycle counts (sweep frames + rest frames).
_GLINT_TRAVEL = _BAR_W + _GLINT_W
_GLINT_SWEEP_STEPS = _GLINT_TRAVEL // _GLINT_STEP + 1
_GLINT_REST_FRAMES = _GLINT_REST_MS // _GLINT_FRAME_MS
_GLINT_CYCLE = _GLINT_SWEEP_STEPS + _GLINT_REST_FRAMES


def _build_glint_profile(width, slice_w, core):
    """Precompute the glint's brightness ramp once at import.

    Returns a list of (offset_px, slice_width_px, lighten_frac) covering the
    band left-to-right. Brightness is a quadratic bell (1 - d^2, where d is the
    distance from the centre normalised to the half-width), so the highlight is
    brightest in the middle and fades smoothly out. Near-zero edge slices are
    dropped — they'd lighten the base colour imperceptibly while still costing a
    fillRect. Pure arithmetic (no `math`) so it works on stripped MicroPython
    builds.
    """
    prof = []
    half = width / 2.0
    off = 0
    while off < width:
        w = slice_w if off + slice_w <= width else (width - off)
        d = abs((off + w / 2.0) - half) / half
        if d > 1.0:
            d = 1.0
        frac = core * (1.0 - d * d)
        if frac >= 0.05:
            prof.append((off, w, frac))
        off += slice_w
    return prof


_GLINT_PROFILE = _build_glint_profile(_GLINT_W, _GLINT_SLICE, _GLINT_CORE)

# Usage bars render the *real* Claude quota, which only the host knows.
# The device is BLE-only (claude_buddy.py takes WiFi down for radio
# coexistence) so it can't query usage itself — the host companion
# (scripts/quota_push.py, backed by `codexbar`) sends, per heartbeat:
#   five_h_util / week_util / sonnet_util   - utilization % (0..100, "used")
#                                             -> bar length = 100 - util
#   five_h_color / week_color / sonnet_color - RGB int -> bar fill colour
#   five_h_expected / week_expected          - even-burn baseline % (a *used*
#                                             %) -> expected-pace tick position
#   five_h_expected_color / week_expected_color - RGB int -> tick colour
# The host derives the colour from the codexbar pace stage (and a
# remaining-% fallback for windows with no pace); the device just paints
# it. Keeping the stage->colour map host-side means colours can be retuned
# without re-flashing. The expected tick (5h / Week only — Sonnet has no
# pace) marks CodexBar's expectedUsedPercent: green where you're under the
# baseline (in reserve), red where you're over it (in deficit).
#
# Claude.app's own heartbeat carries none of these, so on that link the
# bars read "--". See buddy/references/protocol.md.


def _has_cjk(s: str) -> bool:
    """True if s contains any character outside the DejaVu9 Latin range."""
    return any(ord(c) > 0x7E for c in s)


def _set_font_auto(text: str) -> int:
    """Set EFontJA24 for CJK text, DejaVu9 otherwise. Returns px height."""
    if _has_cjk(text):
        try:
            _LCD.setFont(_LCD.FONTS.EFontJA24)
            return 24
        except AttributeError:
            pass
    _LCD.setFont(_LCD.FONTS.DejaVu9)
    return 10


def _right(y: int, pad: int, text: str) -> int:
    """Cursor X so `text` ends `pad` px from the right edge."""
    return _W - pad - _LCD.textWidth(text)


def _center(text: str) -> int:
    """Cursor X to horizontally center `text` in the viewport."""
    return (_W - _LCD.textWidth(text)) // 2


def _lighten(color: int, f: float) -> int:
    """Blend a 0xRRGGBB int toward white by fraction f (0..1).

    The glint is a brighter shade of whatever pace colour the host sent for
    that bar, so we derive it at draw time rather than hard-coding a palette.
    M5GFX accepts 24-bit ints directly (it downsamples to RGB565 internally).
    """
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    r = int(r + (255 - r) * f)
    g = int(g + (255 - g) * f)
    b = int(b + (255 - b) * f)
    return (r << 16) | (g << 8) | b


class BuddyUI:
    """240x135 view. Mirrors the Basic's BuddyUI API so the protocol
    and app layers don't care which display is underneath."""

    def __init__(self):
        self._last = {}
        self._passkey = None
        # Unpair confirmation overlay: True while the host has asked
        # us to unpair and we're waiting for an on-device Y/N press.
        # See the threat model in buddy_ble.py — the BLE link is
        # unauthenticated, so destructive commands need an in-person
        # confirmation that an in-range BLE attacker can't fake.
        self._unpair_prompt = False
        self._connection_state = "advertising"
        self._prompt = None
        self._identity_name = "Buddy"
        self._identity_owner = ""
        # Cache last footer values so _draw_connected_main can repaint
        # the footer after _draw_main's fillRect wipes y=96..110.
        self._last_stats = {}
        self._last_battery = {}
        # Voltage-trend colour state for the battery "{pct}%" footer
        # segment (see _update_batt_trend).
        self._batt_mv_ref = None
        self._batt_mv_ref_ms = 0
        self._batt_pct_color = WHITE
        # Shimmer state. _anim_bars is rebuilt by _draw_data_rows with the
        # geometry of the currently-visible quota bars; tick_anim sweeps a
        # glint across them. _glint_phase walks the sweep+rest cycle;
        # _glint_was_active lets the rest gap short-circuit after one cleanup
        # frame so we don't keep repainting solid bars while nothing moves.
        self._anim_bars = []
        self._glint_phase = 0
        self._last_glint_ms = 0
        self._glint_was_active = False
        _LCD.fillScreen(BLACK)
        # setFont is sticky across setTextSize calls, so we pick
        # DejaVu9 once at init. Wrapped in try/except so a future
        # UIFlow build that drops the font still loads us (falls back
        # to the default at an uglier size).
        try:
            _LCD.setFont(_LCD.FONTS.DejaVu9)
        except Exception as e:
            print("buddy_ui_cp: setFont fallback:", e)
        # No setRotation — Cardputer-Adv boots in landscape already.
        self._redraw_chrome()

    # ---- public setters (shape matches Basic's BuddyUI)

    def set_connection(self, state: str):
        if state == self._connection_state:
            return
        self._connection_state = state
        self._draw_header()
        if state in ("advertising", "disconnected"):
            self._prompt = None
            self._last = {}
        self._draw_main()
        self.restore_button_hints()

    def show_passkey(self, pk: int):
        self._passkey = pk
        self._draw_passkey_overlay()
        self.restore_button_hints()

    def clear_passkey(self):
        if self._passkey is None:
            return
        self._passkey = None
        self._draw_main()

    def show_unpair_prompt(self):
        """Show the destructive-action confirmation overlay.

        Repaints the main panel and the hint strip so the only useful
        keys are Y (confirm) and N (cancel) — Q stays an exit even
        here, mirroring the passkey overlay's escape hatch.
        """
        self._unpair_prompt = True
        self._draw_unpair_overlay()
        self.restore_button_hints()

    def clear_unpair_prompt(self):
        if not self._unpair_prompt:
            return
        self._unpair_prompt = False
        self._draw_main()
        self.restore_button_hints()

    def update_heartbeat(self, hb: dict):
        prev_pending = bool(self._prompt)
        self._last = hb
        self._prompt = hb.get("prompt")
        curr_pending = bool(self._prompt)
        # Steady-state connected heartbeat: skip the full clear+redraw in
        # _draw_main and just update the bar rows in-place.  This eliminates
        # the black flash that _draw_main's fillRect causes on every tick.
        # Fall through to _draw_main only on transitions (prompt appear/
        # disappear) or non-connected states, where a full repaint is needed.
        if (
            self._connection_state not in ("advertising", "disconnected")
            and self._passkey is None
            and not self._unpair_prompt
            and curr_pending == prev_pending
        ):
            self._draw_data_rows()
        else:
            self._draw_main()
        if curr_pending != prev_pending:
            self.restore_button_hints()

    def update_identity(self, name: str, owner: str):
        # The connected layout no longer renders an identity band — that
        # row is reused for the Sonnet bar, and the header already shows
        # "Claude Buddy". Just remember the values.
        self._identity_name = name or "Buddy"
        self._identity_owner = owner or ""

    def update_footer(self, stats: dict, battery: dict):
        self._last_stats = stats
        self._last_battery = battery
        # Recompute the trend colour here, on a fresh reading — not in
        # _draw_footer, which also runs on cache-restore repaints.
        self._update_batt_trend(battery)
        # Stats footer only appears during the connected layout.
        if self._connection_state not in ("advertising", "disconnected"):
            self._draw_footer(stats, battery)

    def _update_batt_trend(self, battery: dict):
        """Set the {pct}% colour from the battery-voltage trend.

        ORANGE = rising (charging), GREEN = falling (draining), WHITE =
        steady. Compares the current voltage against a reference sampled
        at least _BATT_TREND_MS ago, with a _BATT_TREND_MV deadband; the
        colour is held between updates so it never flickers.
        """
        mv = battery.get("mV", 0)
        now = time.ticks_ms()
        if self._batt_mv_ref is None:
            self._batt_mv_ref = mv
            self._batt_mv_ref_ms = now
            return
        if time.ticks_diff(now, self._batt_mv_ref_ms) < _BATT_TREND_MS:
            return
        delta = mv - self._batt_mv_ref
        if delta >= _BATT_TREND_MV:
            self._batt_pct_color = ORANGE
        elif delta <= -_BATT_TREND_MV:
            self._batt_pct_color = GREEN
        else:
            self._batt_pct_color = WHITE
        self._batt_mv_ref = mv
        self._batt_mv_ref_ms = now

    def flash_decision(self, decision: str):
        color = GREEN if decision == "once" else RED
        self.flash_toast(decision.upper() + " sent", color)

    def flash_toast(self, text: str, color: int = CYAN):
        """Overwrite the hint strip with a one-line colored status."""
        _LCD.fillRect(0, 112, _W, _H - 112, color)
        _LCD.setTextColor(WHITE, color)
        _LCD.setTextSize(1)
        # Clip to whatever fits on the strip; in practice callers
        # keep text short.
        t = text
        while _LCD.textWidth(t) > _W - 12 and len(t) > 1:
            t = t[:-1]
        _LCD.drawString(t, 6, 117)

    def restore_button_hints(self):
        """Paint the hint strip. Shows the keyboard-command menu.

        Two modes:
          - Passkey on screen: Q only — Y/N are no-ops during pairing
            and showing them would be misleading.
          - Otherwise: full Y / N / Q menu, regardless of whether a
            prompt is currently pending. The earlier "only show what
            does something right now" version hid Y/N until a prompt
            arrived, which meant the operator couldn't learn the
            bindings just by looking at the device — the whole
            keyboard menu was invisible except during the ~1s windows
            of active prompts. When Y/N are pressed without a prompt,
            the main loop flashes a "no prompt" toast so the user
            still gets feedback; the menu staying visible is what
            makes the toast's meaning obvious.
        """
        # Thin orange hairline above the strip + DARK fill.
        _LCD.fillRect(0, 111, _W, 1, ORANGE)
        _LCD.fillRect(0, 112, _W, _H - 112, DARK)
        _LCD.setTextColor(CREAM, DARK)
        _LCD.setTextSize(1)
        if self._unpair_prompt:
            # Only Y and N during a destructive-action confirmation;
            # showing Q here invites a thumb-fumble exit that leaves
            # the host hanging on a pending ack.
            _LCD.drawString("Y confirm", 8, 117)
            n = "N cancel"
            _LCD.drawString(n, _right(117, 8, n), 117)
            return
        if self._passkey is not None:
            # During pairing only Q makes sense — Y and N don't
            # actually do anything until the encrypted state fires.
            label = "Q = Exit"
            _LCD.drawString(label, _center(label), 117)
            return
        # 3-column layout. Measured widths on DejaVu9: 38/39/34 px.
        # Left-aligned columns at x=8/96/right-aligned-8 give the
        # eye a clear "approve / deny / back" reading order.
        _LCD.drawString("Y once", 8, 117)
        _LCD.drawString("N deny", 96, 117)
        q = "Q exit"
        _LCD.drawString(q, _right(117, 8, q), 117)

    def is_idle(self) -> bool:
        return (
            self._connection_state in ("advertising", "disconnected")
            and self._passkey is None
            and self._prompt is None
            and not self._unpair_prompt
        )

    def tick_idle_burst(self, frame, last_tick):
        # No burst animation on Cardputer-Adv — kept for API shape
        # so buddy_app's main loop can call unconditionally.
        return frame, last_tick

    # ---- drawing primitives

    def _draw_header(self):
        _LCD.fillRect(0, 0, _W, 20, DARK)
        _LCD.fillRect(0, 20, _W, 1, ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, DARK)
        _LCD.drawString("Claude Buddy", 6, 5)
        icon, color = self._connection_icon()
        _LCD.setTextColor(color, DARK)
        _LCD.drawString(icon, _right(5, 6, icon), 5)

    def _connection_icon(self):
        s = self._connection_state
        if s == "encrypted":
            return ("LINKED", GREEN)
        if s == "connected":
            return ("PAIR..", YELLOW)
        if s == "disconnected":
            return ("OFF", RED)
        return ("ADV", CYAN)

    def _draw_main(self):
        # In connected state: clear only the content band (y=21..95),
        # leaving the footer band (y=96..110) untouched. This prevents
        # rapid heartbeats from flickering the footer black — the footer
        # is only written by explicit _draw_footer calls via update_footer.
        # In all other states: clear the full band including the footer,
        # since those layouts own the entire area.
        if (
            not self._unpair_prompt
            and self._passkey is None
            and self._connection_state not in ("advertising", "disconnected")
        ):
            _LCD.fillRect(0, 21, _W, 75, BLACK)  # y=21..95, footer spared
        else:
            _LCD.fillRect(0, 21, _W, 90, BLACK)  # y=21..110, full clear
        # Overlays take precedence over the layout under them. The
        # unpair prompt outranks the passkey because they should never
        # both be live at once (passkey only fires during a real
        # pairing flow, which doesn't exist on this build), but
        # ordering it first means future builds with both don't
        # accidentally render the wrong thing.
        if self._unpair_prompt:
            self._draw_unpair_overlay()
            return
        if self._passkey is not None:
            self._draw_passkey_overlay()
            return
        if self._connection_state in ("advertising", "disconnected"):
            self._draw_idle_main()
            return
        self._draw_connected_main()

    def _draw_idle_main(self):
        # Four short lines at size 1. y stride is 18 px which leaves
        # ~8 px of whitespace between 10-px-tall glyphs.
        _LCD.setTextSize(1)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.drawString("Waiting to pair...", 6, 28)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("Open Claude, go to", 6, 48)
        _LCD.drawString("Settings > Buddy", 6, 66)
        _LCD.drawString("and pick this one", 6, 84)

    def _bar_color(self, color):
        """Bar fill colour. The host (scripts/quota_push.py) resolves the
        codexbar pace stage — and the remaining-% fallback for windows with
        no pace — into an RGB int and sends it per bar, so the device just
        paints what it's told. GRAY_MID is only a defensive default for a
        bar with data but no colour (shouldn't happen: the host always pairs
        a colour with a util)."""
        return GRAY_MID if color is None else color

    def _draw_marker(self, bar_y: int, mx: int, color: int):
        """Paint the expected-pace tick: a 2 px coloured column with a 1 px
        black border on each side, spanning the bar height.

        The border is what makes it readable regardless of background — the
        tick can land on a same-hue pace fill (reserve: a green tick inside a
        green fill) or on the gray remainder (deficit), and the black edges
        keep it crisp either way. tick_anim repaints it after the glint sweeps
        through, so it survives the shimmer."""
        _LCD.fillRect(mx - 1, bar_y, 4, 8, BLACK)
        _LCD.fillRect(mx, bar_y, 2, 8, color)

    def _draw_bar(self, label: str, pct, y: int, color: int,
                  expected=None, line_color=None):
        """Draw a labeled horizontal progress bar showing remaining quota.

        pct is the *remaining* percentage (0..100); pct=100 means the bar is
        full. pct=None means "no data yet" — we draw an empty bar and a "--"
        label rather than inventing a number. `color` is the fill colour,
        chosen by the caller (pace stage, or remaining-% fallback).

        `expected` (a *used* %, 0..100) and `line_color` add CodexBar's
        expected-pace tick. Because the bar fills *remaining* from the left,
        a used-% maps to x = 6 + bar_w*(100-expected)/100: the tick lands
        inside the fill when actual used < expected (in reserve) and on the
        gray remainder when used > expected (in deficit) — which is why the
        host colours it green vs red. Both None -> no tick (e.g. Sonnet).

        Draws the filled and empty portions of the bar in a single pass (no
        intermediate full-gray state) to avoid visible flicker on in-place
        updates.  The percentage text area is always cleared to a fixed width
        before writing so variable-width strings ("9%" vs "100%") don't leave
        stale pixels from a previous wider value.

        Returns (bar_y, fill_w, marker) where marker is (mx, line_color) or
        None — _draw_data_rows records the filled region for tick_anim's glint
        to sweep across, and the marker so the glint can repaint it.
        """
        _LCD.setTextSize(1)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString(label, 6, y)
        # Clear a 36 px slot at the right edge for the pct label so that
        # a shorter string ("9%" / "--") always overwrites a longer one ("100%").
        _LCD.fillRect(_W - 38, y, 32, 10, BLACK)
        if pct is None:
            pct_str = "--"
            fill_pct = 0
        else:
            fill_pct = max(0, min(100, pct))
            pct_str = "{}%".format(fill_pct)
        _LCD.setTextColor(WHITE, BLACK)
        _LCD.drawString(pct_str, _right(y, 6, pct_str), y)
        bar_y = y + 13
        bar_w = _W - 12
        fill_w = int(bar_w * fill_pct // 100)
        # Draw filled portion then empty portion in one pass — never shows
        # an intermediate all-gray state, so the bar updates without flash.
        if fill_w > 0:
            _LCD.fillRect(6, bar_y, fill_w, 8, color)
        if fill_w < bar_w:
            _LCD.fillRect(6 + fill_w, bar_y, bar_w - fill_w, 8, GRAY_DIM)
        # Expected-pace tick on top of the just-drawn fill/gray. Clamp so the
        # 4 px-wide marker (1 px border each side) stays within [6, 6+bar_w).
        marker = None
        if expected is not None and line_color is not None:
            mx = 6 + (bar_w * (100 - max(0, min(100, expected)))) // 100
            if mx < 7:
                mx = 7
            elif mx > 6 + bar_w - 3:
                mx = 6 + bar_w - 3
            self._draw_marker(bar_y, mx, line_color)
            marker = (mx, line_color)
        return bar_y, fill_w, marker

    def _data_pcts(self):
        """Return (h5, wk, sonnet) remaining % (0..100), each None if unknown.

        `five_h_util` / `week_util` / `sonnet_util` are utilization
        percentages (0..100, "used") the companion sends from `codexbar`
        (primary / secondary / tertiary). We display *remaining*, i.e.
        100 - utilization. An absent field yields None — that bar renders a
        "--" no-data state rather than a fabricated number.
        """
        hb = self._last

        def _rem(key):
            v = hb.get(key)
            return None if v is None else max(0, min(100, 100 - int(v)))

        return _rem("five_h_util"), _rem("week_util"), _rem("sonnet_util")

    def _draw_data_rows(self):
        """Update the quota bars in-place without clearing the full content
        area, so the screen never goes black between heartbeats.

        Three rows (5h / Week / Sonnet) at y=24/48/72 — the identity band
        was dropped to make vertical room. A pending prompt occupies the
        Sonnet row's space (prompt box y=74..108), so we hide Sonnet then.

        Bar length is remaining quota; bar colour is whatever the host sent
        (`*_color`, derived from the codexbar pace stage on the Mac side).
        """
        hb = self._last
        h5, wk, snt = self._data_pcts()
        bars = []
        c5 = self._bar_color(hb.get("five_h_color"))
        by, fw, mk = self._draw_bar("5h", h5, 24, c5,
                                    hb.get("five_h_expected"),
                                    hb.get("five_h_expected_color"))
        bars.append({"bar_y": by, "fill_w": fw, "color": c5, "marker": mk})
        cw = self._bar_color(hb.get("week_color"))
        by, fw, mk = self._draw_bar("Week", wk, 48, cw,
                                    hb.get("week_expected"),
                                    hb.get("week_expected_color"))
        bars.append({"bar_y": by, "fill_w": fw, "color": cw, "marker": mk})
        if self._prompt:
            # The prompt box (y=74..108) takes the Sonnet row — don't animate
            # a bar that isn't drawn. 5h/Week stay live above it.
            self._draw_prompt_box(self._prompt)
        else:
            cs = self._bar_color(hb.get("sonnet_color"))
            # Sonnet (tertiary) has no pace, so no expected tick.
            by, fw, mk = self._draw_bar("Sonnet", snt, 72, cs)
            bars.append({"bar_y": by, "fill_w": fw, "color": cs, "marker": mk})
        # Replacing the list (vs mutating) means a fresh solid fill was just
        # painted under every bar, so any in-flight glint is already gone and
        # tick_anim restarts cleanly on its next frame.
        self._anim_bars = bars

    def _draw_connected_main(self):
        self._draw_data_rows()
        # After an overlay exits back to connected, _draw_main's full-clear
        # fillRect wiped the footer — restore it, but not while a prompt box
        # (y=74..108) overlaps the footer band (y=96..110).
        if not self._prompt and (self._last_stats or self._last_battery):
            self._draw_footer(self._last_stats, self._last_battery)

    def tick_anim(self):
        """Advance the quota-bar glint. Called every main-loop iteration.

        Cheap and self-gating: returns immediately unless we're in the
        connected steady state (no passkey / unpair overlay), self-throttles
        to _GLINT_FRAME_MS, and short-circuits during the rest gap after one
        cleanup frame.

        Flicker-free by writing every pixel at most once per frame: the band
        slices are drawn straight over the previous frame's gradient (colour ->
        colour, no flat intermediate), and only the columns the band has
        *vacated* since last frame are repainted to base. The static fill is
        never re-touched after _draw_bar paints it once — repainting the whole
        fill every frame (the old approach) flashed the band flat-to-gradient
        20x/s, which the panel shows directly since there's no frame buffer.
        Each bar's last-drawn glint span is cached in bar["glint"].
        """
        if (
            self._connection_state in ("advertising", "disconnected")
            or self._passkey is not None
            or self._unpair_prompt
            or not self._anim_bars
        ):
            return
        now = time.ticks_ms()
        if self._last_glint_ms and time.ticks_diff(now, self._last_glint_ms) < _GLINT_FRAME_MS:
            return
        self._last_glint_ms = now

        phase = self._glint_phase % _GLINT_CYCLE
        self._glint_phase += 1
        active = phase < _GLINT_SWEEP_STEPS
        if not active and not self._glint_was_active:
            # Resting and the trailing glint was already cleared last frame —
            # nothing to repaint, so spare the SPI bus until the next sweep.
            return
        self._glint_was_active = active

        gx = None
        if active:
            travel = phase * _GLINT_STEP
            if _GLINT_REVERSE:
                gx = (6 + _BAR_W) - travel  # band enters from the right edge
            else:
                gx = (6 - _GLINT_W) + travel
        for bar in self._anim_bars:
            fw = bar["fill_w"]
            if fw <= 0:
                bar["glint"] = None
                continue
            x0 = 6
            x1 = 6 + fw
            by = bar["bar_y"]
            base = bar["color"]
            # New band span, clipped to the filled width (empty while resting
            # or while the band sits entirely off this bar's fill).
            if gx is None:
                new_lo = new_hi = 0
            else:
                new_lo = gx if gx > x0 else x0
                gxe = gx + _GLINT_W
                new_hi = gxe if gxe < x1 else x1
            has_new = new_hi > new_lo
            # Draw the new gradient directly over the old one — in-band columns
            # go colour -> colour with no flat intermediate, so no flicker.
            if has_new:
                self._draw_glint(bar, gx)
            # Repaint to base ONLY the columns the band has vacated since last
            # frame; everything else is left as _draw_bar painted it.
            prev = bar.get("glint")
            if prev is not None:
                p_lo, p_hi = prev
                if not has_new:
                    _LCD.fillRect(p_lo, by, p_hi - p_lo, 8, base)
                else:
                    if p_lo < new_lo:
                        r_hi = new_lo if new_lo < p_hi else p_hi
                        _LCD.fillRect(p_lo, by, r_hi - p_lo, 8, base)
                    if p_hi > new_hi:
                        r_lo = new_hi if new_hi > p_lo else p_lo
                        _LCD.fillRect(r_lo, by, p_hi - r_lo, 8, base)
            bar["glint"] = (new_lo, new_hi) if has_new else None
            # The glint repaints [6, 6+fw) to base, wiping an expected tick
            # that sits inside the fill (the reserve case). Repaint it on top
            # after the sweep/cleanup so it stays put while the shimmer slides
            # behind it. A deficit tick lands on the gray remainder, which the
            # glint never touches, so it's left alone.
            mk = bar.get("marker")
            if mk is not None and mk[0] - 1 < x1:
                self._draw_marker(by, mk[0], mk[1])

    def _draw_glint(self, bar, gx):
        """Paint the soft highlight band onto one bar's fill, clipped to it.

        Walks the precomputed _GLINT_PROFILE (thin slices with a centre-bright
        quadratic falloff) and lightens the bar's base colour per slice, so the
        sheen is a smooth gradient rather than a few hard steps. Each slice is
        clipped to [6, 6+fill_w) so the glint never spills onto the gray
        remainder.
        """
        x0 = 6
        x1 = 6 + bar["fill_w"]
        by = bar["bar_y"]
        base = bar["color"]
        for off, w, frac in _GLINT_PROFILE:
            s = gx + off
            e = s + w
            a = s if s > x0 else x0
            b = e if e < x1 else x1
            if b > a:
                _LCD.fillRect(a, by, b - a, 8, _lighten(base, frac))

    def _draw_prompt_box(self, prompt: dict):
        # Orange-bordered box for the pending permission. y=74..109
        # gives us 35 px of height — two 10-px text rows with a 4-px
        # top gap, 2-px inter-row gap, and 2-px bottom gap. That's
        # enough breathing room to render cleanly without touching
        # either the tokens line at y=58..68 or the hint strip
        # hairline at y=111.
        _LCD.drawRect(3, 74, _W - 6, 35, ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, BLACK)
        tool_line = "PERM: " + prompt.get("tool", "?")
        h_tool = _set_font_auto(tool_line)
        while _LCD.textWidth(tool_line) > _W - 14 and len(tool_line) > 1:
            tool_line = tool_line[:-1]
        _LCD.drawString(tool_line, 7, 78)
        if h_tool > 10:
            _LCD.setFont(_LCD.FONTS.DejaVu9)
        hint = prompt.get("hint", "")
        h_hint = _set_font_auto(hint)
        _LCD.setTextColor(CREAM, BLACK)
        # CJK hint at 24 px: shift up so it fits in the box (y+24 <= 109).
        hint_y = 82 if h_hint > 10 else 94
        while _LCD.textWidth(hint) > _W - 14 and len(hint) > 1:
            hint = hint[:-1]
        _LCD.drawString(hint, 7, hint_y)
        if h_hint > 10:
            _LCD.setFont(_LCD.FONTS.DejaVu9)

    def _draw_unpair_overlay(self):
        if not self._unpair_prompt:
            return
        _LCD.fillRect(0, 21, _W, 90, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(RED, BLACK)
        # Two-line attention header so the destructive nature is clear
        # at a glance — this is the only path that wipes user state.
        _LCD.drawString("UNPAIR REQUEST", 6, 28)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.drawString("from connected host.", 6, 46)
        _LCD.drawString("Wipes name, owner, stats", 6, 64)
        _LCD.drawString("and disconnects.", 6, 78)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("Y confirm   N cancel", 6, 96)

    def _draw_passkey_overlay(self):
        if self._passkey is None:
            return
        _LCD.fillRect(0, 21, _W, 90, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.drawString("Pairing passkey:", 6, 28)
        # Size 4 passkey on DejaVu9 = 40 px tall, ~6 digits wide.
        # Centered with textWidth so size-4 doesn't throw off the math.
        pk_str = "{:06d}".format(self._passkey)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.setTextSize(4)
        pk_w = _LCD.textWidth(pk_str)
        _LCD.drawString(pk_str, (_W - pk_w) // 2, 44)
        _LCD.setTextSize(1)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("type it into Claude", 6, 96)

    def _draw_footer(self, stats: dict, battery: dict):
        # Thin stats line between main panel and hint strip, only in
        # the connected layout. y=96..110 (14 tall) holds one 10-px row.
        _LCD.fillRect(0, 96, _W, 15, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(GRAY_MID, BLACK)
        left = "Lv.{} a:{} d:{}".format(
            stats.get("lvl", 0),
            stats.get("appr", 0),
            stats.get("deny", 0),
        )
        _LCD.drawString(left, 6, 98)
        pct = max(0, min(100, battery.get("pct", 0)))
        # e.g. "0mA 3950mV 87%" — current (always 0; the Cardputer has no
        # current sensor) and voltage in CREAM, then the level.
        prefix = "{}mA {}mV ".format(battery.get("mA", 0), battery.get("mV", 0))
        pct_str = "{}%".format(pct)
        # Right-align the whole string as a unit — width from textWidth,
        # not a char-count estimate, so proportional-font surprises (e.g.
        # '%' being 8 px wide) don't push it off-screen and wrap.
        start_x = _right(98, 6, prefix + pct_str)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.drawString(prefix, start_x, 98)
        # Level colour tracks the voltage trend: ORANGE rising (charging),
        # GREEN falling (draining), WHITE steady.
        _LCD.setTextColor(self._batt_pct_color, BLACK)
        _LCD.drawString(pct_str, start_x + _LCD.textWidth(prefix), 98)

    def _redraw_chrome(self):
        _LCD.fillScreen(BLACK)
        self._draw_header()
        self._draw_main()
        self.restore_button_hints()
