'''
Gate walk-through calibration for RotorHazard.

The operator opens a calibration window (default 30 s, configurable). During
the window each pilot, with their quad powered up, walks through the gate and
holds/carries the craft over the timer. The plugin watches every seat's live
RSSI trace (node.history_values / history_times — populated even between
races, where RotorHazard keeps the last 60 s) and, when a clear pass peak is
seen for a seat, derives EnterAt/ExitAt from that pass and applies them
through RotorHazard's own calibration path (node + current profile).

Each successful calibration is remembered per pilot together with the
frequency (band/channel) it was made on. Afterwards the plugin keeps
watching: when a pilot's channel changes (frequency set, heat change, race
staging) it flags the seat and recommends a recalibration.

Fully local — no network, no AI.
'''

import json
import logging
from time import time, monotonic

import gevent

import RHUtils
from RHRace import RaceStatus
from RHUI import UIField, UIFieldType, UIFieldSelectOption

logger = logging.getLogger(__name__)

PLUGIN_ID = 'gate_calibration'

OPT_SECS = 'gcal_secs'
OPT_SENS = 'gcal_sens'
OPT_MARGIN = 'gcal_margin'
OPT_WATCH = 'gcal_watch'
OPT_WARN_UNCAL = 'gcal_warn_uncal'
OPT_PRIORITY = 'gcal_priority'
OPT_THEME = 'gcal_theme'
OPT_RECORDS = 'gcal_records'      # JSON store of per-pilot calibration records

STATE_EVENT = 'gate_cal_state'    # server -> browser panel snapshot
EV_GET = 'gate_cal_get'
EV_START = 'gate_cal_start'
EV_STOP = 'gate_cal_stop'

POLL_SECS = 0.25
PUSH_SECS = 1.0                   # countdown re-broadcast cadence
MIN_SAMPLES = 4
# minimum peak rise above the window's noise floor to call it a gate pass
SENS_MIN_RISE = {'low': 45, 'normal': 30, 'high': 18}
REAPPLY_RISE = 10                 # later peak must beat the applied one by this
PRE_WINDOW_SECS = 60              # pre-window history probed for the true floor
DEFAULT_MARGIN = 35               # EnterAt margin below the walk peak (% of span)
EXIT_FRACTION = 0.25              # ExitAt sits this far up the floor->enter span
FLY_PEAK_GAP = 5                  # EnterAt stays below flying peaks by this
RECORD_VER = 2                    # calibration-record format (bump = re-derive)
TIMER_SOURCES = (0, 2, 3)         # LapSource REALTIME / RECALC / AUTOMATIC


