"""Headless geometry check for the Snake playfield (issue #20).

Runs on plain CPython (no device). snake.py imports M5, ui_theme, and
machine at module level and self-starts via the launcher entrypoint
(_main() at module bottom), so all three are faked and injected into
sys.modules BEFORE the import. The fake ui_theme only provides the
palette constants snake.py reads at module scope plus no-op functions
-- the test never inspects its behavior. Driving the fake buttons
(BtnC pressed, BtnA/BtnB released) lets the self-starting game loop
terminate: the snake runs off the right wall, game over returns
"exit", run() returns, and the fake machine.reset() is a no-op.

The fit criterion is offset-inclusive: the playfield origin sits at
_PLAY_Y=24 below the 22px header, so it must satisfy
_PLAY_X + _GRID_W*_CELL <= 320 and _PLAY_Y + _GRID_H*_CELL <= 240.
It exists to catch a port that shifts _PLAY_Y down and clips cells
into the footer. All constants are pulled from the imported module,
never hand-copied.
"""

import sys
import types


def _fake(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod
    return mod


class _FakeLCD:
    def fillRect(self, *args):
        pass

    def fillScreen(self, *args):
        pass

    def setTextSize(self, *args):
        pass

    def setTextColor(self, *args):
        pass

    def setCursor(self, *args):
        pass

    def print(self, *args):
        pass


class _FakeButton:
    def wasPressed(self):
        return False


class _FakeCButton:
    def wasPressed(self):
        return True


class _FakeM5:
    Lcd = _FakeLCD()
    BtnA = _FakeButton()
    BtnB = _FakeButton()
    BtnC = _FakeCButton()

    def update(self):
        pass


class _FakeTime:
    def ticks_ms(self):
        return 0

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        pass


# ui_theme: constants snake.py reads at module scope + no-op functions.
ui_theme = _fake("ui_theme")
ui_theme.BLACK = 0x000000
ui_theme.ORANGE = 0xCC785C
ui_theme.CREAM = 0xF0EEE6
ui_theme.DARK = 0x1F1F1F
ui_theme.ORANGE_DIM = 0x6A3E2E
ui_theme.RED = 0xFF0000


def _noop(*args, **kwargs):
    pass


def _tick_burst(cx, cy, frame, last_tick, **kwargs):
    return frame + 1, last_tick


ui_theme.header = _noop
ui_theme.footer = _noop
ui_theme.clear_body = _noop
ui_theme.draw_burst = _noop
ui_theme.tick_burst = _tick_burst

# snake.py imports machine at module bottom for the entrypoint.
machine = _fake("machine")
machine.reset = _noop

sys.modules["M5"] = _FakeM5()
sys.modules["time"] = _FakeTime()

sys.path.insert(0, "buddy/device-basic/apps")
import snake  # noqa: E402  (after sys.modules injection)

ok = True

right = snake._PLAY_X + snake._GRID_W * snake._CELL
if right > 320:
    ok = False
    print("FAIL grid fit: _PLAY_X + _GRID_W * _CELL = %d + %d * %d = %d > 320"
          % (snake._PLAY_X, snake._GRID_W, snake._CELL, right))

bottom = snake._PLAY_Y + snake._GRID_H * snake._CELL
if bottom > 240:
    ok = False
    print("FAIL grid fit: _PLAY_Y + _GRID_H * _CELL = %d + %d * %d = %d > 240"
          % (snake._PLAY_Y, snake._GRID_H, snake._CELL, bottom))

if ok:
    print("PASS grid fit: right=%d bottom=%d" % (right, bottom))
    sys.exit(0)

sys.exit(1)
