"""Phase B Task 2: plan-informed net ordering for the detailed router.

The global plan assigns each net a corridor through the multi-layer capacity
graph. Nets whose corridors pass through the most-congested / most-constrained
regions are routed FIRST by the detailed router, so scarce lanes are claimed
early (the #472 / bus-corridor doctrine). Within a contention tier, nets are
ordered by net_id for determinism.

This is a thin adapter: it reads PCBData, runs the multi-layer planner, and
returns an ordered list of (net_name, net_id). It does NOT change any search
region or routing behavior -- only the order in which nets are attempted.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .capacity_graph import build_capacity_graph
from .multi_layer_planner import plan_board_multi


def _keepout_polys_for_layer(board_info, layer):
    polys = []
    for k in board_info.keepouts:
        if layer in k.get("layers", set()):
            if k.get("tracks_allowed", True):
                continue
            polys.append(k["polygon"])
    return polys


def build_graphs_from_pcb(pcb_data, trace_width: float,
                          clearance: float) -> Dict[str, object]:
    """Build per-copper-layer capacity graphs from parsed PCBData."""
    bi = pcb_data.board_info
    bounds = bi.board_bounds
    graphs = {}
    for layer in bi.copper_layers:
        pads_on_layer = []
        for plist in pcb_data.pads_by_net.values():
            for p in plist:
                if layer in p.layers:
                    pads_on_layer.append(p)
        kpolys = _keepout_polys_for_layer(bi, layer)
        g = build_capacity_graph(layer, pads_on_layer, kpolys, bounds,
                                 trace_width, clearance)
        graphs[layer] = g
    return graphs


def _net_congestion_score(res, net):
    """Max occupancy/capacity ratio along a planned net's corridor."""
    best = 0.0
    for (layer, node) in net.path:
        cap = res.capacity.get(layer, {}).get(node, 0)
        if cap <= 0:
            continue
        occ = res.occupancy.get(layer, {}).get(node, 0)
        ratio = occ / cap
        if ratio > best:
            best = ratio
    return best


def planner_net_order(pcb_data,
                      trace_width: float,
                      clearance: float,
                      via_size: float = 0.3,
                      fixed_cost: float = 50.0,
                      congestion_threshold: int = 2) -> List[Tuple[str, int]]:
    """Return nets ordered by planned-corridor congestion (most-congested first).

    Args:
        pcb_data: parsed PCBData
        trace_width / clearance / via_size: routing geometry (mm)
        fixed_cost: via fixed cost used by the planner
        congestion_threshold: planner layer-choice threshold

    Returns:
        List of (net_name, net_id) ordered most-congested-corridor first,
        tie-broken by net_id for determinism.
    """
    graphs = build_graphs_from_pcb(pcb_data, trace_width, clearance)
    # fast=True: plan each multi-pin net as a single 2-pin connection between
    # its two most-distant pads -- a cheap congestion proxy sufficient for
    # ORDERING (which nets are most contended), far cheaper than the full MST.
    res = plan_board_multi(pcb_data, graphs, trace_width, clearance,
                           via_size=via_size, fixed_cost=fixed_cost,
                           congestion_threshold=congestion_threshold,
                           fast=True)

    # Map net_id -> planned NetPlan (only nets that got routed appear).
    plan_by_id = {n.net_id: n for n in res.nets}

    # Build from all nets present in pcb_data so we cover every candidate.
    all_nets = []
    for net_id in sorted(pcb_data.nets.keys()):
        name = pcb_data.nets[net_id].name
        plan = plan_by_id.get(net_id)
        if plan is not None:
            score = _net_congestion_score(res, plan)
        else:
            score = 0.0
        all_nets.append((score, net_id, name))

    # Sort by score descending (most congested first), then net_id ascending.
    all_nets.sort(key=lambda t: (-t[0], t[1]))
    return [(name, net_id) for (_score, net_id, name) in all_nets]
