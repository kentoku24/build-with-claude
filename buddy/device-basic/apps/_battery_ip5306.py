"""IP5306 battery gauge reader for the device-basic bundle.

The IP5306 power IC (classic M5Stack Basic) exposes a 4-LED battery
gauge at register 0x78 (high nibble = bitmap of lit LEDs). There is no
voltage/current readout, so pct is quantized to 0/25/50/75/100 by
counting consecutive lit bits from the MSB of the high nibble.

This module is import-safe on plain CPython: `machine` is imported only
inside `_read_battery()`, and that function never raises (any OSError
returns the fallback dict).
"""

_IP5306_ADDR = 0x75
_IP5306_REG_LED = 0x78


def decode_led_bitmap(byte):
    """Return the battery percent for any byte value, never raising.

    Counts consecutive lit bits starting from the MSB of the high
    nibble (bit 7 of the low byte mask), giving the canonical map
    0x00->0, 0x80->25, 0xC0->50, 0xE0->75, 0xF0->100.  A non-canonical
    byte such as 0x90 has its leading run of lit bits truncated at the
    first unlit bit, so it still maps into {0, 25, 50, 75, 100}; the
    function is total over all 256 byte values and never raises.
    """
    bits = 0
    v = byte & 0xF0
    while v & 0x80:
        bits += 1
        v = (v << 1) & 0xFF
    return bits * 25


def _read_battery():
    """Read the IP5306 gauge over I2C; returns a dict and never raises.

    Constructs machine.I2C(0, sda=Pin(21), scl=Pin(22), freq=100000)
    and reads register 0x78 of address 0x75.  `machine` is imported
    here (not at module level) so this file stays importable on
    CPython.  On any OSError -- including an absent I2C bus -- returns
    EXACTLY {"pct": 0, "mV": 0, "mA": 0, "usb": True}.
    """
    try:
        import machine

        i2c = machine.I2C(0, sda=machine.Pin(21), scl=machine.Pin(22), freq=100000)
        buf = i2c.readfrom_mem(_IP5306_ADDR, _IP5306_REG_LED, 1)
        high = buf[0] & 0xF0
        return {"pct": decode_led_bitmap(high), "mV": 0, "mA": 0, "usb": True}
    except OSError:
        return {"pct": 0, "mV": 0, "mA": 0, "usb": True}
