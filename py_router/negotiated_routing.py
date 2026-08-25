"""Parallel negotiated-congestion routing pre-pass (v2, PathFinder-style).

Runs BEFORE the sequential Phase-3 tap loop. All unresolved multipoint nets
route their next pending MST edge IN PARALLEL (rayon, inside one FFI call)
against a SHARED FROZEN cost map:

    base cost  = working obstacles (all committed copper as hard blocks)
               + stub/track proximity soft costs
    + present-congestion cost  = cells claimed by >1 net THIS iteration
    + history cost             = accumulates each iteration a cell stays
                                 over-subscribed

Costs are FROZEN during an iteration and only updated between iterations, so
each request's result is a PURE FUNCTION of (request + frozen map) -- rayon
scheduling cannot change the output (determinism, verified by the Rust unit
tests 1-thread vs N-thread).

Resolution rule (guarantees no DRC violations): after routing, count per-cell
claims across ALL returned paths. A cell is over-subscribed if claimed by >1
net. An edge is RESOLVED only if its path contains ZERO over-subscribed cells;
its copper is then committed through the SAME safety pipeline as sequential
Phase-3 (path->segments/vias, necking, terminal-graze short gate). Edges that
share cells with other edges stay unresolved and re-route next iteration with
higher congestion/history cost on the contested cells. After the iteration cap
(~8) or when no progress is made, unresolved nets fall through to the
sequential Phase-3 loop unchanged -- a guaranteed no-worse fallback.

The shared map keeps every net's committed copper as hard obstacles; each rayon
worker clones it and removes ONLY its own net's committed copper (so it can tap
into its own trunk), via the request's remove_blocked_cells/vias fields.
"""

import time

import numpy as np

from grid_router import NegotiatedRequest, route_negotiated

# Iteration cap for the negotiated pre-pass.
MAX_ITERATIONS = 8
# Congestion cost per over-subscribed claim (grid cost units).
CONGESTION_COST = 2000
# History cost added per iteration a cell stays over-subscribed.
HISTORY_COST = 1000


def _path_cells(path):
    """Yield (gx, gy, layer) cells along a routed path (dedup consecutive)."""
    prev = None
    for p in path:
        if p != prev:
            yield p
            prev = p


def _count_claims(paths):
    """Count per-cell claims across all paths. Returns dict cell->count."""
    claims = {}
    for path in paths:
        if not path:
            continue
        for cell in _path_cells(path):
            claims[cell] = claims.get(cell, 0) + 1
    return claims


def _edge_is_resolved(path, claims):
    """An edge is resolved iff its path has zero over-subscribed cells."""
    if not path:
        return False
    return all(claims.get(cell, 0) <= 1 for cell in _path_cells(path))


def _segments_vias_cells(segments, vias, config, coord, layer_names):
    """Grid cells (gx, gy, layer) + via cells (gx, gy) for a list of segments
    and vias -- the cells a net's own copper occupies, used to remove it from a
    worker's clone so the net can route through its own trunk. Uses the same
    capsule geometry as the obstacle cache (segment_blocked_cells_array), so the
    removal matches what the shared map stamped."""
    from routing_utils import segment_blocked_cells_array, circle_offsets
    layer_map = {name: i for i, name in enumerate(layer_names)}
    cells = []
    via_cells = []
    for seg in segments:
        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue
        margin = config.track_width / 2 + config.obstacle_clearance(seg.net_id)
        arr = segment_blocked_cells_array(
            seg.start_x, seg.start_y, seg.end_x, seg.end_y, margin, coord.grid_step)
        for gx, gy in arr:
            cells.append((int(gx), int(gy), layer_idx))
    for via in vias:
        gx, gy = coord.to_grid(via.x, via.y)
        radius_mm = config.via_size / 2 + config.clearance
        rng = int(radius_mm / coord.grid_step) + 1
        eff_sq = (radius_mm / coord.grid_step) ** 2
        offs = circle_offsets(rng, eff_sq)
        for ox, oy in offs:
            via_cells.append((int(gx + ox), int(gy + oy)))
    return cells, via_cells


