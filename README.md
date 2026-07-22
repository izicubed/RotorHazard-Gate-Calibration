# Gate Walkthrough Calibration (RotorHazard plugin)

Pre-flight calibration for RotorHazard in one walk: open a timed calibration
window and have every pilot **carry their powered-up quad through the gate,
over the timer**. The plugin watches each seat's live RSSI, detects the pass
and sets that seat's **EnterAt/ExitAt** automatically — applied to the node
and saved in the active profile, exactly as if tuned by hand.

Fully local and deterministic: no network, no AI, no extra hardware. Works on
any RotorHazard 4.3+ timer.

## The procedure

1. **Seat the pilots** — select the heat and make sure every seat has a
   frequency assigned.
2. On the **Run** page, expand the *Gate Walkthrough Calibration* bar and
   press **▶ Start calibration**. The window duration is set right on the
   panel (default 30 s). There is also a quickbutton in *Settings*.
3. Each pilot, **with their quad powered up** (video transmitter on),
   **walks through the gate holding the quad over the timer** — at roughly
   flying height, close to the timer — and keeps walking away from it. The
   pass must *end* (the RSSI must fall again) to be measured; if someone is
   still standing over the timer when the window closes, the best peak seen
   is used anyway.
   - One pilot at a time is easiest to supervise, but seats are independent —
     simultaneous passes on different channels work fine.
   - A second, stronger pass inside the same window simply re-calibrates that
     seat.
4. Watch the panel: each seat cell goes *waiting for pass* → *pass detected…*
   → *calibrated* with the applied `EnterAt/ExitAt` values. The window closes
   early once every seat is calibrated; seats with no detected pass keep
   their previous thresholds and are flagged.

Need to redo just one pilot (e.g. after a channel change)? Press the **↻**
button on that pilot's cell — it opens the same window for that seat alone.

## How thresholds are derived

For each seat the plugin takes the RSSI trace of the walk-through window:

- the **noise floor** is the window minimum;
- a **pass peak** must rise above the floor by a sensitivity threshold
  (Low 45 / Normal 30 / High 18 RSSI points) and be *completed* — the signal
  must drop back by ≥35 % of the pass height;
- **EnterAt** is set a configurable margin (default 25 % of the pass height)
  below the peak, kept well above the noise floor;
- **ExitAt** is set between the noise floor and EnterAt (35 % of the span).

Values are applied through RotorHazard's own calibration path
(`EnterAt`/`ExitAt` on the node **and** in the current frequency profile).

## Channel-change watch

Every successful calibration is remembered **per pilot**, together with the
band/channel it was made on. The plugin then monitors frequency edits, heat
changes and race staging: if a calibrated pilot ends up on a **different
channel**, the seat is flagged and a *"recalibration recommended"*
notification is raised (as an alert when it happens at race staging). The
recommendation is also shown in red on the collapsed panel bar, so it is
never hidden. Optionally the plugin can also warn at staging about pilots
who were never walk-through calibrated.

## Plays nicely with Adaptive Calibration

RotorHazard's *Adaptive Calibration* (Calibration Mode: Adaptive) pulls
thresholds from saved races on every heat change — useful, but it would
overwrite a fresh walk-through. With **Walk-through overrides Adaptive
Calibration** enabled (default), the plugin re-applies its values right after
Adaptive runs, **until the pilot races on that channel** — from then on the
race-derived values (which started from the walk-through anyway, plus any
operator/marshal adjustments) are newer and win again. A new walk-through
re-arms the priority.

## The panel

A slim collapsible bar on the **Run** page (collapsed by default, forced
closed while a race is staged/running). Collapsed it shows a *"N/M
calibrated"* summary — or a red *"⚠ recalibrate: …"* chip when someone's
channel changed. It expands automatically while a calibration window is open
and when a new recommendation appears. Dark, light and auto (browser/OS)
themes.

## Options (Settings → Gate Walkthrough Calibration)

| Option | Default | Meaning |
| --- | --- | --- |
| Calibration window (seconds) | 30 | How long the window stays open (also editable right on the panel) |
| Pass detection sensitivity | Normal | Required RSSI rise above the window noise floor |
| EnterAt margin below peak (%) | 25 | How far below the pass peak EnterAt is set |
| Recommend recalibration on channel change | On | Watch frequency/heat changes and flag stale calibrations |
| Walk-through overrides Adaptive Calibration | On | Re-apply walk-through thresholds after Adaptive Calibration until the pilot races on that channel |
| Also warn about never-calibrated pilots at staging | Off | Extra staging notification |
| Panel theme | Dark | Dark / Light / Auto (browser/OS) |

## Install

Install from the RotorHazard **community plugins** list, or manually: download
the release zip and extract it into your data directory so the plugin lands in
`plugins/gate_calibration/`, or upload the zip on the RotorHazard *Plugins*
page. Restart RotorHazard afterwards.

## Compatibility

- RotorHazard 4.3 / 4.4 (RHAPI ≥ 1.0), any hardware (S32_BPill, Pi hats,
  ESP32 nodes) — everything happens server-side on the standard RSSI history.
- No Python dependencies.

## License

MIT NON-AI (see [LICENSE](LICENSE)).
