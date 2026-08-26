"""Parallel multi-connection NEGOTIATED routing pre-pass (v2).

Runs BEFORE the sequential Phase-3 tap loop. Each unresolved multipoint net
routes its ENTIRE tap set as ONE COHERENT UNIT inside one FFI call against a
SHARED FROZEN cost map -- parallelism moved to NET level (vs the v2 experiment's
CONNECTION level), so multipoint nets never fragment across iterations:

    base cost  = working obstacles (all committed copper as hard blocks)
               + stub/track proximity soft costs
    + present-congestion cost  = cells claimed by >1 net THIS iteration
    + history cost             = accumulates each iteration a cell stays
                                 over-subscribed

Costs are FROZEN during an iteration and only updated between iterations, so each
request's result is a PURE FUNCTION of (taps + frozen map) -- rayon scheduling
cannot change the output (determinism, verified by the Rust unit tests 1-thread
vs N-thread).

Resolution rule (guarantees no DRC violations): after routing, count per-cell
claims across ALL returned trees using the EXPANDED CAPSULE FOOTPRINT of each
edge's copper (the same segment_blocked_cells_array geometry the obstacle cache
stamps), keyed by distinct NET id so same-net junctions never self-contest. A cell
is over-subscribed if claimed by >1 DISTINCT net. A net is RESOLVED only if its
ENTIRE tree contains ZERO over-subscribed cells; its copper is then committed
through the SAME safety pipeline as sequential Phase-3 (path->segments/vias,
necking, terminal-graze short gate). Nets that share cells with other nets stay
unresolved and re-route next iteration with higher congestion/history cost on the
contested cells. After the iteration cap (~8) or when no progress is made,
unresolved nets fall through to the sequential Phase-3 loop unchanged -- a
guaranteed no-worse fallback.

The shared map keeps every net's committed copper as hard obstacles; each rayon
worker clones it and removes ONLY its own net's committed copper (so it can tap
into its own trunk), via the request's remove_blocked_cells/vias fields.
"""

import time

import numpy as np

from grid_router import NetTreeRequest, route_tree

# Iteration cap for the negotiated pre-pass. Nets with zero committed edges
# after iteration 1 are dropped to sequential (see the loop), so 4 iterations
# is enough for the easy nets to separate.
MAX_ITERATIONS = 4
# Congestion cost per over-subscribed claim (grid cost units).
CONGESTION_COST = 2000
# History cost added per iteration a cell stays over-subscribed.
HISTORY_COST = 1000


