"""Layer-suggestion seam: feed the plan's preferred layer into the detailed
router as a SOFT per-net layer-cost bias.

The Phase B multi-layer planner assigns each net a preferred layer (the one
with most free capacity at its pads / along its planned corridor). This module
surfaces that as a per-net preferred-layer map and applies it to the detailed
router as a per-net ``layer_costs`` multiplier: leaving the suggested layer
costs more (a soft tax), so a net vias out only when its corridor on the
suggested layer is genuinely exhausted.

Design constraints (the ordering experiments' failure mode):
  * NEVER a hard constraint -- nothing is forbidden, nothing gets cheaper.
  * NEVER an ordering change -- net order is untouched.
  * Soft by construction: the router still defects for real obstacle-cost
    reasons, so connectivity cannot regress from this bias alone.

The mechanism reuses the existing per-net ``layer_costs`` machinery (the
#589 plan_layer_config / #658 power_layer_config pattern): a ``replace`` clone
of the routing config with a per-net layer-cost multiplier list, consumed by
GridRouter.layer_costs. No Rust change, no search-region change.

Env knobs (all default OFF):
  KICAD_LAYER_SUGGEST=1        enable the bias
  KICAD_LAYER_SUGGEST_TAX      multiplier on NON-preferred layers (default 1.3)
  KICAD_LAYER_SUGGEST_DEBUG=1  print per-net application
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Dict, List, Optional

import env_knobs

# Module-level fallback for the ad-hoc config._layer_suggestion_prefs attribute:
# replace() clones of GridRouteConfig drop ad-hoc attributes (the #658
# _ACTIVE_PLAN precedent), so nested sub-runs (reconcile laps) that build a
# fresh config would lose the map. Set by planner_layer_prefs; read as a
# fallback in apply_layer_suggestion.
_ACTIVE_PREFS: Dict[int, str] = {}


def _tax() -> float:
    try:
        return float(env_knobs.LAYER_SUGGEST_TAX)
    except (AttributeError, ValueError):
        return 1.3


def _debug() -> bool:
    return bool(getattr(env_knobs, 'LAYER_SUGGEST_DEBUG', False))


def planner_layer_prefs(pcb_data,
                        trace_width: float,
                        clearance: float,
                        via_size: float = 0.3,
                        fixed_cost: float = 50.0,
                        congestion_threshold: int = 2) -> Dict[int, str]:
    """Run the Phase B multi-layer planner and return each net's preferred
    layer (its planned path's majority layer).

    Returns {net_id: layer_name} for every net the planner routed. Nets the
    planner could not route get no entry (the bias simply does not apply).
    """
    from .ordering import build_graphs_from_pcb
    from .multi_layer_planner import plan_board_multi
    graphs = build_graphs_from_pcb(pcb_data, trace_width, clearance)
    res = plan_board_multi(pcb_data, graphs, trace_width, clearance,
                           via_size=via_size, fixed_cost=fixed_cost,
                           congestion_threshold=congestion_threshold,
                           fast=True)
    prefs: Dict[int, str] = {}
    for n in res.nets:
        layers = [l for l, _ in n.path]
        if layers:
            prefs[n.net_id] = Counter(layers).most_common(1)[0][0]
    global _ACTIVE_PREFS
    _ACTIVE_PREFS = dict(prefs)
    return prefs


def ground_truth_layer_prefs(routed_board_path: str) -> Dict[int, str]:
    """Load the per-net majority layer from an EXISTING routed board.

    This is the CONTROL arm: instead of the plan's predicted layer, bias the
    router toward the layer each net ACTUALLY routed on in a reference routed
    board. Separates "layer bias as a mechanism does not help" from
    "the mechanism works but the plan's layer picks are bad".
    """
    from kicad_parser import parse_kicad_pcb
    from collections import defaultdict
    pcb = parse_kicad_pcb(routed_board_path)
    seglayer = defaultdict(Counter)
    for s in pcb.segments:
        seglayer[s.net_id][s.layer] += 1
    prefs = {nid: c.most_common(1)[0][0] for nid, c in seglayer.items()}
    global _ACTIVE_PREFS
    _ACTIVE_PREFS = dict(prefs)
    return prefs


def apply_layer_suggestion(cfg_route, config, net_id: int):
    """Apply the per-net soft layer-suggestion bias to a routing config clone.

    Taxes every layer EXCEPT the net's suggested layer by the tax multiplier
    (default 1.3 = 30% more expensive to leave it). The suggested layer keeps
    its base cost (1.0 unless the caller already set something else). Soft by
    construction: nothing is forbidden, nothing gets cheaper, and the router
    still defects when the suggested layer's corridor is genuinely exhausted.

    Returns cfg_route unchanged when the knob is off or the net has no
    suggestion -- callers may wire it unconditionally (the SE loop does).
    """
    if not getattr(env_knobs, 'LAYER_SUGGEST', False):
        return cfg_route
    prefs = (getattr(config, '_layer_suggestion_prefs', None)
             or _ACTIVE_PREFS)
    if not prefs:
        return cfg_route
    pref_layer = prefs.get(net_id)
    if pref_layer is None:
        return cfg_route
    try:
        li = cfg_route.layers.index(pref_layer)
    except ValueError:
        return cfg_route  # suggested layer not in this run's stack -- inert
    tax = _tax()
    if tax == 1.0:
        return cfg_route
    base = list(cfg_route.layer_costs or []) or [1.0] * len(cfg_route.layers)
    while len(base) < len(cfg_route.layers):
        base.append(1.0)
    out = []
    for i in range(len(base)):
        if i == li or base[i] < 0:
            out.append(base[i])  # suggested layer / forbidden: unchanged
        else:
            out.append(base[i] * tax)
    if _debug():
        print(f"    [layer-suggest] net {net_id}: prefer {pref_layer} "
              f"(tax {tax}x on other layers)")
    return replace(cfg_route, layer_costs=out)
