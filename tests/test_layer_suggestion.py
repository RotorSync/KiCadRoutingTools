"""Unit tests for the layer-suggestion seam (Phase B 3.3).

Covers the soft per-net layer-cost bias: leaving the plan's suggested layer
costs more (a tax), never a hard constraint, never an ordering change, and
inert when the knob is off or the net has no suggestion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))

from dataclasses import replace

import env_knobs
from routing_config import GridRouteConfig
from global_planner.layer_suggestion import (
    apply_layer_suggestion, planner_layer_prefs, _ACTIVE_PREFS,
)


def _cfg():
    return GridRouteConfig(
        track_width=0.2, clearance=0.1, via_size=0.3, via_drill=0.15,
        layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'],
        layer_costs=[1.0, 1.0, 1.0, 1.0])


def _set_knob(on):
    os.environ['KICAD_LAYER_SUGGEST'] = '1' if on else '0'
    env_knobs.refresh()


def test_off_is_inert():
    _set_knob(False)
    cfg = _cfg()
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'B.Cu'
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out is cfg


def test_taxes_non_preferred_layers():
    _set_knob(True)
    cfg = _cfg()
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'B.Cu'
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out is not cfg
    assert out.layer_costs == [1.3, 1.3, 1.3, 1.0]


def test_no_pref_is_inert():
    _set_knob(True)
    cfg = _cfg()
    _ACTIVE_PREFS.clear()
    out = apply_layer_suggestion(cfg, cfg, 99)
    assert out is cfg


def test_forbidden_layer_preserved():
    _set_knob(True)
    cfg = replace(_cfg(), layer_costs=[1.0, -1.0, 1.0, 1.0])
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'F.Cu'
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out.layer_costs == [1.0, -1.0, 1.3, 1.3]


def test_config_attr_preferred_over_module():
    _set_knob(True)
    cfg = _cfg()
    cfg._layer_suggestion_prefs = {5: 'In1.Cu'}
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'B.Cu'
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out.layer_costs == [1.3, 1.0, 1.3, 1.3]


def test_tax_one_is_inert():
    _set_knob(True)
    os.environ['KICAD_LAYER_SUGGEST_TAX'] = '1.0'
    env_knobs.refresh()
    cfg = _cfg()
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'B.Cu'
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out is cfg


def test_suggested_layer_not_in_stack_is_inert():
    _set_knob(True)
    cfg = GridRouteConfig(
        track_width=0.2, clearance=0.1, via_size=0.3, via_drill=0.15,
        layers=['F.Cu', 'B.Cu'], layer_costs=[1.0, 1.0])
    _ACTIVE_PREFS.clear()
    _ACTIVE_PREFS[5] = 'In1.Cu'  # not in this run's stack
    out = apply_layer_suggestion(cfg, cfg, 5)
    assert out is cfg