class GateCalibrator:
    def __init__(self, rhapi):
        self._rhapi = rhapi
        self._gen = 0             # monitor generation token
        self._stop = True
        self._phase = 'idle'
        self._message = ''
        self._seats = {}          # node_index -> per-window state
        self._t_start = 0.0
        self._t_end = 0.0
        self._last_push = 0.0
        self._notified = set()    # (record_key, frequency) combos already flagged

    # ------------------------------------------------------------------ setup

    def register_ui(self, _args=None):
        ui = self._rhapi.ui
        fields = self._rhapi.fields
        ui.register_panel(PLUGIN_ID, 'Gate Walkthrough Calibration', 'settings',
                          order=0)

        def opt(name, label, ftype, value, desc, options=None):
            kw = dict(name=name, label=label, field_type=ftype, value=value,
                      desc=desc)
            if options is not None:
                kw['options'] = options
            fields.register_option(UIField(**kw), PLUGIN_ID)

        opt(OPT_SECS, 'Calibration window (seconds)', UIFieldType.BASIC_INT, 30,
            'How long the walk-through window stays open. Every pilot should '
            'carry their powered-up quad through the gate, over the timer, '
            'within this time.')
        opt(OPT_SENS, 'Pass detection sensitivity', UIFieldType.SELECT, 'normal',
            'How strong the RSSI peak (vs the window noise floor) must be to '
            'count as a gate pass.', options=[
                UIFieldSelectOption('low', 'Low (only very clear peaks)'),
                UIFieldSelectOption('normal', 'Normal'),
                UIFieldSelectOption('high', 'High (weaker peaks too)')])
        opt(OPT_MARGIN, 'EnterAt margin below peak (%)', UIFieldType.BASIC_INT,
            DEFAULT_MARGIN,
            'EnterAt is set this share of the pass height below the observed '
            'peak. A hand-carried quad passes closer/slower than a flying one, '
            'so its peak reads high — keep a generous margin. When the pilot '
            'has raced on this channel, EnterAt is additionally capped below '
            'their real flying pass peaks.')
        opt(OPT_WATCH, 'Recommend recalibration on channel change',
            UIFieldType.CHECKBOX, True,
            'Watch frequency/heat changes; when a calibrated pilot ends up on '
            'a different channel, flag the seat and notify.')
        opt(OPT_PRIORITY, 'Walk-through overrides Adaptive Calibration',
            UIFieldType.CHECKBOX, True,
            'When Adaptive Calibration is enabled, re-apply walk-through '
            'thresholds after each heat change — until the pilot races on '
            'that channel, after which the (newer) race values win again.')
        opt(OPT_WARN_UNCAL, 'Also warn about never-calibrated pilots at staging',
            UIFieldType.CHECKBOX, False,
            'On race staging, notify when a seated pilot has no walk-through '
            'calibration record at all.')
        opt(OPT_THEME, 'Panel theme', UIFieldType.SELECT, 'dark',
            'Colour scheme of the calibration panel on the Run page. Auto '
            'follows each viewer\'s browser/OS preference.', options=[
                UIFieldSelectOption('dark', 'Dark'),
                UIFieldSelectOption('light', 'Light'),
                UIFieldSelectOption('auto', 'Auto (follow browser/OS)')])

        ui.register_quickbutton(PLUGIN_ID, 'gcal_start_btn',
                                'Start calibration window',
                                self._quickbutton_start)

        self._register_loader(ui, fields)
        try:
            self._upgrade_records()
        except Exception:
            logger.exception('gate_calibration record upgrade failed')

    def _register_loader(self, ui, fields):
        # Inject the panel front-end on the Run page (same loader trick as
        # claude_marshal: a markdown panel carrying a script tag).
        loader = ('<script src="/gate_calibration/static/'
                  'gate_calibration.js"></script>')
        panel = 'gate_cal_load_run'
        ui.register_panel(panel, 'Gate Walkthrough Calibration', 'run', order=0)
        ui.register_markdown(panel, 'gate_cal_boot_run', loader)
        fields.register_option(UIField(
            name='_gate_cal_boot_run', label='', value='',
            field_type=UIFieldType.TEXT, private=True, desc=loader), panel)

    def _quickbutton_start(self, _args=None):
        self.start_window()

    # ------------------------------------------------------------- rh access

    @property
    def _ctx(self):
        return self._rhapi.db._racecontext

    def _opt(self, name, default=None):
        try:
            val = self._rhapi.db.option(name)
        except Exception:
            return default
        return default if val is None or val == '' else val

    def _opt_bool(self, name, default=False):
        return self._opt(name, default) in (True, 1, '1', 'true', 'True')

    def _opt_int(self, name, default):
        try:
            return int(float(self._opt(name, default)))
        except (TypeError, ValueError):
            return default

    def _notify(self, message, interrupt=False):
        try:
            if interrupt:
                self._rhapi.ui.message_alert(message)
            else:
                self._rhapi.ui.message_notify(message)
        except Exception:
            try:
                self._rhapi.ui.message_notify(message)
            except Exception:
                pass

    def _callsign(self, pilot_id):
        try:
            p = self._ctx.rhdata.get_pilot(pilot_id)
            return getattr(p, 'callsign', None) or None
        except Exception:
            return None

    def _chan_label(self, idx, freq):
        '''Band+channel label ("R8") from the current profile, else the raw
        frequency in MHz.'''
        try:
            freqs = json.loads(self._ctx.race.profile.frequencies)
            band = (freqs.get('b') or [])[idx]
            chan = (freqs.get('c') or [])[idx]
            if band and chan:
                return '{}{}'.format(band, chan)
        except Exception:
            pass
        return str(freq)

    # ---------------------------------------------------------- record store

    def _records(self):
        try:
            data = json.loads(self._opt(OPT_RECORDS, '') or '{}')
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_records(self, recs):
        try:
            self._rhapi.db.option_set(OPT_RECORDS, json.dumps(recs))
        except Exception:
            logger.exception('gate_calibration record save failed')

    @staticmethod
    def _record_key(pilot_id, idx):
        return str(pilot_id) if pilot_id else 'seat:{}'.format(idx)

    def _trace_floor(self, pilot_id, frequency):
        '''Real noise floor for this pilot/frequency: the minimum of their most
        recent stored race traces (a race trace spans minutes of flying, so its
        floor is the honest between-passes level).'''
        if not pilot_id:
            return None
        try:
            prs = [pr for pr in
                   (self._ctx.rhdata.get_savedPilotRaces() or [])
                   if pr.pilot_id == pilot_id
                   and int(pr.frequency or 0) == int(frequency or 0)]
            floors = []
            for pr in sorted(prs, key=lambda p: p.id, reverse=True)[:2]:
                try:
                    vals = json.loads(pr.history_values)
                except Exception:
                    continue
                if vals and len(vals) > 10:
                    floors.append(min(vals))
            return min(floors) if floors else None
        except Exception:
            return None

    def _upgrade_records(self):
        '''One-time re-derivation of stored records after the calibration
        formula changed (v2: true noise floor + flying-peak cap — a walk
        window where the powered quad idled next to the gate used to inflate
        the floor, and a hand-carried peak overshoots flying peaks, both
        pushing EnterAt/ExitAt above what real passes reach). Records whose
        pilot has since raced are left alone: adaptive calibration owns them.'''
        recs = self._records()
        changed = False
        for key, rec in recs.items():
            if rec.get('ver', 1) >= RECORD_VER or rec.get('raced'):
                continue
            rec['ver'] = RECORD_VER
            changed = True
            peak, floor = rec.get('peak'), rec.get('floor')
            if not peak or not floor:
                continue
            pilot_id, freq = rec.get('pilot_id'), rec.get('frequency')
            tfloor = self._trace_floor(pilot_id, freq)
            if tfloor is not None and tfloor < floor:
                floor = tfloor
            enter, exit_at = self._derive(
                peak, floor, self._fly_peak_median(pilot_id, freq))
            if enter is None \
                    or (enter == rec.get('enter') and exit_at == rec.get('exit')):
                continue
            old_e, old_x = rec.get('enter'), rec.get('exit')
            rec.update(enter=enter, exit=exit_at, floor=floor)
            who = rec.get('callsign') or key
            self._notify('Gate Calibration: {} walkthrough re-derived with the '
                         'updated formula — EnterAt {}, ExitAt {} (was {}/{})'
                         .format(who, enter, exit_at, old_e, old_x))
            logger.info('gate_calibration record upgrade %s: %s/%s -> %s/%s '
                        '(floor %s, peak %s)', who, old_e, old_x, enter,
                        exit_at, floor, peak)
        if changed:
            self._save_records(recs)
            try:
                # seats may already carry the old record values (adaptive +
                # restore ran before the upgrade) — re-apply immediately
                self._restore_walkthrough()
            except Exception:
                logger.exception('gate_calibration post-upgrade restore failed')

    # -------------------------------------------------------------- snapshot

    def _active_seats(self, ctx):
        '''Seats the panel/window works with: only those with a pilot in the
        current heat — empty seats that merely have a frequency configured are
        noise for the operator. When NO pilot is seated at all (no heat set up
        yet), fall back to every frequency-enabled seat so a walk-through is
        still possible.'''
        race = ctx.race
        out = []
        for node in ctx.interface.nodes:
            if not node.frequency:
                continue
            pilot_id = (race.node_pilots or {}).get(node.index,
                                                    RHUtils.PILOT_ID_NONE)
            out.append((node, pilot_id))
        seated = [(n, p) for n, p in out if p]
        return seated if seated else out

    def _seat_rows(self):
        '''Panel rows. During a window: live per-seat progress. Otherwise:
        calibration freshness for the currently seated pilots.'''
        ctx = self._ctx
        recs = self._records()
        rows = []
        for node, pilot_id in self._active_seats(ctx):
            idx = node.index
            freq = node.frequency
            callsign = self._callsign(pilot_id) or 'Seat {}'.format(idx + 1)
            row = {'seat': idx, 'callsign': callsign,
                   'chan': self._chan_label(idx, freq), 'freq': freq}
            st = self._seats.get(idx)
            if self._phase == 'running' and st is not None:
                row.update({'status': st['status'], 'peak': st.get('peak'),
                            'enter': st.get('enter'), 'exit': st.get('exit')})
            else:
                rec = recs.get(self._record_key(pilot_id, idx))
                if st is not None and st['status'] == 'nopass':
                    row['status'] = 'nopass'
                elif rec is None:
                    row['status'] = 'uncal'
                elif int(rec.get('frequency', 0)) != int(freq):
                    row['status'] = 'stale'
                    row['cal_chan'] = rec.get('chan') or str(rec.get('frequency'))
                else:
                    row['status'] = 'ok'
                    row['enter'] = rec.get('enter')
                    row['exit'] = rec.get('exit')
                    if rec.get('ts'):
                        row['age_min'] = int(max(0, time() - rec['ts']) // 60)
            rows.append(row)
        return rows

    def _snapshot(self):
        snap = {'phase': self._phase, 'seats': self._seat_rows(),
                'secs': self._opt_int(OPT_SECS, 30),
                'theme': self._opt(OPT_THEME, 'dark'),
                'message': self._message}
        if self._phase == 'running':
            snap['remaining'] = round(max(0.0, self._t_end - monotonic()), 1)
        snap['adaptive_on'] = self._calibration_mode() == 1
        snap['priority_on'] = self._opt_bool(OPT_PRIORITY, True)
        try:
            snap['race_active'] = \
                self._ctx.race.race_status != RaceStatus.READY
        except Exception:
            snap['race_active'] = False
        return snap

    def _calibration_mode(self):
        # RotorHazard 4.4 moved this to the server config; 4.3 keeps it as a
        # db option (reading the migrated one via db.option logs deprecations)
        try:
            return int(self._rhapi.config.get_item('TIMING',
                                                   'calibrationMode') or 0)
        except Exception:
            pass
        try:
            return int(self._opt('calibrationMode', 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _push(self):
        self._last_push = monotonic()
        try:
            self._rhapi.ui.socket_broadcast(STATE_EVENT, self._snapshot())
        except Exception:
            logger.exception('gate_calibration broadcast failed')

    def on_get(self, _data=None):
        try:
            self._rhapi.ui.socket_send(STATE_EVENT, self._snapshot())
        except Exception:
            pass

    def on_option_set(self, args):
        if (args or {}).get('option') == OPT_THEME:
            self._push()

    # -------------------------------------------------------- window control

    def on_start(self, data=None):
        secs = None
        try:
            secs = int((data or {}).get('secs'))
        except (TypeError, ValueError):
            pass
        if secs and secs > 0 and secs != self._opt_int(OPT_SECS, 30):
            # duration set from the panel becomes the new default
            try:
                self._rhapi.db.option_set(OPT_SECS, max(5, min(600, secs)))
            except Exception:
                pass
        seat = (data or {}).get('seat')
        try:
            seat = int(seat) if seat is not None else None
        except (TypeError, ValueError):
            seat = None
        self.start_window(secs, seat=seat)

    def on_stop(self, _data=None):
        if self._phase == 'running':
            self._stop = True

    def start_window(self, secs=None, seat=None):
        '''Open a calibration window for all enabled seats, or for a single
        seat when `seat` is given (per-pilot recalibration).'''
        if self._phase == 'running':
            self._notify('Gate Calibration: a window is already open')
            return
        ctx = self._ctx
        if ctx.race.race_status != RaceStatus.READY:
            self._notify('Gate Calibration: cannot calibrate while a race '
                         'is staged or running')
            return
        if secs is None or secs <= 0:
            secs = self._opt_int(OPT_SECS, 30)
        secs = max(5, min(600, secs))

        self._gen += 1
        self._stop = False
        self._seats = {}
        for node, pilot_id in self._active_seats(ctx):
            if seat is not None and node.index != seat:
                continue
            self._seats[node.index] = {
                'status': 'wait', 'pilot_id': pilot_id,
                'peak': None, 'floor': None, 'enter': None, 'exit': None,
                'applied_peak': None}
        if not self._seats:
            self._notify('Gate Calibration: no enabled seats (set frequencies '
                         'first)')
            return

        self._t_start = monotonic()
        self._t_end = self._t_start + secs
        self._phase = 'running'
        self._message = ''
        self._push()
        gevent.spawn(self._monitor, self._gen)
        if seat is not None:
            st = self._seats[seat]
            who = self._callsign(st['pilot_id']) or 'Seat {}'.format(seat + 1)
            self._notify('Gate Calibration: window open for {} s — {} only, '
                         'walk the powered-up quad through the gate'.format(
                             secs, who))
        else:
            self._notify('Gate Calibration: window open for {} s — walk your '
                         'powered-up quad through the gate'.format(secs))
        logger.info('gate_calibration window opened for %s s (%s seats)',
                    secs, len(self._seats))

    # --------------------------------------------------------------- monitor

    def _monitor(self, gen):
        ctx = self._ctx
        try:
            while gen == self._gen and not self._stop:
                now = monotonic()
                if now >= self._t_end:
                    break
                if ctx.race.race_status != RaceStatus.READY:
                    # a race got staged mid-window: abort, keep what was set
                    self._message = 'Race staged — window closed early'
                    break
                try:
                    if self._scan_all(ctx, now):
                        self._message = 'All seats calibrated'
                        break
                except Exception:
                    logger.exception('gate_calibration scan failed')
                if now - self._last_push >= PUSH_SECS:
                    self._push()
                gevent.sleep(POLL_SECS)
            # final sweep so a peak right at the buzzer still counts
            if gen == self._gen:
                try:
                    self._scan_all(ctx, self._t_end, closing=True)
                except Exception:
                    logger.exception('gate_calibration final scan failed')
                self._finalize(gen)
        except Exception:
            logger.exception('gate_calibration monitor crashed')
            if gen == self._gen:
                self._phase = 'idle'
                self._push()

    def _scan_all(self, ctx, now, closing=False):
        '''Scan every pending seat; returns True when all seats are set.'''
        min_rise = SENS_MIN_RISE.get(self._opt(OPT_SENS, 'normal'), 30)
        all_done = True
        for idx, st in self._seats.items():
            node = ctx.interface.nodes[idx]
            self._scan_seat(ctx, node, st, min_rise, now, closing)
            if st['status'] not in ('set', 'updated'):
                all_done = False
        return all_done

    def _scan_seat(self, ctx, node, st, min_rise, now, closing):
        # history is shared and pruned from the front while READY — filter by
        # timestamp, never by index
        n = min(len(node.history_values), len(node.history_times))
        if not n:
            return
        vals = []
        pre_floor = None
        for v, t in zip(node.history_values[:n], node.history_times[:n]):
            if t >= self._t_start:
                vals.append(v)
            elif t >= self._t_start - PRE_WINDOW_SECS \
                    and (pre_floor is None or v < pre_floor):
                pre_floor = v
        if len(vals) < MIN_SAMPLES:
            return
        floor = min(vals)
        peak = max(vals)
        rise = peak - floor
        if rise < min_rise:
            return
        cur = vals[-1]
        drop_req = max(10, int(0.35 * rise))
        # peak must be over (craft carried away from the timer) — or the
        # window is closing, in which case the best peak so far is used
        if peak - cur < drop_req and not closing:
            if st['status'] == 'wait':
                st['status'] = 'pass'
                st['peak'] = peak
                self._push()
            return
        if st['applied_peak'] is not None \
                and peak < st['applied_peak'] + REAPPLY_RISE:
            return  # nothing meaningfully better than what was applied
        # The window floor is biased high when the powered quad idles next to
        # the gate for the whole window — the quiet minute BEFORE the window
        # shows the real noise floor, and ExitAt must clear the real one.
        if pre_floor is not None and pre_floor < floor:
            floor = pre_floor
        self._apply(ctx, node, st, floor, peak)

    def _apply(self, ctx, node, st, floor, peak):
        '''Derive EnterAt/ExitAt from the observed pass and set them through
        RotorHazard's calibration path (transmits to the node and persists in
        the current profile). Follows doc/Tuning Parameters.md: EnterAt below
        the pass peak but well above the noise floor; ExitAt between them.
        A hand-carried pass reads systematically hotter than a flying one, so
        when the pilot has raced on this channel EnterAt is capped below the
        median of their real flying pass peaks.'''
        fly_med = self._fly_peak_median(st['pilot_id'], node.frequency)
        enter, exit_at = self._derive(peak, floor, fly_med)
        if enter is None:
            return

        idx = node.index
        ctx.calibration.set_enter_at_level(idx, enter)
        ctx.calibration.set_exit_at_level(idx, exit_at)
        try:
            ctx.rhui.emit_enter_and_exit_at_levels()
        except Exception:
            pass

        updated = st['applied_peak'] is not None
        st.update({'status': 'updated' if updated else 'set',
                   'peak': peak, 'floor': floor,
                   'enter': enter, 'exit': exit_at, 'applied_peak': peak})
        self._store_record(idx, st)
        self._push()
        callsign = self._callsign(st['pilot_id']) or 'Seat {}'.format(idx + 1)
        self._notify('Gate Calibration: {} {} — EnterAt {}, ExitAt {} '
                     '(peak {}, floor {})'.format(
                         callsign, 're-calibrated' if updated else 'calibrated',
                         enter, exit_at, peak, floor))
        logger.info('gate_calibration seat %s (%s): peak %s floor %s -> '
                    'EnterAt %s ExitAt %s', idx + 1, callsign, peak, floor,
                    enter, exit_at)

    def _derive(self, peak, floor, fly_med=None):
        '''EnterAt/ExitAt from a walk-through peak and the true noise floor,
        optionally capped by the pilot's median flying pass peak.
        Returns (enter, exit) or (None, None) when the numbers are unusable.'''
        margin = max(5, min(60, self._opt_int(OPT_MARGIN, DEFAULT_MARGIN))) / 100.0
        span = max(1, peak - floor)
        enter = peak - max(8, int(margin * span))
        if fly_med and fly_med > floor + 12:
            # racing data beats the walk estimate: real crossings must clear
            # EnterAt, and flying peaks sit below hand-carried ones
            enter = min(enter, fly_med - FLY_PEAK_GAP)
        enter = max(enter, floor + 12)
        enter = min(enter, peak - 5)
        exit_at = floor + max(6, int(EXIT_FRACTION * (enter - floor)))
        exit_at = min(exit_at, enter - 5)
        if enter <= floor or exit_at <= 0:
            return None, None
        return enter, exit_at

    def _fly_peak_median(self, pilot_id, frequency):
        '''Median peak RSSI of this pilot's real (timer-recorded) laps on this
        frequency across their saved races — the level actual flying passes
        reach, typically below a hand-carried walkthrough peak.'''
        if not pilot_id:
            return None
        try:
            rhdata = self._ctx.rhdata
            peaks = []
            for pr in (rhdata.get_savedPilotRaces() or []):
                if pr.pilot_id != pilot_id \
                        or int(pr.frequency or 0) != int(frequency or 0):
                    continue
                for lap in (rhdata.get_savedRaceLaps_by_savedPilotRace(pr.id)
                            or []):
                    if lap.deleted or lap.source not in TIMER_SOURCES:
                        continue
                    p = getattr(lap, 'peak_rssi', None)
                    if p:
                        peaks.append(p)
        except Exception:
            logger.exception('gate_calibration fly-peak lookup failed')
            return None
        if len(peaks) < 3:
            return None
        peaks.sort()
        n = len(peaks)
        m = n // 2
        return peaks[m] if n % 2 else (peaks[m - 1] + peaks[m]) // 2

    def _store_record(self, idx, st):
        freq = self._ctx.interface.nodes[idx].frequency
        key = self._record_key(st['pilot_id'], idx)
        recs = self._records()
        recs[key] = {
            'pilot_id': st['pilot_id'], 'seat': idx,
            'callsign': self._callsign(st['pilot_id']),
            'frequency': freq, 'chan': self._chan_label(idx, freq),
            'enter': st['enter'], 'exit': st['exit'],
            'peak': st['peak'], 'floor': st['floor'], 'ts': int(time()),
            'raced': False, 'ver': RECORD_VER}
        self._save_records(recs)
        # channel is now current again — allow future change notifications
        self._notified = {(k, f) for (k, f) in self._notified if k != key}

    def _finalize(self, gen):
        if gen != self._gen:
            return
        done = [st for st in self._seats.values()
                if st['status'] in ('set', 'updated')]
        missed = [idx for idx, st in self._seats.items()
                  if st['status'] not in ('set', 'updated')]
        for idx in missed:
            self._seats[idx]['status'] = 'nopass'
        self._phase = 'done'
        if not self._message:
            if self._stop:
                self._message = 'Stopped'
            elif missed:
                self._message = '{} of {} seats calibrated'.format(
                    len(done), len(self._seats))
            else:
                self._message = 'All seats calibrated'
        self._push()
        if missed:
            names = ', '.join(
                self._callsign(self._seats[i]['pilot_id'])
                or 'Seat {}'.format(i + 1) for i in sorted(missed))
            self._notify('Gate Calibration: no pass detected for {} — '
                         'thresholds unchanged'.format(names))
        logger.info('gate_calibration window closed: %s calibrated, %s missed',
                    len(done), len(missed))

    # -------------------------------------------------- channel-change watch

    def on_frequency_set(self, args):
        idx = (args or {}).get('nodeIndex')
        if idx is None or self._phase == 'running':
            return
        self._watch_check(only_idx=idx)

    def on_heat_set(self, _args=None):
        if self._phase == 'running':
            return
        # RotorHazard runs Adaptive Calibration inside set_heat BEFORE this
        # event fires, so this is the right moment to put fresh walk-through
        # values back on top of it.
        try:
            self._restore_walkthrough()
        except Exception:
            logger.exception('gate_calibration walkthrough restore failed')
        self._watch_check()

    def _restore_walkthrough(self):
        '''Adaptive Calibration just pulled EnterAt/ExitAt from the pilot's
        past races; when the pilot has a walk-through calibration on the same
        channel that they have not raced on yet, the walk-through is fresher —
        re-apply it. Once the pilot races on the channel, the race values (which
        started from the walk-through anyway, plus any operator adjustments)
        take priority again.'''
        if self._calibration_mode() != 1 \
                or not self._opt_bool(OPT_PRIORITY, True):
            return
        ctx = self._ctx
        race = ctx.race
        recs = self._records()
        restored = []
        for node in ctx.interface.nodes:
            idx = node.index
            freq = node.frequency
            if not freq:
                continue
            pilot_id = (race.node_pilots or {}).get(idx, RHUtils.PILOT_ID_NONE)
            rec = recs.get(self._record_key(pilot_id, idx))
            if not rec or rec.get('raced') \
                    or int(rec.get('frequency', 0)) != int(freq):
                continue
            enter, exit_at = rec.get('enter'), rec.get('exit')
            if not enter or not exit_at:
                continue
            if node.enter_at_level == enter and node.exit_at_level == exit_at:
                continue
            ctx.calibration.set_enter_at_level(idx, enter)
            ctx.calibration.set_exit_at_level(idx, exit_at)
            restored.append(self._callsign(pilot_id)
                            or 'Seat {}'.format(idx + 1))
        if restored:
            try:
                ctx.rhui.emit_enter_and_exit_at_levels()
            except Exception:
                pass
            self._notify('Gate Calibration: walk-through thresholds restored '
                         'over adaptive for {}'.format(', '.join(restored)))
            logger.info('gate_calibration: walkthrough restored over adaptive '
                        'for %s', ', '.join(restored))

    def on_laps_save(self, args):
        '''A race was saved: pilots who flew on their calibrated channel now
        have fresher data in the DB — from now on Adaptive Calibration wins
        for them (until the next walk-through).'''
        race_id = (args or {}).get('race_id')
        try:
            if race_id is not None:
                recs = self._records()
                changed = False
                runs = self._ctx.rhdata \
                    .get_savedPilotRaces_by_savedRaceMeta(race_id) or []
                for run in runs:
                    for key in (str(run.pilot_id),
                                'seat:{}'.format(run.node_index)):
                        rec = recs.get(key)
                        if rec and not rec.get('raced') \
                                and int(rec.get('frequency', 0)) == \
                                int(run.frequency or 0):
                            rec['raced'] = True
                            changed = True
                if changed:
                    self._save_records(recs)
        except Exception:
            logger.exception('gate_calibration laps-save marking failed')
        self._push()
        # LAPS_SAVE fires while race_status is still DONE; the reset to READY
        # happens right after in discard_laps(saved=True), which emits no
        # event — re-push shortly after so panels unlock without a reload
        gevent.spawn_later(1.0, self._push)

    def on_race_stage(self, _args=None):
        if self._phase != 'running':
            self._watch_check(staging=True)

    def on_race_state(self, _args=None):
        '''Race started/stopped/saved: re-broadcast so open panels collapse
        (race_active) or resume showing calibration freshness.'''
        self._push()

    def _watch_check(self, only_idx=None, staging=False):
        if not self._opt_bool(OPT_WATCH, True):
            return
        ctx = self._ctx
        race = ctx.race
        recs = self._records()
        for node in ctx.interface.nodes:
            idx = node.index
            if only_idx is not None and idx != only_idx:
                continue
            freq = node.frequency
            if not freq:
                continue
            pilot_id = (race.node_pilots or {}).get(idx, RHUtils.PILOT_ID_NONE)
            key = self._record_key(pilot_id, idx)
            rec = recs.get(key)
            callsign = self._callsign(pilot_id) or 'Seat {}'.format(idx + 1)
            if rec is None:
                if staging and pilot_id \
                        and self._opt_bool(OPT_WARN_UNCAL, False) \
                        and (key, 0) not in self._notified:
                    self._notified.add((key, 0))
                    self._notify('Gate Calibration: {} has no walk-through '
                                 'calibration yet'.format(callsign))
                continue
            if int(rec.get('frequency', 0)) == int(freq):
                continue
            mark = (key, int(freq))
            if mark in self._notified:
                continue
            self._notified.add(mark)
            old = rec.get('chan') or str(rec.get('frequency'))
            self._notify('Gate Calibration: {} channel changed since '
                         'calibration ({} → {}) — recalibration '
                         'recommended'.format(callsign, old,
                                              self._chan_label(idx, freq)),
                         interrupt=staging)
            logger.info('gate_calibration: %s channel changed %s -> %s, '
                        'recalibration recommended', callsign, old, freq)
        self._push()