def run_negotiated_phase3_prepass(
    state,
    pcb_data,
    config,
    base_obstacles,
    all_unrouted_net_ids,
    routed_net_ids,
    remaining_net_ids,
    routed_results,
    results,
    track_proximity_cache,
    layer_map,
    net_obstacles_cache,
    progress_callback=None,
    cancel_check=None,
):
    """Best-effort parallel negotiated pre-pass over pending multipoint nets.

    Returns (resolved_net_ids, stats_dict). Nets NOT resolved here fall through
    to the sequential Phase-3 loop unchanged. Only commits copper whose path has
    no over-subscribed cells, so it can never create a DRC violation.
    """
    from single_ended_routing import (
        get_all_segment_tap_points, _path_to_segments_vias,
        _pad_all_layer_reach, _drop_segments_already_present,
        _neck_route_terminal_grazes,
    )
    from obstacle_cache import (
        remove_net_obstacles_from_cache, update_net_obstacles_after_routing,
        add_net_obstacles_from_cache,
    )
    from pcb_modification import add_route_to_pcb_data
    from phase3_routing import _commit_net_result
    from routing_config import GridCoord

    pending = state.pending_multipoint_nets
    if not pending:
        return [], {}

    coord = GridCoord(config.grid_step)
    layer_names = config.layers

    # ---- Build the shared frozen base map once (all committed copper as hard
    # ---- obstacles + soft proximity costs). We use the incremental builder on
    # ---- a clone of the working map so stub/track proximity are included.
    shared = base_obstacles.clone_fresh()
    # The working map already carries every net's committed copper; clone it so
    # we get the full obstacle set plus soft costs.
    if state.working_obstacles is not None:
        shared = state.working_obstacles.clone_fresh()
        # Add stub/track proximity for the still-unrouted nets (mirrors the
        # per-net incremental builder, but once for the shared map).
        from routing_context import build_incremental_obstacles as _binc
        # We can't reuse build_incremental_obstacles directly (it removes one
        # net's obstacles); instead stamp the soft costs onto the clone.
        from obstacle_costs import apply_stub_proximity, merge_track_proximity_costs, add_cross_layer_tracks
        from connectivity import get_stub_endpoints
        from net_queries import get_chip_pad_positions
        stub_proximity_net_ids = [nid for nid in all_unrouted_net_ids
                                  if nid not in routed_net_ids]
        unrouted_stubs = get_stub_endpoints(pcb_data, stub_proximity_net_ids)
        chip_pads = get_chip_pad_positions(pcb_data, stub_proximity_net_ids)
        all_stubs = unrouted_stubs + chip_pads
        apply_stub_proximity(shared, pcb_data, stub_proximity_net_ids,
                             all_stubs, config, layer_map=layer_map)
        merge_track_proximity_costs(shared, track_proximity_cache, config=config)
        add_cross_layer_tracks(shared, pcb_data, config, layer_map)

    # Per-net committed copper cells (for the worker to remove its own).
    net_remove_cells = {}
    net_remove_vias = {}
    for net_id in pending:
        if net_id in net_obstacles_cache:
            nd = net_obstacles_cache[net_id]
            net_remove_cells[net_id] = [tuple(map(int, r)) for r in nd.blocked_cells]
            net_remove_vias[net_id] = [tuple(map(int, r)) for r in nd.blocked_vias]
        else:
            net_remove_cells[net_id] = []
            net_remove_vias[net_id] = []

    # History cost map: cell -> accumulated over-subscription count.
    history = {}

    # Per-net pending-edge state: we advance each net one edge per iteration.
    # net_state[net_id] = dict(main_result, remaining_edges, routed_indices,
    #                          routed_components, all_segments, all_vias,
    #                          pad_info, pad_components)
    net_state = {}
    for net_id, main_result in pending.items():
        pad_info = main_result['multipoint_pad_info']
        routed_indices = set(main_result['routed_pad_indices'])
        pad_components = main_result.get('pad_components', {i: i for i in range(len(pad_info))})
        routed_components = {pad_components.get(idx, idx) for idx in routed_indices}
        mst_edges = main_result.get('mst_edges', [])
        remaining_edges = list(mst_edges[1:] if len(mst_edges) > 1 else [])
        all_segments = list(main_result['new_segments'])
        all_vias = list(main_result.get('new_vias', []))
        net_state[net_id] = {
            'main_result': main_result,
            'pad_info': pad_info,
            'routed_indices': routed_indices,
            'routed_components': routed_components,
            'pad_components': pad_components,
            'remaining_edges': remaining_edges,
            'all_segments': all_segments,
            'all_vias': all_vias,
            'through_hole_positions': set(),
            'tap_point_map': {},
        }

    resolved_nets = []
    stats = {'iterations': 0, 'edges_routed': 0, 'edges_failed': 0,
             'nets_resolved': 0, 'total_time': 0.0}

    start_time = time.time()

    for iteration in range(MAX_ITERATIONS):
        if cancel_check and cancel_check():
            break

        # ---- Build this iteration's requests: each unresolved net's next edge.
        requests = []
        request_nets = []  # parallel to requests: net_id
        request_edge_keys = []
        request_sources = []
        request_targets = []
        request_tap_maps = []
        request_seg_lists = []
        request_via_lists = []
        request_th_positions = []

        for net_id, ns in net_state.items():
            if net_id in resolved_nets:
                continue
            if not ns['remaining_edges']:
                resolved_nets.append(net_id)
                continue

            # Find the next edge connecting a routed pad to an unrouted pad.
            edge_to_route = None
            for edge in ns['remaining_edges']:
                idx_a, idx_b, length = edge
                a_routed = (idx_a in ns['routed_indices']
                            or ns['pad_components'].get(idx_a, idx_a) in ns['routed_components'])
                b_routed = (idx_b in ns['routed_indices']
                            or ns['pad_components'].get(idx_b, idx_b) in ns['routed_components'])
                if a_routed and not b_routed:
                    edge_to_route = (idx_a, idx_b)
                    break
                elif b_routed and not a_routed:
                    edge_to_route = (idx_b, idx_a)
                    break

            if edge_to_route is None:
                # No eligible edge (orphaned subtree) -- leave to sequential.
                continue

            src_idx, tgt_idx = edge_to_route
            src_pad = ns['pad_info'][src_idx]
            tgt_pad = ns['pad_info'][tgt_idx]

            # Sources: all tap points of the net's committed copper.
            all_tap_points = get_all_segment_tap_points(
                ns['all_segments'], coord, layer_names, vias=ns['all_vias'])
            sources = [(gx, gy, layer_idx) for gx, gy, layer_idx, _, _ in all_tap_points]
            tap_point_map = {(gx, gy, layer_idx): (ox, oy)
                             for gx, gy, layer_idx, ox, oy in all_tap_points}

            src_x, src_y = src_pad[3], src_pad[4]
            src_gx, src_gy = coord.to_grid(src_x, src_y)
            src_pad_obj = src_pad[5] if len(src_pad) > 5 else None
            if _pad_all_layer_reach(pcb_data, src_pad_obj):
                for layer_idx in range(len(layer_names)):
                    key = (src_gx, src_gy, layer_idx)
                    if key not in tap_point_map:
                        sources.append(key)
                        tap_point_map[key] = (src_x, src_y)
            else:
                key = (src_gx, src_gy, src_pad[2])
                if key not in tap_point_map:
                    sources.append(key)
                    tap_point_map[key] = (src_x, src_y)

            if not sources:
                continue

            # Targets: target pad on all layers (through-hole) or its layer.
            tgt_gx, tgt_gy = tgt_pad[0], tgt_pad[1]
            tgt_pad_obj = tgt_pad[5] if len(tgt_pad) > 5 else None
            if _pad_all_layer_reach(pcb_data, tgt_pad_obj):
                targets = [(tgt_gx, tgt_gy, layer_idx) for layer_idx in range(len(layer_names))]
            else:
                targets = [(tgt_gx, tgt_gy, tgt_pad[2])]

            # Endpoint overrides.
            source_target_cells = list(sources) + list(targets)
            allowed_cells = []
            allow_radius = 5
            for dx in range(-allow_radius, allow_radius + 1):
                for dy in range(-allow_radius, allow_radius + 1):
                    allowed_cells.append((tgt_gx + dx, tgt_gy + dy))

            requests.append(NegotiatedRequest(
                sources=sources,
                targets=targets,
                max_iterations=config.max_iterations,
                collinear_vias=False,
                via_exclusion_radius=0,
                direction_steps=2,
                track_margin=config.track_margins_for_net(net_id),
                max_iterations_ceiling=config.max_iterations,
                quantum_cells=2.0,
                quantum_pct=2.0,
                grace_tranches=0,
                via_rung=0,
                via_cost=config.via_cost_units(),
                h_weight=config.heuristic_weight,
                turn_cost=config.turn_cost,
                via_proximity_cost=config.via_proximity_cost_int(),
                vertical_attraction_radius=coord.to_grid_dist(config.vertical_attraction_radius) if config.vertical_attraction_radius > 0 else 0,
                vertical_attraction_bonus=config.cell_cost(config.vertical_attraction_cost) if config.vertical_attraction_cost > 0 else 0,
                layer_costs=config.get_layer_costs(),
                proximity_heuristic_cost=0,
                layer_direction_preferences=config.get_layer_direction_preferences(),
                direction_preference_cost=config.direction_preference_cost,
                attraction_path=[],
                source_target_cells=source_target_cells,
                allowed_cells=allowed_cells,
                endpoint_exempt_positions=[(t[0], t[1]) for t in targets[:8]],
                endpoint_exempt_radius=coord.to_grid_dist(config.track_width + config.clearance),
                free_via_positions=list(ns['through_hole_positions']),
                remove_blocked_cells=net_remove_cells.get(net_id, []),
                remove_blocked_vias=net_remove_vias.get(net_id, []),
            ))
            request_nets.append(net_id)
            request_edge_keys.append((src_idx, tgt_idx))
            request_sources.append(sources)
            request_targets.append(targets)
            request_tap_maps.append(tap_point_map)
            request_seg_lists.append(ns['all_segments'])
            request_via_lists.append(ns['all_vias'])
            request_th_positions.append(ns['through_hole_positions'])

        if not requests:
            break

        # ---- Inject congestion/history costs into the shared map's layer
        # ---- proximity costs (frozen for this iteration).
        shared.clear_layer_proximity()
        cong_rows = []
        for cell, cnt in history.items():
            gx, gy, layer = cell
            cong_rows.append((layer, gx, gy, HISTORY_COST * cnt))
        if cong_rows:
            shared.set_layer_proximity_batch(np.array(cong_rows, dtype=np.int32))

        # ---- Route all requests in parallel against the frozen shared map.
        results_batch = route_negotiated(requests, shared)

        # ---- Count claims across all returned paths.
        paths = [r[0] for r in results_batch]
        claims = _count_claims(paths)

        # ---- Resolve edges whose paths have no over-subscribed cells.
        newly_resolved_nets = set()
        for i, net_id in enumerate(request_nets):
            path = paths[i]
            if not path:
                continue
            if not _edge_is_resolved(path, claims):
                continue

            ns = net_state[net_id]
            src_idx, tgt_idx = request_edge_keys[i]
            tap_point_map = request_tap_maps[i]

            # Convert path to segments/vias through the same safety pipeline.
            try:
                start_key = path[0]
                end_key = path[-1]
                start_orig = tap_point_map.get(start_key)
                end_key_tgt = request_targets[i][0]
                end_orig = tap_point_map.get(end_key) or (
                    ns['pad_info'][tgt_idx][3], ns['pad_info'][tgt_idx][4])
                segments, vias = _path_to_segments_vias(
                    path, coord, layer_names, net_id, config,
                    start_original=(start_orig[0], start_orig[1], layer_names[start_key[2]]) if start_orig else None,
                    end_original=(end_orig[0], end_orig[1], layer_names[end_key[2]]) if end_key else None,
                    through_hole_positions=ns['through_hole_positions'],
                    pcb_data=pcb_data)
            except Exception as e:
                print(f"  [negotiated] {pcb_data.nets[net_id].name}: convert failed: {e}")
                continue

            if not segments and not vias:
                continue

            # Terminal-graze short gate: fail edges whose terminal copper would
            # overlap a foreign track/via.
            try:
                _hard_tap = _neck_route_terminal_grazes(
                    segments, path, coord,
                    (ns['pad_info'][src_idx][3], ns['pad_info'][src_idx][4]),
                    (ns['pad_info'][tgt_idx][3], ns['pad_info'][tgt_idx][4]),
                    pcb_data, net_id, config)
                if _hard_tap:
                    print(f"  [negotiated] {pcb_data.nets[net_id].name}: "
                          f"terminal short gate -- leaving to sequential")
                    continue
            except Exception:
                pass

            # Drop segments already present (avoid duplicate coincident copper).
            segments = _drop_segments_already_present(segments, ns['all_segments'])

            # Commit to this net's state.
            ns['all_segments'].extend(segments)
            ns['all_vias'].extend(vias)
            for _v in vias:
                ns['through_hole_positions'].add(coord.to_grid(_v.x, _v.y))
            # Track this edge's cells so LATER iterations of THIS net can route
            # through its own just-committed tap copper (the shared map keeps it
            # as a hard obstacle; the worker must remove it).
            _ec, _ev = _segments_vias_cells(segments, vias, config, coord, layer_names)
            net_remove_cells.setdefault(net_id, []).extend(_ec)
            net_remove_vias.setdefault(net_id, []).extend(_ev)
            # CRITICAL: add this committed edge to the SHARED map as a hard
            # obstacle so OTHER nets' later iterations cannot route through it
            # (the shared map is a clone; commits to working_obstacles do not
            # propagate to it). Without this, iteration-2+ edges of other nets
            # overlap this edge -> DRC violations.
            from obstacle_map import add_segments_list_as_obstacles, add_vias_list_as_obstacles
            add_segments_list_as_obstacles(shared, segments, config)
            add_vias_list_as_obstacles(shared, vias, config)
            ns['routed_indices'].add(tgt_idx)
            tgt_component = ns['pad_components'].get(tgt_idx, tgt_idx)
            ns['routed_components'].add(tgt_component)
            ns['remaining_edges'] = [e for e in ns['remaining_edges']
                                     if not ((e[0] == src_idx and e[1] == tgt_idx)
                                             or (e[0] == tgt_idx and e[1] == src_idx))]
            stats['edges_routed'] += 1

            # If all pads connected -> net fully resolved.
            if len(ns['routed_indices']) == len(ns['pad_info']):
                newly_resolved_nets.add(net_id)

        # ---- Update history: accumulate over-subscription.
        for cell, cnt in claims.items():
            if cnt > 1:
                history[cell] = history.get(cell, 0) + 1

        resolved_nets.extend(newly_resolved_nets)
        stats['iterations'] += 1

        # Progress check: if no edge was routed this iteration and nothing was
        # resolved, further iterations won't help -- stop.
        if stats['edges_routed'] == 0 and not newly_resolved_nets:
            break

    # ---- Commit fully-resolved nets to pcb_data + working obstacles.
    committed_nets = []
    for net_id in resolved_nets:
        ns = net_state.get(net_id)
        if not ns:
            continue
        main_result = ns['main_result']
        completed_result = dict(main_result)
        completed_result['new_segments'] = ns['all_segments']
        completed_result['new_vias'] = ns['all_vias']
        completed_result['routed_pad_indices'] = ns['routed_indices']
        completed_result['tap_edges_routed'] = main_result.get('tap_edges_routed', 0) + stats['edges_routed']
        completed_result['tap_edges_failed'] = main_result.get('tap_edges_failed', 0)

        lm_segments = main_result['new_segments']
        lm_vias = main_result.get('new_vias', [])
        tap_segments = completed_result['new_segments'][len(lm_segments):]
        tap_vias = completed_result['new_vias'][len(lm_vias):]

        if tap_segments or tap_vias:
            tap_result = {'new_segments': tap_segments, 'new_vias': tap_vias}
            add_route_to_pcb_data(pcb_data, tap_result, debug_lines=config.debug_lines)
            completed_result['new_segments'] = list(lm_segments) + tap_result['new_segments']
            completed_result['new_vias'] = list(lm_vias) + tap_result['new_vias']

            if state.working_obstacles is not None and net_obstacles_cache is not None:
                if net_id in net_obstacles_cache:
                    remove_net_obstacles_from_cache(state.working_obstacles,
                                                    net_obstacles_cache[net_id])
                update_net_obstacles_after_routing(pcb_data, net_id, completed_result,
                                                   config, net_obstacles_cache)
                add_net_obstacles_from_cache(state.working_obstacles,
                                             net_obstacles_cache[net_id])

        if main_result in results:
            results.remove(main_result)
        _commit_net_result(results, routed_results, net_id, completed_result,
                           pcb_data, config)

        # Remove from pending so sequential Phase-3 skips it.
        state.pending_multipoint_nets.pop(net_id, None)
        # Keep routed/remaining bookkeeping consistent so the sequential loop's
        # obstacle building excludes this net's stubs from proximity.
        if net_id not in routed_net_ids:
            routed_net_ids.append(net_id)
        if net_id in remaining_net_ids:
            remaining_net_ids.remove(net_id)
        committed_nets.append(net_id)
        stats['nets_resolved'] += 1

    stats['total_time'] = time.time() - start_time
    return committed_nets, stats
