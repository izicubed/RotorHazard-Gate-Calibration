'''
Gate Walkthrough Calibration plugin for RotorHazard.

Open a timed calibration window (default 30 s); pilots walk their powered-up
quads through the gate over the timer; each seat's EnterAt/ExitAt is derived
from the observed pass peak and applied via RotorHazard's calibration path.
Calibrations are remembered per pilot with the channel they were made on, and
a recalibration is recommended whenever a pilot's channel changes.
'''

from flask import Blueprint

from eventmanager import Evt
from .calibrator import GateCalibrator, PLUGIN_ID, EV_GET, EV_START, EV_STOP


def initialize(rhapi):
    calibrator = GateCalibrator(rhapi)

    bp = Blueprint(PLUGIN_ID, __name__, static_folder='static',
                   static_url_path='/gate_calibration/static')
    rhapi.ui.blueprint_add(bp)

    rhapi.events.on(Evt.STARTUP, calibrator.register_ui,
                    name='gate_cal_ui')
    # channel-change watch: recommend recalibration when a calibrated pilot
    # ends up on a different frequency
    rhapi.events.on(Evt.FREQUENCY_SET, calibrator.on_frequency_set,
                    name='gate_cal_freq')
    rhapi.events.on(Evt.HEAT_SET, calibrator.on_heat_set,
                    name='gate_cal_heat')
    rhapi.events.on(Evt.RACE_STAGE, calibrator.on_race_stage,
                    name='gate_cal_stage')
    # keep browser panels in sync with the race state (collapse during races)
    rhapi.events.on(Evt.RACE_START, calibrator.on_race_state,
                    name='gate_cal_race_start')
    rhapi.events.on(Evt.RACE_STOP, calibrator.on_race_state,
                    name='gate_cal_race_stop')
    rhapi.events.on(Evt.LAPS_SAVE, calibrator.on_laps_save,
                    name='gate_cal_laps_save')
    rhapi.events.on(Evt.LAPS_DISCARD, calibrator.on_race_state,
                    name='gate_cal_laps_discard')
    rhapi.events.on(Evt.OPTION_SET, calibrator.on_option_set,
                    name='gate_cal_theme')

    rhapi.ui.socket_listen(EV_GET, calibrator.on_get)
    rhapi.ui.socket_listen(EV_START, calibrator.on_start)
    rhapi.ui.socket_listen(EV_STOP, calibrator.on_stop)
