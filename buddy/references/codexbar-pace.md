# codexbar JSON response — `usage` + `pace` reference

`scripts/quota_push.py` reads `codexbar --provider claude --format json`
to drive the device's quota bars, plus a second
`codexbar --provider opencodego --format json` call for the OpenCode Go
bars (see the OpenCode Go section below). The **`pace`** part of the
claude response is provided by the released CLI (verified on codexbar
v0.48.0) and barely documented upstream, so this file is our captured,
verified record of the shape we depend on.

- **Captured from:** codexbar `version` `2.1.186` (the `version` field is in
  each entry — check it if the shape below ever stops matching).
- **Upstream source of truth:** `Sources/CodexBarCore/UsagePace.swift`
  (`UsagePace.weekly(...)`, `stage(for:)`), `Sources/CodexBarCLI/CLIRenderer.swift`
  (formatting / JSON), `Sources/CodexBarCLI/CLIPayloads.swift` (`PacePayload`).
- **Stability:** unofficial / under-documented upstream. Treat field presence defensively.

---

## Command & top-level shape

```bash
codexbar --provider claude --format json    # `usage` subcommand is the default
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
| `usage.extraRateWindows[*]` | varies (e.g. Daily Routines) | — | `bar3_util` + `bar3_label` | **3rd (generic)** |
| `usage.tertiary`  | 7-day, Sonnet (10080)| `seven_day_sonnet.utilization` | `bar3_util` (fallback) | **3rd (generic)** |

Verification: a simultaneous `codexbar` + `oauth/usage` snapshot matched
`primary↔five_hour`, `secondary↔seven_day`, `tertiary↔seven_day_sonnet`
to the percent. (On this account `seven_day_opus` is null, so `tertiary`
was Sonnet — could differ for accounts that use Opus.)

**The 3rd buddy bar is generic.** It used to be hard-wired to `tertiary`
(Sonnet), but that window left the codexbar GUI in favour of an
**extra-rate window** ("Daily Routines"). `quota_push.py` now sources the
3rd bar from `usage.extraRateWindows` by default (sending its `title` as
`bar3_label` and `window.usedPercent` as `bar3_util`), falling back to
`usage.tertiary` only when no extra window exists. `--bar3-id` /
`--bar3-label` / `--bar3-value` override the source, name, and value.

---

## `pace` — consumption pace (expected-pace tick colour source)

Pace answers: *within a window, how far ahead/behind an even-burn baseline
is my actual usage?* Baseline = `expectedUsedPercent` (the % you'd be at if
you spent the window evenly over elapsed time).

```jsonc
"pace": {
  "primary":   { /* Session / 5h pace */ },
  "secondary": { /* Weekly pace        */ }
}
```

> **Only `primary` and `secondary` ever carry pace (claude provider).**
> `tertiary` and the `extraRateWindows` never do for claude.
> The buddy's bar *fill* is the provider brand colour (Claude orange /
> Go blue) regardless of pace; pace only drives the small expected-pace tick
> (`*_expected_color`), so a window with no pace simply gets no tick.
> The opencodego provider is different: it CAN emit `pace.tertiary` (see below).

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
missing. `quota_push.py` does: no stage → no expected-pace tick (the bar
fill stays the fixed provider brand colour regardless).

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

Note (claude provider): `pace` has no `tertiary`; `primary.willLastToReset=true`
so its `etaSeconds` is omitted; no `runOutProbability` anywhere.

---

## How `quota_push.py` consumes this

| device bar | name from | length from | fill colour from | tick colour from |
|---|---|---|---|---|
| 5h     | (fixed)                  | `usage.primary.usedPercent`   | Claude brand orange `0xCC785C` | `pace.primary.stage` → `_line_color_for` (green/red/yellow) |
| Week   | (fixed)                  | `usage.secondary.usedPercent` | Claude brand orange `0xCC785C` | `pace.secondary.stage` → `_line_color_for` (green/red/yellow) |
| 3rd    | `extraRateWindows[*].title` (default) | `extraRateWindows[*].window.usedPercent` (default) | Claude brand orange `0xCC785C` | **no tick** (no pace) |
| Go5h   | (fixed)                  | opencodego `usage.primary.usedPercent`   | Go brand blue `0x4D6BFE` | **no tick** (by decision) |
| GoWk   | (fixed)                  | opencodego `usage.secondary.usedPercent` | Go brand blue `0x4D6BFE` | **no tick** (by decision) |
| GoMo   | (fixed)                  | opencodego `usage.tertiary.usedPercent`  | Go brand blue `0x4D6BFE` | **no tick** (by decision) |

The bar fill is the fixed provider brand colour (`0xCC785C` Claude orange /
`0x4D6BFE` Go blue) — constant, independent of pace. Pace only drives the
expected-pace tick: the host resolves stage→RGB
(`_line_color_for`, green=reserve … red=deficit, yellow on pace) and sends
`<name>_util` + `<name>_color` per heartbeat, plus `bar3_label` for the 3rd
bar's name, and `*_expected` + `*_expected_color` for 5h/Week when a
classifiable stage is present. See
[protocol.md](protocol.md#quota-fields-from-the-ble-companion-not-claudeapp).

---

## OpenCode Go provider (`codexbar --provider opencodego`)

`quota_push.py` also queries the **OpenCode Go** provider for the
right-hand column of bars. Its response shape differs from the claude
provider above, so it gets its own captured record. Live shape from this
machine's `codexbar --provider opencodego --format json`:

```jsonc
[
  {
    "source": "local",              // or "web" when the account API is reachable
    "provider": "opencodego",
    "usage": {
      "primary":   { "usedPercent": 1,  "windowMinutes": 300,   "resetsAt": "...", "resetDescription": "..." },
      "secondary": { "usedPercent": 3,  "windowMinutes": 10080, "resetsAt": "...", "resetDescription": "..." },
      "tertiary":  { "usedPercent": 16, "windowMinutes": 43200, "resetsAt": "...", "resetDescription": "..." }
    },
    "pace": {
      "tertiary": { "stage": "farBehind", "expectedUsedPercent": 37, "deltaPercent": -21, "willLastToReset": true, "summary": "..." }
    }
  }
]
```

- The windows map to the three Go bars: `primary` → `go5h_util` (5-hour
  $12), `secondary` → `gowk_util` (weekly $30), `tertiary` → `gomo_util`
  (monthly $60), each `usedPercent` read with the same `.get()`-chaining
  as the claude path.
- **`pace` CAN appear, including `pace.tertiary`.** The "only primary and
  secondary ever carry pace" statement above is scoped to the **claude**
  provider. Live-verified on this machine:
  `"pace":{"tertiary":{"stage":"farBehind","expectedUsedPercent":37,"deltaPercent":-21,"willLastToReset":true,...}}`.
  A future consumer must not assume the opencodego shape is pace-free.
- **The local SQLite source computes only `usagePercent`.** The local
  reader sums session/week/month cost rows and divides by the plan limit
  (`OpenCodeGoLocalUsageReader.swift:183-204` in the CodexBar source); it
  never derives pace. The `pace.tertiary` seen in live output therefore
  comes from the web/non-local path or a different computation, so treat
  opencodego pace as best-effort, not guaranteed.
- **Anchored-monthly reset math.** The monthly window is anchored to the
  *earliest* stored usage row, not a calendar month:
  `monthBounds(now:anchorMs:)` with `anchorMs = earliest createdMs`
  (`OpenCodeGoLocalUsageReader.swift:190-204`) picks the billing month
  containing `now`, and `monthlyResetInSec` counts down to that bucket's
  end. So "monthly" is the current anchor-month bucket, and its bounds
  shift with the oldest row in the DB.
- **We skip the expected-pace tick for Go bars by decision**, not because
  the data can't contain pace: `quota_push.py` gives the Go bars a fixed
  DeepSeek-blue fill (`0x4D6BFE`) and sends no `*_expected` keys, matching
  the generic 3rd-bar precedent. (The device-basic bundle renders the host's
  brand colours — Claude orange `0xCC785C` / Go blue `0x4D6BFE` — for the
  bar fill; pace only ever drives the 5h/Week expected-pace tick.)
