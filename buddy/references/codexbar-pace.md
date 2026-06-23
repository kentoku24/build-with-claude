# codexbar JSON response — `usage` + `pace` reference

`scripts/quota_push.py` reads `codexbar --provider anthropic --format json`
to drive the device's quota bars. The **`pace`** part of that response is
new and barely documented upstream — it landed in
[steipete/CodexBar#1722](https://github.com/steipete/CodexBar/pull/1722) —
so this file is our captured, verified record of the shape we depend on.

- **Captured from:** codexbar `version` `2.1.186` (the `version` field is in
  each entry — check it if the shape below ever stops matching).
- **Upstream source of truth:** `Sources/CodexBarCore/UsagePace.swift`
  (`UsagePace.weekly(...)`, `stage(for:)`), `Sources/CodexBarCLI/CLIRenderer.swift`
  (formatting / JSON), `Sources/CodexBarCLI/CLIPayloads.swift` (`PacePayload`).
- **Stability:** unofficial / pre-merge. Treat field presence defensively.

---

## Command & top-level shape

```bash
codexbar --provider anthropic --format json    # `usage` subcommand is the default
```

Returns a JSON **array**; we read element `[0]`. Each entry:

```jsonc
{
  "provider": "claude",
  "source": "web",
  "version": "2.1.186",
  "usage": { /* raw windows — drives bar LENGTH */ },
  "pace":  { /* derived pace  — drives bar COLOUR */ }   // may be absent (see Guards)
}
```

---

## `usage` — the windows (bar length)

```jsonc
"usage": {
  "primary":   { "usedPercent": 31, "windowMinutes": 300,   "resetsAt": "2026-06-23T05:00:00Z", "resetDescription": "Jun 23 at 2:00PM" },
  "secondary": { "usedPercent": 13, "windowMinutes": 10080, "resetsAt": "...", "resetDescription": "..." },
  "tertiary":  { "usedPercent": 6,  "windowMinutes": 10080, "resetsAt": "...", "resetDescription": "..." },
  "extraRateWindows": [ { "id": "claude-routines", "title": "Daily Routines", "window": { "windowMinutes": 10080, "usedPercent": 0 } } ],
  "identity": { "accountEmail": "...", "accountOrganization": "...", "providerID": "claude" },
  "updatedAt": "..."
}
```

`usedPercent` is utilization (0..100, **used**). The device draws
**remaining = `100 − usedPercent`** as the bar length.

### Window mapping (verified against the labeled usage API)

| codexbar key | window | usage API equivalent | buddy field | buddy bar |
|---|---|---|---|---|
| `usage.primary`   | 5-hour (300 min)     | `five_hour.utilization`        | `five_h_util`  | **5h** |
| `usage.secondary` | 7-day, all (10080)   | `seven_day.utilization`        | `week_util`    | **Week** |
| `usage.tertiary`  | 7-day, Sonnet (10080)| `seven_day_sonnet.utilization` | `sonnet_util`  | **Sonnet** |

Verification: a simultaneous `codexbar` + `oauth/usage` snapshot matched
`primary↔five_hour`, `secondary↔seven_day`, `tertiary↔seven_day_sonnet`
to the percent. (On this account `seven_day_opus` is null, so `tertiary`
is Sonnet — could differ for accounts that use Opus.)

---

## `pace` — consumption pace (bar colour source)

Pace answers: *within a window, how far ahead/behind an even-burn baseline
is my actual usage?* Baseline = `expectedUsedPercent` (the % you'd be at if
you spent the window evenly over elapsed time).

```jsonc
"pace": {
  "primary":   { /* Session / 5h pace */ },
  "secondary": { /* Weekly pace        */ }
}
```

> **Only `primary` and `secondary` ever carry pace. `tertiary` (Sonnet) never does.**
> The buddy therefore colours Sonnet by a remaining-% fallback, not pace.

### Per-window fields

| field | type | notes |
|---|---|---|
| `stage` | string | one of 7 fixed values (table below). **The reliable classification.** |
| `deltaPercent` | int | `round(used − expected)`. **+ = deficit** (ahead/over-pace), **− = reserve** (behind/under-pace) |
| `expectedUsedPercent` | int | `round(expected)`, 0–100 |
| `willLastToReset` | bool | `true` = current rate won't exhaust the window before it resets |
| `etaSeconds` | int | seconds until projected exhaustion. **Key omitted entirely** when `willLastToReset=true` or not computable |
| `runOutProbability` | number | **always omitted by the CLI** (only the GUI's historical evaluator fills it) — treat as never present |
| `summary` | string | human one-liner, e.g. `"2% in deficit \| Expected 11% used \| Runs out in 5d 2h"` |

Notes:
- `etaSeconds` and `runOutProbability` follow the "omit when nil" convention —
  they are **absent keys**, never `null`.
- Actual `usedPercent` is **not** repeated inside `pace` (read it from `usage`).

### `stage` — fixed 7-value enum (the important part)

Let `Δ = used − expected` (percentage points, computed on the **raw,
pre-round** values). Sign convention: **positive = deficit / over pace
(`*Ahead`)**, **negative = reserve / under pace (`*Behind`)**.

| `stage` | condition (`Δ`) | meaning | summary label |
|---|---|---|---|
| `farBehind`      | `Δ ≤ −12`        | big reserve   | `"<n>% in reserve"` |
| `behind`         | `−12 < Δ ≤ −6`   | reserve       | `"<n>% in reserve"` |
| `slightlyBehind` | `−6 < Δ ≤ −2`    | slight reserve| `"<n>% in reserve"` |
| `onTrack`        | `−2 ≤ Δ ≤ 2`     | on pace       | `"On pace"` |
| `slightlyAhead`  | `2 < Δ ≤ 6`      | slight deficit| `"<n>% in deficit"` |
| `ahead`          | `6 < Δ ≤ 12`     | deficit       | `"<n>% in deficit"` |
| `farAhead`       | `Δ > 12`         | big deficit   | `"<n>% in deficit"` |

Boundaries are inclusive on the upper side (`<=`); `|Δ| ≤ 2` is always
`onTrack`. Because `stage` uses the **raw** Δ while `deltaPercent` is
rounded, a boundary case can look inconsistent (e.g. raw `Δ=2.4` →
`slightlyAhead` but `deltaPercent=2`). **Trust `stage` for classification;
`deltaPercent` is display-only.**

```
Δ ≤ -12        farBehind       (reserve)
-12 < Δ ≤ -6   behind          (reserve)
-6  < Δ ≤ -2   slightlyBehind  (reserve)
-2  ≤ Δ ≤ 2    onTrack         (on pace)
2   < Δ ≤ 6    slightlyAhead   (deficit)
6   < Δ ≤ 12   ahead           (deficit)
Δ > 12         farAhead        (deficit)
```

### Guards — when `pace` (or a sub-window) is absent

A window's pace is emitted **only if all** hold; otherwise that key is
omitted, and if neither `primary` nor `secondary` qualifies the whole
`pace` object is omitted:

1. the window (`primary`/`secondary`) exists;
2. provider is in the pace allowlist (claude qualifies for both windows);
3. `usedPercent < 100`;
4. `resetsAt` present;
5. `0 < timeUntilReset ≤ duration` (we're inside the window);
6. not the degenerate `elapsed == 0 && used > 0`;
7. **`expectedUsedPercent ≥ 3`** — no pace early in a window.

→ Consumers must handle `pace`, `pace.primary`, or `pace.secondary` being
missing. `quota_push.py` does: no stage → remaining-% colour fallback.

### CLI vs GUI (caveat)

The CLI weekly pace is **plain linear** (`UsagePace.weekly`, no workDays
correction, no Codex historical correction). The menu-bar GUI applies
extra corrections, so `stage`/`deltaPercent`/`etaSeconds` for the **weekly**
window can differ between `codexbar` CLI and the GUI. **Session (5h) matches.**

---

## Real captured example

```jsonc
{
  "provider": "claude",
  "source": "web",
  "version": "2.1.186",
  "usage": {
    "primary":   { "usedPercent": 13, "windowMinutes": 300,   "resetsAt": "2026-06-23T05:00:00Z" },
    "secondary": { "usedPercent": 13, "windowMinutes": 10080, "resetsAt": "2026-06-29T07:00:00Z" },
    "tertiary":  { "usedPercent": 6,  "windowMinutes": 10080, "resetsAt": "2026-06-29T07:00:00Z" }
  },
  "pace": {
    "primary": {
      "stage": "farBehind",
      "deltaPercent": -14,
      "expectedUsedPercent": 27,
      "willLastToReset": true,
      "summary": "14% in reserve | Expected 27% used | Lasts until reset"
    },
    "secondary": {
      "stage": "slightlyAhead",
      "deltaPercent": 2,
      "expectedUsedPercent": 11,
      "etaSeconds": 442295,
      "willLastToReset": false,
      "summary": "2% in deficit | Expected 11% used | Runs out in 5d 2h"
    }
  }
}
```

Note: `pace` has no `tertiary`; `primary.willLastToReset=true` so its
`etaSeconds` is omitted; no `runOutProbability` anywhere.

---

## How `quota_push.py` consumes this

| device bar | length from | colour from |
|---|---|---|
| 5h     | `usage.primary.usedPercent`   | `pace.primary.stage`   → `_STAGE_COLORS` |
| Week   | `usage.secondary.usedPercent` | `pace.secondary.stage` → `_STAGE_COLORS` |
| Sonnet | `usage.tertiary.usedPercent`  | **no pace** → remaining-% fallback |

The host resolves stage→RGB (`_STAGE_COLORS`, green=reserve … red=deficit)
and a remaining-% fallback for any window with no stage (Sonnet always, or
the 5h/Week windows when a Guard above suppressed pace), then sends
`<name>_util` + `<name>_color` per heartbeat. See
[protocol.md](protocol.md#quota-fields-from-the-ble-companion-not-claudeapp).
