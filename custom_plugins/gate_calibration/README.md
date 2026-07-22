# Gate Walkthrough Calibration

A RotorHazard plugin for quick pre-flight calibration: open a timed window and
have every pilot carry their **powered-up** quad through the gate, over the
timer. The plugin watches each seat's live RSSI, detects the pass and sets
that seat's **EnterAt/ExitAt** automatically. Fully local — no network, no AI.

## How it works

1. Seat the pilots (select the heat) and make sure frequencies are assigned.
2. On the **Run** page expand the collapsed **Gate Walkthrough Calibration**
   bar and press **Start calibration** (window duration is set right on the
   panel, default 30 s). A quickbutton is also available in Settings. Each
   seat cell also has a **↻** button that opens a window for that pilot
   alone — handy for recalibrating a single pilot after a channel change.
3. Each pilot walks through the gate holding their powered-up quad over the
   timer, then carries it away. One pilot at a time is easiest, but seats are
   independent — simultaneous passes on different channels work too.
4. As soon as a clear RSSI peak completes for a seat, the plugin derives
   thresholds from that pass and applies them through RotorHazard's own
   calibration path — sent to the node **and saved in the current profile**:
   - `EnterAt = peak − margin% of the pass height` (margin configurable,
     default 25%), kept well above the noise floor;
   - `ExitAt` between the noise floor and EnterAt (35% of the span).
   A second, stronger pass within the window re-calibrates the seat.
5. When the window closes you get a per-seat summary; seats with no detected
   pass keep their previous thresholds and are flagged.

The window auto-closes early once every seat is calibrated, and aborts safely
if a race gets staged.

## Channel-change watch

Every successful calibration is remembered per pilot together with the
frequency (band/channel) it was made on. Afterwards the plugin monitors
frequency edits, heat changes and race staging: if a calibrated pilot ends up
on a **different channel**, the seat is flagged on the panel and a
*"recalibration recommended"* notification is raised (as an alert when it
happens at race staging). Optionally it can also warn at staging about pilots
who were never walk-through calibrated.

The panel stays **collapsed** by default (and is forced closed while a race
is staged or running) — day-to-day it is just a slim bar with a
"N/M calibrated" summary. It expands automatically while a calibration
window is open and when a new channel-change recommendation appears; the
recommendation with the pilot's name is also shown in red on the collapsed
bar itself, so it is never hidden. Use the pilot's **↻** button to
recalibrate just that seat.

## Options (Settings → Gate Walkthrough Calibration)

| Option | Default | Meaning |
| --- | --- | --- |
| Calibration window (seconds) | 30 | How long the window stays open |
| Pass detection sensitivity | Normal | Required RSSI rise above the window noise floor (Low 45 / Normal 30 / High 18) |
| EnterAt margin below peak (%) | 25 | How far below the pass peak EnterAt is set |
| Recommend recalibration on channel change | On | Watch frequency/heat changes and flag stale calibrations |
| Walk-through overrides Adaptive Calibration | On | Re-apply walk-through thresholds after Adaptive Calibration until the pilot races on that channel |
| Also warn about never-calibrated pilots at staging | Off | Extra staging notification |
| Panel theme | Dark | Dark / Light / Auto (browser/OS) |

## Notes

- Works on RotorHazard 4.3 and 4.4 (RHAPI 1.0+).
- Walk the quad **through** the gate at flying height, close to the timer,
  and keep walking — the pass must end (RSSI must drop) to be measured. If a
  pilot stands still over the timer until the window closes, the best peak
  seen is used anyway.
- RotorHazard's **Adaptive Calibration** ("Calibration Mode: Adaptive") pulls
  thresholds from saved races on every heat change — a useful feature that
  would normally overwrite a fresh walk-through calibration. With
  *Walk-through overrides Adaptive Calibration* enabled (default), the plugin
  re-applies its values right after Adaptive runs, **until the pilot races on
  that channel** — from then on the race-derived values (which started from
  the walk-through anyway, plus any operator/marshal adjustments) are newer
  and win again. A new walk-through calibration re-arms the priority. The
  panel shows a red warning chip only when Adaptive is on and this override
  is disabled.
- Thresholds are stored in the active frequency profile, exactly as if set
  manually from the Settings page.