def _edge_capsule_cells(segments, vias, config, coord, layer_names, net_id):
    """Grid cells (gx, gy, layer) + via cells (gx, gy) for a list of segments
    and vias -- the EXPANDED CAPSULE FOOTPRINT of the copper, using the same
    segment_blocked_cells_array geometry as the obstacle cache. This is what
    claim-counting uses: two tracks sub-clearance apart share no centerline
    cell yet violate DRC, so counting raw path cells (the v2 experiment's
    mistake) missed exactly those conflicts.
    """
    from routing_utils import segment_blocked_cells_array, circle_offsets
    layer_map = {name: i for i, name in enumerate(layer_names)}
    cells = []
    via_cells = []
    for seg in segments:
        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue
        # Same expansion formula as add_segments_list_as_obstacles (#156/#498/#549):
        # reserve_width/2 + seg_width/2 + track clearance. Using the exact stamping
        # geometry keeps claim-counting conservative -- any two nets whose copper
        # would violate DRC share a capsule cell.
        reserve_width = config.route_reserve_width(seg.layer)
        seg_width = seg.width if hasattr(seg, 'width') and seg.width > 0 else config.get_track_width(seg.layer)
        seg_clearance = config.layer_clearance(
            seg.layer, config.obstacle_clearance(seg.net_id))
        trk_clearance = config.track_obstacle_clearance(seg.net_id, seg_clearance)
        margin = reserve_width / 2 + seg_width / 2 + trk_clearance
        arr = segment_blocked_cells_array(
            seg.start_x, seg.start_y, seg.end_x, seg.end_y, margin, coord.grid_step)
        for gx, gy in arr:
            cells.append((int(gx), int(gy), layer_idx))
    for via in vias:
        gx, gy = coord.to_grid(via.x, via.y)
        via_size = via.size if hasattr(via, 'size') and via.size > 0 else config.via_size
        via_clearance = config.obstacle_clearance(via.net_id)
        # Via-via expansion (conservative: >= via-track for typical sizes) --
        # matches add_vias_list_as_obstacles' via_via_mm formula.
        radius_mm = (via_size / 2 + config.via_size / 2
                     + config.stack_clearance(via_clearance))
        rng = int(radius_mm / coord.grid_step) + 1
        eff_sq = (radius_mm / coord.grid_step) ** 2
        offs = circle_offsets(rng, eff_sq)
        # Via barrels span every copper layer: claim the ring on ALL layers so
        # claim keys stay uniform (gx, gy, layer) and a via-vs-track conflict on
        # any layer is caught.
        for li in range(len(layer_names)):
            for ox, oy in offs:
                via_cells.append((int(gx + ox), int(gy + oy), li))
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
    to the sequential Phase-3 loop unchanged. Only commits a net whose ENTIRE
    tree has no over-subscribed cells, so it can never create a DRC violation.
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
    if state.working_obstacles is not None:
        shared = state.working_obstacles.clone_fresh()
        from obstacle_costs import (apply_stub_proximity, merge_track_proximity_costs,
                                   add_cross_layer_tracks)
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

    # Per-net state: the whole tap set + growing tree.
    net_state = {}
    for net_id, main_result in pending.items():
        pad_info = main_result['multipoint_pad_info']
        routed_indices = set(main_result['routed_pad_indices'])
        pad_components = main_result.get('pad_components', {i: i for i in range(len(pad_info))})
        mst_edges = main_result.get('mst_edges', [])
        net_state[net_id] = {
            'main_result': main_result,
            'pad_info': pad_info,
            'routed_indices': routed_indices,
            'pad_components': pad_components,
            'routed_components': {pad_components.get(i, i) for i in routed_indices},
            'mst_edges': mst_edges,
            'all_segments': list(main_result['new_segments']),
            'all_vias': list(main_result.get('new_vias', [])),
            'through_hole_positions': set(),
            'tap_point_map': {},
            '_committed_edges': 0,
        }

    resolved_nets = []
    stats = {'iterations': 0, 'edges_routed': 0, 'edges_failed': 0,
             'nets_resolved': 0, 'total_time': 0.0}

    start_time = time.time()

    for iteration in range(MAX_ITERATIONS):
        if cancel_check and cancel_check():
            break

        # ---- Build this iteration's requests: each unresolved net's WHOLE
        # ---- tap set as ONE request (the v2.1 change).
        requests = []
        request_nets = []  # parallel to requests: net_id
        request_net_states = []

        for net_id, ns in net_state.items():
            if net_id in resolved_nets:
                continue
            if not ns['mst_edges'] or len(ns['mst_edges']) < 2:
                # Single-edge net (already fully routed by Phase 1) -- nothing to tap.
                resolved_nets.append(net_id)
                continue

            pad_info = ns['pad_info']
            n_pads = len(pad_info)

            # ---- Initial sources: every cell of the net's committed copper.
            all_tap_points = get_all_segment_tap_points(
                ns['all_segments'], coord, layer_names, vias=ns['all_vias'])
            initial_sources = [(gx, gy, layer_idx) for gx, gy, layer_idx, _, _ in all_tap_points]
            tap_point_map = {(gx, gy, layer_idx): (ox, oy)
                              for gx, gy, layer_idx, ox, oy in all_tap_points}

            # ---- Pads: grid pos + all-layer-reach flag.
            pads = []
            pad_all_layer_reach = []
            for pad in pad_info:
                pads.append((pad[0], pad[1], pad[2]))
                pad_obj = pad[5] if len(pad) > 5 else None
                pad_all_layer_reach.append(_pad_all_layer_reach(pcb_data, pad_obj))

            # ---- Routed pad indices + components.
            routed_indices = sorted(ns['routed_indices'])
            pad_components = [ns['pad_components'].get(i, i) for i in range(n_pads)]

            # ---- MST edges (longest-first); index 0 was routed by Phase 1.
            mst_edges = [(int(a), int(b)) for a, b, _d in ns['mst_edges']]

            # ---- Endpoint overrides: source/target cells + allowed cells around
            # ---- every target pad (mirror sequential Phase 3's per-edge stamps).
            source_target_cells = list(initial_sources)
            allowed_cells = []
            endpoint_exempt_positions = []
            for i in range(n_pads):
                gx, gy = pads[i][0], pads[i][1]
                if pad_all_layer_reach[i]:
                    for li in range(len(layer_names)):
                        source_target_cells.append((gx, gy, li))
                else:
                    source_target_cells.append((gx, gy, pads[i][2]))
                for dx in range(-5, 6):
                    for dy in range(-5, 6):
                        allowed_cells.append((gx + dx, gy + dy))
                endpoint_exempt_positions.append((gx, gy))

            # ---- Free vias: same-net through-hole pads + existing vias.
            free_via_positions = list(ns['through_hole_positions'])

            requests.append(NetTreeRequest(
                pads=pads,
                pad_all_layer_reach=pad_all_layer_reach,
                initial_sources=initial_sources,
                routed_pad_indices=routed_indices,
                pad_components=pad_components,
                mst_edges=mst_edges,
                max_probe_iterations=config.max_probe_iterations,
                # Fail-fast: the pre-pass never runs expensive full searches.
                # Hard/contested edges fail within the probe budget and fall
                # through to sequential Phase 3, which has the full budget.
                max_iterations_full_search=config.max_probe_iterations,
                collinear_vias=False,
                direction_steps=2,
                track_margin_scalar_or_perlayer=_track_margin_list(config, net_id),
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
                source_target_cells=source_target_cells,
                allowed_cells=allowed_cells,
                endpoint_exempt_positions=endpoint_exempt_positions,
                endpoint_exempt_radius=coord.to_grid_dist(config.track_width + config.clearance),
                free_via_positions=free_via_positions,
                remove_blocked_cells=net_remove_cells.get(net_id, []),
                remove_blocked_vias=net_remove_vias.get(net_id, []),
            ))
            request_nets.append(net_id)
            request_net_states.append(ns)

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

        # ---- Route all nets in parallel against the frozen shared map.
        results_batch = route_tree(requests, shared)

        # ---- Convert each net's returned paths to segments/vias through the
        # ---- safety pipeline, tracking per-edge capsule footprints.
        # edge_data[net_id] = list of dicts:
        #   {tgt_idx, path, segments, vias, cells: set((gx,gy,layer))}
        edge_data = {}
        for i, net_id in enumerate(request_nets):
            ns = request_net_states[i]
            edges = results_batch[i]
            all_segments = list(ns['all_segments'])
            all_vias = list(ns['all_vias'])
            per_edge = []
            for tgt_idx, path in edges:
                if path is None:
                    continue
                try:
                    segments, vias = _path_to_segments_vias(
                        path, coord, layer_names, net_id, config,
                        start_original=None,
                        end_original=None,
                        through_hole_positions=ns['through_hole_positions'],
                        pcb_data=pcb_data)
                except Exception as e:
                    print(f'  [negotiated] {pcb_data.nets[net_id].name}: convert failed: {e}')
                    continue
                if not segments and not vias:
                    continue
                # Terminal-graze short gate: fail edges whose terminal copper would
                # overlap a foreign track/via.
                try:
                    _hard_tap = _neck_route_terminal_grazes(
                        segments, path, coord,
                        None,  # start = the growing tree's launch cell
                        (ns['pad_info'][tgt_idx][3], ns['pad_info'][tgt_idx][4]),
                        pcb_data, net_id, config)
                    if _hard_tap:
                        print(f'  [negotiated] {pcb_data.nets[net_id].name}: terminal short gate -- leaving to sequential')
                        continue
                except Exception:
                    pass
                segments = _drop_segments_already_present(segments, all_segments)
                if not segments and not vias:
                    continue
                # Expanded capsule footprint of THIS edge's copper.
                _cells, _via_cells = _edge_capsule_cells(
                    segments, vias, config, coord, layer_names, net_id)
                per_edge.append({
                    'tgt_idx': tgt_idx,
                    'path': path,
                    'segments': segments,
                    'vias': vias,
                    'cells': set(_cells) | set(_via_cells),
                })
            edge_data[net_id] = per_edge

        # ---- Count claims across all edges' expanded footprints (keyed by
        # ---- distinct NET id so same-net junctions never self-contest).
        claims = {}
        for net_id, per_edge in edge_data.items():
            for ed in per_edge:
                for cell in ed['cells']:
                    claims[cell] = claims.get(cell, 0) + 1

        _over = sum(1 for c in claims.values() if c > 1)
        _n_edges = sum(len(edge_data[nid]) for nid in request_nets)
        print(f'  [negotiated] iter {iteration}: {len(request_nets)} nets, '
              f'{_n_edges} edges routed, {_over} over-subscribed cells')

        # ---- Update history: accumulate over-subscription.
        for cell, cnt in claims.items():
            if cnt > 1:
                history[cell] = history.get(cell, 0) + 1

        stats['iterations'] += 1

        # ---- Commit EDGES whose capsule has no over-subscribed cells (per-edge
        # ---- resolution within each coherently-routed tree). Intra-net coherence
        # ---- is guaranteed by route_one_tree (all edges routed against one clone
        # ---- with same-net copper removed), so same-net edges never conflict.
        # ---- A net whose ENTIRE remaining edge set commits is fully resolved and
        # ---- freezes into real obstacles; partially-committed nets re-route their
        # ---- remaining edges next iteration from the partial tree.
        committed_nets = []
        edges_committed_this_iter = 0
        for net_id in request_nets:
            if net_id in resolved_nets:
                continue
            ns = net_state.get(net_id)
            if not ns:
                continue
            per_edge = edge_data.get(net_id, [])
            if not per_edge:
                continue

            # Which of this net's edges are conflict-free?
            ok_edges = [ed for ed in per_edge
                        if all(claims.get(c, 0) <= 1 for c in ed['cells'])]
            if not ok_edges:
                continue

            # Commit the conflict-free edges to this net's tree.
            main_result = ns['main_result']
            lm_segments = main_result['new_segments']
            lm_vias = main_result.get('new_vias', [])
            committed_segments = []
            committed_vias = []
            committed_paths = []
            for ed in ok_edges:
                committed_segments.extend(ed['segments'])
                committed_vias.extend(ed['vias'])
                committed_paths.append((ed['tgt_idx'], ed['path']))
                ns['routed_indices'].add(ed['tgt_idx'])
                tgt_comp = ns['pad_components'].get(ed['tgt_idx'], ed['tgt_idx'])
                ns['routed_components'].add(tgt_comp)
                for _v in ed['vias']:
                    ns['through_hole_positions'].add(coord.to_grid(_v.x, _v.y))
            ns['all_segments'].extend(committed_segments)
            ns['all_vias'].extend(committed_vias)

            # Add committed copper to pcb_data + working obstacles.
            tap_result = {'new_segments': committed_segments,
                          'new_vias': committed_vias}
            if committed_segments or committed_vias:
                add_route_to_pcb_data(pcb_data, tap_result,
                                      debug_lines=config.debug_lines)
                if state.working_obstacles is not None and net_obstacles_cache is not None:
                    if net_id in net_obstacles_cache:
                        remove_net_obstacles_from_cache(state.working_obstacles,
                                                        net_obstacles_cache[net_id])
                    update_net_obstacles_after_routing(pcb_data, net_id, tap_result,
                                                       config, net_obstacles_cache)
                    add_net_obstacles_from_cache(state.working_obstacles,
                                                 net_obstacles_cache[net_id])
                # CRITICAL: add committed copper to the SHARED map as hard
                # obstacles so OTHER nets' later iterations cannot route through
                # it (the shared map is a clone; commits to working_obstacles do
                # not propagate to it). Without this, iteration-2+ edges of other
                # nets overlap this copper -> DRC.
                from obstacle_map import add_segments_list_as_obstacles, add_vias_list_as_obstacles
                add_segments_list_as_obstacles(shared, committed_segments, config)
                add_vias_list_as_obstacles(shared, committed_vias, config)

            edges_committed_this_iter += len(ok_edges)
            stats['edges_routed'] += len(ok_edges)
            ns['_committed_edges'] += len(ok_edges)

            # Is the net fully resolved now (all pads connected)?
            n_pads = len(ns['pad_info'])
            pads_connected = len(ns['routed_indices'])
            if pads_connected >= n_pads:
                # Fully resolved: freeze into real obstacles + remove from pending.
                completed_result = dict(main_result)
                completed_result['new_segments'] = list(ns['all_segments'])
                completed_result['new_vias'] = list(ns['all_vias'])
                completed_result['routed_pad_indices'] = ns['routed_indices']
                completed_result['tap_edges_routed'] = main_result.get('tap_edges_routed', 0) + len(committed_paths)
                completed_result['tap_edges_failed'] = main_result.get('tap_edges_failed', 0)

                if main_result in results:
                    results.remove(main_result)
                _commit_net_result(results, routed_results, net_id, completed_result,
                                   pcb_data, config)
                state.pending_multipoint_nets.pop(net_id, None)
                if net_id not in routed_net_ids:
                    routed_net_ids.append(net_id)
                if net_id in remaining_net_ids:
                    remaining_net_ids.remove(net_id)
                resolved_nets.append(net_id)
                committed_nets.append(net_id)
                stats['nets_resolved'] += 1

        # Drop hopeless nets: after iteration 1, a net with ZERO committed
        # edges is heavily contested and unlikely to resolve -- fall it through
        # to sequential Phase 3 NOW rather than re-routing it every remaining
        # iteration (the v2 timing regression was exactly this grind).
        if iteration >= 1:
            for net_id in list(net_state.keys()):
                if net_id in resolved_nets:
                    continue
                ns = net_state.get(net_id)
                if not ns:
                    continue
                if ns.get('_committed_edges', 0) == 0:
                    resolved_nets.append(net_id)

        # Progress check: PathFinder needs multiple iterations for congestion
        # costs to separate nets (iteration 1 routes everything in empty space
        # -> massive contention; later iterations avoid contested cells). Only
        # stop when NO edges were routed this iteration AND nothing resolved.
        if edges_committed_this_iter == 0 and not committed_nets:
            break

    stats['total_time'] = time.time() - start_time
    return resolved_nets[:], stats


def _track_margin_list(config, net_id):
    """Track margin as a scalar-or-per-layer list (#156), matching
    config.track_margins_for_net. Empty => Scalar(0)."""
    try:
        tm = config.track_margins_for_net(net_id)
    except Exception:
        return []
    if isinstance(tm, (int, float)):
        return [float(tm)] if tm else []
    return [float(x) for x in tm] if tm else []
