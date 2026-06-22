# buddy

MicroPython app bundle for the M5Stack Cardputer-Adv. Installed onto `/flash/` by the [`m5-onboard`](../onboard/) skill — see the [monorepo README](../README.md) for the end-to-end flow.

## What's on the device

```
/flash/
├── main.py              launcher menu (replaces UIFlow's boot flow)
├── buddy_*.py           shared libs (BLE, UI, state, protocol, chars)
├── burst_frames.py      sprite frames
└── apps/
    ├── claude_buddy.py  BLE client that pairs with Claude.app's Hardware Buddy
    ├── hello_cardputer.py
    └── snake.py
```

`main.py` scans `/flash/apps/` at boot and shows every `.py` as a menu entry. Drop a new file in there, re-run `m5-onboard go` (or `install_apps.py --src buddy`), and it shows up.

## Claude Buddy (BLE)

Open Claude → Developer menu → **Hardware Buddy** → Connect. BLE-only. Stats (approvals / denials / level) persist across reboots via NVS under the `buddy` namespace.

## Quota bars (BLE companion)

The "5h remaining" / "Week remaining" bars show the real account quota. Claude.app's heartbeat doesn't carry quota (and the device can't reach the usage API itself), so a host companion supplies it:

```bash
pip install bleak
python3 scripts/quota_push.py            # scan for the buddy, push real quota every 60s
python3 scripts/quota_push.py --dry-run  # print the current quota without BLE
```

`quota_push.py` polls the usage API (reusing the Keychain token, like the `quota-check` skill) and writes `five_h_util` / `week_util` heartbeats to the device. It connects as the BLE central **in place of** Claude.app — one central at a time, so run it for a live quota readout and quit it to use Claude.app's prompt-approval. See [references/protocol.md](references/protocol.md#quota-fields-from-the-ble-companion-not-claudeapp).

## Iterating on device code

`scripts/` has dev tooling for editing device sources without re-running the full onboard flow:

```bash
# Push a subset of files over USB-serial
python3 scripts/push.py --port /dev/cu.usbmodem1101 --files apps/snake.py

# Watch device logs
python3 scripts/tail_serial.py --port /dev/cu.usbmodem1101

# One-shot REPL exec
python3 scripts/repl_run.py --port /dev/cu.usbmodem1101 --script "import os; print(os.listdir('/flash'))"
```

`gen_burst_frames.py` regenerates `burst_frames.py` from source sprites.

## References

- `references/` — BLE protocol notes for the Claude Buddy app

## License

Apache 2.0 — see the [root LICENSE](../LICENSE) and [LICENSE-THIRD-PARTY.md](../LICENSE-THIRD-PARTY.md).
