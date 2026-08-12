"""IP5306 battery gauge reader for the device-basic bundle.

The IP5306 power IC (classic M5Stack Basic) exposes a 4-LED battery
SOC gauge at register 0x78 (read-only; high nibble = LED state).  The
register semantics are INVERTED vs the physical LED count in the M5Stack
firmware: 0x00 (no LEDs lit) means FULL (100%), and each LED that lights
as the cell drains drops the level by 25% (0x80->75, 0xC0->50, 0xE0->25,
anything else including 0xF0 -> 0).  This is the mapping used by both
M5Stack/src/utility/Power.cpp and M5Unified IP5306_Class.cpp.

The authoritative source is the firmware's own M5.Power.getBatteryLevel()
(which reads exactly this register through the firmware's I2C driver, and
additionally reports isCharging()); a raw machine.I2C read of 0x78 with
decode_led_bitmap() is the fallback when the M5 module is unavailable.
The IP5306 exposes NO voltage/current ADC (datasheet: "no internal
voltage and current information"), so mV/mA are always 0 on this chip;
voltage_to_pct() is a pure helper for firmware builds that DO report a
real battery voltage.

This module is import-safe on plain CPython: `machine` and `M5` are
imported only inside `_read_battery()`, and that function never raises
(any failure returns the exact fallback dict).
"""

_IP5306_ADDR = 0x75
_IP5306_REG_LED = 0x78


def decode_led_bitmap(byte):
    """Return the battery percent for any byte value, never raising.

    Maps the IP5306 0x78 high nibble using the M5Stack firmware's
    INVERTED LED semantics: 0x00 -> 100, 0x80 -> 75, 0xC0 -> 50,
    0xE0 -> 25, everything else (incl. 0xF0 = all four LEDs lit) -> 0.
    Total over all 256 byte values and never raises.
    """
    high = byte & 0xF0
    if high == 0x00:
        return 100
    if high == 0x80:
        return 75
    if high == 0xC0:
        return 50
    if high == 0xE0:
        return 25
    return 0


def voltage_to_pct(mv):
    """Map a battery voltage (mV) to a percent in 0..100, never raising.

    Linear curve from 3300 mV (0%) to 4200 mV (100%), clamped outside
    that range: pct = (mv - 3300) * 100 // 900.  Any non-numeric input
    maps to 0.  Total and never raises.
    """
    try:
        pct = (int(mv) - 3300) * 100 // 900
    except Exception:
        return 0
    if pct < 0:
        return 0
    if pct > 100:
        return 100
    return pct


def _read_battery():
    """Read the IP5306 battery gauge; returns a dict and never raises.

    Precedence:
      1. M5.Power.getBatteryLevel() (authoritative firmware gauge).
         If it returns a valid 0..100, report it along with
         M5.Power.isCharging() as `usb`.  If that fails or is invalid,
         fall through to the raw register read.
      2. Raw machine.I2C(0, sda=Pin(21), scl=Pin(22), freq=100000) read
         of register 0x78 at address 0x75, decoded with
         decode_led_bitmap().  `usb` stays True (uncharged status is
         not exposed this way).
      3. On any failure -- including an absent I2C bus -- returns
         EXACTLY {"pct": 0, "mV": 0, "mA": 0, "usb": True}.
    `machine` and `M5` are imported here (not at module level) so this
    file stays importable on CPython.
    """
    try:
        import M5

        pct = M5.Power.getBatteryLevel()
        if isinstance(pct, int) and 0 <= pct <= 100:
            usb = True
            try:
                usb = bool(M5.Power.isCharging())
            except Exception:
                pass
            return {"pct": pct, "mV": 0, "mA": 0, "usb": usb}
    except Exception:
        pass

    try:
        import machine

        i2c = machine.I2C(0, sda=machine.Pin(21), scl=machine.Pin(22), freq=100000)
        raw = i2c.readfrom_mem(_IP5306_ADDR, _IP5306_REG_LED, 1)[0]
        return {"pct": decode_led_bitmap(raw), "mV": 0, "mA": 0, "usb": True}
    except Exception:
        return {"pct": 0, "mV": 0, "mA": 0, "usb": True}
