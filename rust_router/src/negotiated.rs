//! Parallel multi-connection NEGOTIATED routing core -- v2.
//!
//! route_tree(requests) routes N unresolved MULTIPOINT NETS' ENTIRE TAP SETS IN PARALLEL
//! inside ONE FFI call against ONE frozen shared cost map -- parallelism moved to NET level
//! vs earlier experiments' CONNECTION level so multipoint nets never fragment across iterations.
//!
//! Per request inside one call against the frozen map:
//!   - clone the shared frozen map once;
//!   - remove THIS net's committed copper so it can route through its own trunk;
//!   - apply per-net endpoint overrides;
//!   - order taps MST-style (longest-first), matching sequential Phase 3;
//!   - route each tap TO THE NET'S OWN GROWING TREE as the frontier;
//!   - return the complete net tree as per-edge paths (+ target pad index per edge).
//!
//! Costs are FROZEN during an iteration and only updated between iterations by the Python
//! orchestrator; each request result is therefore a PURE FUNCTION of (taps + frozen map),
//! rayon scheduling cannot change output order or content -- determinism verified by unit tests.
//!
//! The core A* logic is NOT modified; this wraps route_with_frontier per edge while growing
//! one coherent per-net tree inside one call -- exactly what sequential Phase 3 does per net,
//! minus cross-net serialization between nets.
use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;
use rayon::prelude::*;
use rustc_hash::FxHashSet;

use crate::obstacle_map::GridObstacleMap;
use crate::router::{GridRouter, TrackMarginArg};

/// One multipoint net's whole-tap-set routing request.
#[pyclass]
#[derive(Clone)]
pub struct NetTreeRequest {
    // ---- Tap set / pads ---------------------------------------------------
    /// Per-pad grid position + layer index ((gx, gy, layer)).
    #[pyo3(get)]
    pub pads: Vec<(i32, i32, u8)>,
    /// Per-pad "all-layer reach" flag (through-hole / same-net via at centre).
    #[pyo3(get)]
    pub pad_all_layer_reach: Vec<bool>,
    /// Initial tap sources -- every cell of the net's existing committed copper
    /// plus its routed pads' centre cells ((gx, gy, layer)).
    #[pyo3(get)]
    pub initial_sources: Vec<(i32, i32, u8)>,
    /// Pad indices already routed by Phase 1 / prior passes.
    #[pyo3(get)]
    pub routed_pad_indices: Vec<u32>,
    /// Per-pad component id ({i:i} default).
    #[pyo3(get)]
    pub pad_components: Vec<u32>,
    /// MST edges [(idx_a, idx_b)] longest-first; index 0 was routed by Phase 1.
    #[pyo3(get)]
    pub mst_edges: Vec<(u32, u32)>,

    // ---- Router config ----------------------------------------------------
    #[pyo3(get)]
    pub max_probe_iterations: u32,
    #[pyo3(get)]
    pub max_iterations_full_search: u32,
    #[pyo3(get)]
    pub collinear_vias: bool,
    #[pyo3(get)]
    pub direction_steps: i32,
    /// Track margin scalar-or-per-layer (#156); empty => Scalar(0).
    #[pyo3(get)]
    pub track_margin_scalar_or_perlayer: Vec<f64>,
    #[pyo3(get)]
    pub via_rung: usize,
    #[pyo3(get)]
    pub via_cost: i32,
    #[pyo3(get)]
    pub h_weight: f32,
    #[pyo3(get)]
    pub turn_cost: i32,
    #[pyo3(get)]
    pub via_proximity_cost: i32,
    #[pyo3(get)]
    pub vertical_attraction_radius: i32,
    #[pyo3(get)]
    pub vertical_attraction_bonus: i32,
    #[pyo3(get)]
    pub layer_costs: Vec<i32>,
    #[pyo3(get)]
    pub proximity_heuristic_cost: i32,
    #[pyo3(get)]
    pub layer_direction_preferences: Vec<u8>,
    #[pyo3(get)]
    pub direction_preference_cost: i32,

    // ---- Per-net obstacle-map overrides applied to the worker clone --------
    /// Source/target cells to exempt from blocking (endpoints).
    #[pyo3(get)]
    pub source_target_cells: Vec<(i32, i32, u8)>,
    /// Allowed cells (BGA-zone escape around targets).
    #[pyo3(get)]
    pub allowed_cells: Vec<(i32, i32)>,
    /// Endpoint-exempt positions + radius around target pads (#424 parity).
    #[pyo3(get)]
    pub endpoint_exempt_positions: Vec<(i32, i32)>,
    #[pyo3(get)]
    pub endpoint_exempt_radius: i32,
    /// Existing same-net free-via positions ((gx, gy)) -- zero-cost layer changes there.
    #[pyo3(get)]
    pub free_via_positions: Vec<(i32, i32)>,
    /// Cells of THIS net's committed copper to remove from the worker clone so it can route
    /// through / tap into its own trunk ((gx, gy, layer)).
    #[pyo3(get)]
    pub remove_blocked_cells: Vec<(i32, i32, u8)>,
    /// Via positions of THIS net's committed copper ((gx, gy)).
    #[pyo3(get)]
    pub remove_blocked_vias: Vec<(i32, i32)>,
}

#[pymethods]
impl NetTreeRequest {
    #[new]
    #[pyo3(signature = (pads, pad_all_layer_reach, initial_sources, routed_pad_indices, pad_components, mst_edges, max_probe_iterations, max_iterations_full_search, collinear_vias, direction_steps, track_margin_scalar_or_perlayer, via_rung, via_cost, h_weight, turn_cost, via_proximity_cost, vertical_attraction_radius, vertical_attraction_bonus, layer_costs, proximity_heuristic_cost, layer_direction_preferences, direction_preference_cost, source_target_cells, allowed_cells, endpoint_exempt_positions, endpoint_exempt_radius, free_via_positions, remove_blocked_cells, remove_blocked_vias))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        pads: Vec<(i32, i32, u8)>,
        pad_all_layer_reach: Vec<bool>,
        initial_sources: Vec<(i32, i32, u8)>,
        routed_pad_indices: Vec<u32>,
        pad_components: Vec<u32>,
        mst_edges: Vec<(u32, u32)>,
        max_probe_iterations: u32,
        max_iterations_full_search: u32,
        collinear_vias: bool,
        direction_steps: i32,
        track_margin_scalar_or_perlayer: Vec<f64>,
        via_rung: usize,
        via_cost: i32,
        h_weight: f32,
        turn_cost: i32,
        via_proximity_cost: i32,
        vertical_attraction_radius: i32,
        vertical_attraction_bonus: i32,
        layer_costs: Vec<i32>,
        proximity_heuristic_cost: i32,
        layer_direction_preferences: Vec<u8>,
        direction_preference_cost: i32,
        source_target_cells: Vec<(i32, i32, u8)>,
        allowed_cells: Vec<(i32, i32)>,
        endpoint_exempt_positions: Vec<(i32, i32)>,
        endpoint_exempt_radius: i32,
        free_via_positions: Vec<(i32, i32)>,
        remove_blocked_cells: Vec<(i32, i32, u8)>,
        remove_blocked_vias: Vec<(i32, i32)>,
    ) -> Self {
        Self {
            pads, pad_all_layer_reach, initial_sources, routed_pad_indices,
            pad_components, mst_edges, max_probe_iterations,
            max_iterations_full_search, collinear_vias, direction_steps,
            track_margin_scalar_or_perlayer, via_rung, via_cost, h_weight,
            turn_cost, via_proximity_cost, vertical_attraction_radius,
            vertical_attraction_bonus, layer_costs, proximity_heuristic_cost,
            layer_direction_preferences, direction_preference_cost,
            source_target_cells, allowed_cells, endpoint_exempt_positions,
            endpoint_exempt_radius, free_via_positions, remove_blocked_cells,
            remove_blocked_vias,
        }
    }
}


/// One edge outcome inside a routed tree.
#[derive(Clone)]
struct EdgeOutcome {
    tgt_pad_idx: u32,
    path: Option<Vec<(i32, i32, u8)>>,
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

fn track_margin_arg(vals: &[f64]) -> TrackMarginArg {
    match vals.len() {
        0 => TrackMarginArg::Scalar(0.0),
        1 => TrackMarginArg::Scalar(vals[0]),
        _ => TrackMarginArg::PerLayer(vals.to_vec()),
    }
}

/// Route one multipoint net's ENTIRE tap set against one frozen clone.
/// Returns per-edge outcomes aligned with MST processing order.
fn route_one_tree(req: &NetTreeRequest,
                  shared_map: &GridObstacleMap)
                  -> Vec<EdgeOutcome> {
    // ---- Clone the shared frozen map once and apply this net's overrides ----
    let mut obstacles = shared_map.clone_fresh();
    if !req.remove_blocked_cells.is_empty() {
        obstacles.remove_blocked_cells_plain(&req.remove_blocked_cells);
    }
    if !req.remove_blocked_vias.is_empty() {
        obstacles.remove_blocked_vias_plain(&req.remove_blocked_vias);
    }
    for &(gx, gy, layer) in &req.source_target_cells {
        obstacles.add_source_target_cell(gx, gy, layer as usize);
    }
    for &(gx, gy) in &req.allowed_cells {
        obstacles.add_allowed_cell(gx, gy);
    }
    if !req.endpoint_exempt_positions.is_empty() || req.endpoint_exempt_radius > 0 {
        obstacles.set_endpoint_exempt(req.endpoint_exempt_positions.clone(),
                                      req.endpoint_exempt_radius);
    }
    if !req.free_via_positions.is_empty() {
        obstacles.add_free_vias_batch(req.free_via_positions.clone());
    }

    let mut router = GridRouter::new(
        req.via_cost, req.h_weight, Some(req.turn_cost),
        Some(req.via_proximity_cost),
        req.vertical_attraction_radius, req.vertical_attraction_bonus,
        Some(req.layer_costs.clone()), Some(req.proximity_heuristic_cost),
        Some(req.layer_direction_preferences.clone()),
        req.direction_preference_cost,
        0, 0, 0, 0);  // attraction disabled (bus routing only)
    let track_margin = track_margin_arg(&req.track_margin_scalar_or_perlayer);

    // ---- Per-net tap state (mirrors sequential Phase 3) ---------------------
    let n_pads = req.pads.len();
    let mut routed_indices: FxHashSet<u32> = req.routed_pad_indices.iter().copied().collect();
    let mut routed_components: FxHashSet<u32> = req.routed_pad_indices.iter()
        .map(|&i| req.pad_components.get(i as usize).copied().unwrap_or(i))
        .collect();
    // The net's growing tree: every cell of copper routed so far (this call),
    // plus the initial sources. Each new edge launches from ALL of these.
    let mut tree_sources: Vec<(i32, i32, u8)> = req.initial_sources.clone();
    let mut tree_set: FxHashSet<(i32, i32, u8)> = req.initial_sources.iter().copied().collect();

    let mut outcomes: Vec<EdgeOutcome> = Vec::new();

    // Remaining MST edges (index 0 was routed by Phase 1).
    let mut remaining: Vec<(u32, u32)> = req.mst_edges.iter().skip(1).copied().collect();

    // Route edges longest-first until every pad is connected or no edge is eligible.
    let max_passes = remaining.len() * 2 + n_pads + 4;
    for _pass in 0..max_passes {
        if routed_indices.len() == n_pads {
            break;
        }
        // Find an edge connecting a routed pad/component to an unrouted one.
        let mut edge_to_route: Option<(u32, u32)> = None;
        for &(a, b) in &remaining {
            let a_routed = routed_indices.contains(&a)
                || routed_components.contains(&req.pad_components.get(a as usize).copied().unwrap_or(a));
            let b_routed = routed_indices.contains(&b)
                || routed_components.contains(&req.pad_components.get(b as usize).copied().unwrap_or(b));
            if a_routed && !b_routed {
                edge_to_route = Some((a, b));
                break;
            } else if b_routed && !a_routed {
                edge_to_route = Some((b, a));
                break;
            }
        }
        let (src_idx, tgt_idx) = match edge_to_route {
            Some(e) => e,
            None => break,
        };

        // Sources: the whole growing tree (all cells) -- the frontier machinery
        // is built for exactly this (route_with_frontier takes many sources).
        let sources = tree_sources.clone();
        // Targets: target pad on all layers if through-hole reach, else its layer.
        let (tgx, tgy, tglayer) = req.pads[tgt_idx as usize];
        let targets: Vec<(i32, i32, u8)> = if req.pad_all_layer_reach[tgt_idx as usize] {
            (0..obstacles.num_layers as u8).map(|l| (tgx, tgy, l)).collect()
        } else {
            vec![(tgx, tgy, tglayer)]
        };

        // Endpoint overrides for this edge (mirror sequential Phase 3).
        for &(gx, gy, layer) in sources.iter().chain(targets.iter()) {
            obstacles.add_source_target_cell(gx, gy, layer as usize);
        }
        let allow_radius = 5;
        for dx in -allow_radius..=allow_radius {
            for dy in -allow_radius..=allow_radius {
                obstacles.add_allowed_cell(tgx + dx, tgy + dy);
            }
        }
        obstacles.set_endpoint_exempt(
            targets.iter().take(8).map(|&(gx, gy, _)| (gx, gy)).collect(),
            req.endpoint_exempt_radius);

        // Probe forward then backward at probe budget; if both reach max, full search.
        let probe = req.max_probe_iterations;
        let (mut path, _iters) = route_with_frontier_wrap(
            &router, &obstacles, &sources, &targets,
            probe, req.collinear_vias, req.direction_steps,
            &track_margin, req.via_rung);
        if path.is_none() {
            // Backward probe.
            let (p2, _) = route_with_frontier_wrap(
                &router, &obstacles, &targets, &sources,
                probe, req.collinear_vias, req.direction_steps,
                &track_margin, req.via_rung);
            if p2.is_some() {
                path = p2.map(|v| v.into_iter().rev().collect());
            }
        }
        if path.is_none() {
            // Both probes failed; full search forward then backward.
            let full = req.max_iterations_full_search.max(probe);
            let (p3, _) = route_with_frontier_wrap(
                &router, &obstacles, &sources, &targets,
                full, req.collinear_vias, req.direction_steps,
                &track_margin, req.via_rung);
            if p3.is_some() {
                path = p3;
            } else {
                let (p4, _) = route_with_frontier_wrap(
                    &router, &obstacles, &targets, &sources,
                    full, req.collinear_vias, req.direction_steps,
                    &track_margin, req.via_rung);
                if p4.is_some() {
                    path = p4.map(|v| v.into_iter().rev().collect());
                }
            }
        }

        match path {
            Some(p) => {
                // Grow the tree with this edge's cells.
                for &cell in &p {
                    if tree_set.insert(cell) {
                        tree_sources.push(cell);
                    }
                }
                routed_indices.insert(tgt_idx);
                let comp = req.pad_components.get(tgt_idx as usize).copied().unwrap_or(tgt_idx);
                routed_components.insert(comp);
                remaining.retain(|&(a, b)| !(a == src_idx && b == tgt_idx)
                                             && !(a == tgt_idx && b == src_idx));
                outcomes.push(EdgeOutcome { tgt_pad_idx: tgt_idx, path: Some(p) });
            }
            None => {
                outcomes.push(EdgeOutcome { tgt_pad_idx: tgt_idx, path: None });
                // Leave the edge in `remaining` so a later pass may retry it.
            }
        }
    }

    outcomes
}

/// Thin wrapper over GridRouter::route_with_frontier with the fixed arg set.
fn route_with_frontier_wrap(
    router: &GridRouter,
    obstacles: &GridObstacleMap,
    sources: &[(i32, i32, u8)],
    targets: &[(i32, i32, u8)],
    max_iterations: u32,
    collinear_vias: bool,
    direction_steps: i32,
    track_margin: &TrackMarginArg,
    via_rung: usize,
) -> (Option<Vec<(i32, i32, u8)>>, u32) {
    let (path, iters, _blocked) = router.route_with_frontier(
        obstacles,
        sources.to_vec(),
        targets.to_vec(),
        max_iterations,
        collinear_vias,
        0,                       // via_exclusion_radius (probe passes use 0)
        None,                    // start_direction
        None,                    // end_direction
        direction_steps,
        track_margin.clone(),
        0,                       // max_iterations_ceiling (dynamic handled in Python)
        2.0,
        2.0,
        0,
        via_rung,
    );
    (path, iters)
}

/// Route N multipoint nets' ENTIRE tap sets in parallel against ONE frozen shared cost map.
/// Returns per-net Vec<EdgeOutcome> in request order (rayon indexed par_iter preserves order).
#[pyfunction]
fn route_tree(
    py: Python<'_>,
    requests: Vec<NetTreeRequest>,
    shared_map: &GridObstacleMap,
) -> PyResult<Vec<Vec<(u32, Option<Vec<(i32, i32, u8)>>)>>> {
    // Thread cap min(12, cores-2).
    let threads = std::cmp::min(12, std::cmp::max(1, num_cpus() - 2));
    py.allow_threads(|| {
        route_tree_with_threads(&requests, shared_map, threads)
            .map_err(|e| PyRuntimeError::new_err(format!("failed to build rayon pool: {e}")))
    })
}

/// Core tree router parameterized by thread count (so tests can compare 1 thread
/// vs N threads for determinism). Each worker clones the shared frozen map and
/// applies its own request's overrides to the clone.
pub(crate) fn route_tree_with_threads(
    requests: &[NetTreeRequest],
    shared_map: &GridObstacleMap,
    threads: usize,
) -> Result<Vec<Vec<(u32, Option<Vec<(i32, i32, u8)>>)>>, rayon::ThreadPoolBuildError> {
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(threads)
        .build()?;
    Ok(pool.install(|| {
        requests.par_iter().map(|req| {
            route_one_tree(req, shared_map).into_iter()
                .map(|o| (o.tgt_pad_idx, o.path))
                .collect::<Vec<_>>()
        }).collect()
    }))
}

/// Register the negotiated module classes/functions with grid_router.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NetTreeRequest>()?;
    m.add_function(wrap_pyfunction!(route_tree, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_request() -> NetTreeRequest {
        NetTreeRequest {
            pads: vec![],
            pad_all_layer_reach: vec![],
            initial_sources: vec![],
            routed_pad_indices: vec![],
            pad_components: vec![],
            mst_edges: vec![],
            max_probe_iterations: 5000,
            max_iterations_full_search: 200000,
            collinear_vias: false,
            direction_steps: 2,
            track_margin_scalar_or_perlayer: vec![],
            via_rung: 0,
            via_cost: 50,
            h_weight: 1.0,
            turn_cost: 1000,
            via_proximity_cost: 1,
            vertical_attraction_radius: 0,
            vertical_attraction_bonus: 0,
            layer_costs: vec![1000, 1000],
            proximity_heuristic_cost: 0,
            layer_direction_preferences: vec![255, 255],
            direction_preference_cost: 0,
            source_target_cells: vec![],
            allowed_cells: vec![],
            endpoint_exempt_positions: vec![],
            endpoint_exempt_radius: 0,
            free_via_positions: vec![],
            remove_blocked_cells: vec![],
            remove_blocked_vias: vec![],
        }
    }

    #[test]
    fn determinism_1_vs_n_threads() {
        // Build a shared frozen map with a wall down the middle on layer 0.
        let mut obstacles = GridObstacleMap::new(2);
        for y in 0..40 {
            obstacles.add_blocked_cell(20, y, 0);
        }

        // Several spatially separated multipoint nets (3 pads each).
        let mut reqs = Vec::new();
        for base_y in [5i32, 15, 25, 35] {
            let mut r = make_request();
            r.pads = vec![(5, base_y, 0), (35, base_y, 0), (20, base_y + 10, 0)];
            r.pad_all_layer_reach = vec![false, false, false];
            r.routed_pad_indices = vec![0];
            r.pad_components = vec![0, 1, 2];
            r.mst_edges = vec![(0, 1), (1, 2), (0, 2)];
            r.initial_sources = vec![(5, base_y, 0)];
            reqs.push(r);
        }

        let single = route_tree_with_threads(&reqs, &obstacles, 1).unwrap();
        let n_threads = std::cmp::min(8, std::cmp::max(2, num_cpus()));
        let multi = route_tree_with_threads(&reqs, &obstacles, n_threads).unwrap();

        assert_eq!(single.len(), multi.len());
        for (i, (s_edges, m_edges)) in single.iter().zip(multi.iter()).enumerate() {
            assert_eq!(s_edges.len(), m_edges.len(), "net {i}: edge count differs");
            for (j, ((s_tgt, s_path), (m_tgt, m_path))) in
                    s_edges.iter().zip(m_edges.iter()).enumerate() {
                assert_eq!(s_tgt, m_tgt, "net {i} edge {j}: target differs");
                assert_eq!(s_path, m_path, "net {i} edge {j}: path differs");
            }
        }
    }

    #[test]
    fn tree_grows_coherently() {
        // A single multipoint net with 4 pads; the whole tree must route in one call.
        let mut obstacles = GridObstacleMap::new(2);
        let mut r = make_request();
        r.pads = vec![(5, 5, 0), (35, 5, 0), (35, 35, 0), (5, 35, 0)];
        r.pad_all_layer_reach = vec![false, false, false, false];
        // Phase-1 main edge (0,1) already routed: both ends are routed pads.
        r.routed_pad_indices = vec![0, 1];
        r.pad_components = vec![0, 1, 2, 3];
        r.mst_edges = vec![(0, 1), (1, 2), (2, 3)];
        r.initial_sources = vec![(5, 5, 0), (35, 5, 0)];

        let out = route_tree_with_threads(&[r], &obstacles, 2).unwrap();
        let edges = &out[0];
        // Phase-1 routed MST edge (0,1); the remaining 2 edges must route
        // (no wall in the way) and connect the whole tree in one call.
        assert_eq!(edges.len(), 2);
        for (tgt, path) in edges {
            assert!(path.is_some(), "target {tgt} should have routed");
        }
    }

    #[test]
    fn congestion_cost_changes_tree() {
        // A corridor with a congestion cost injected into layer_proximity_costs
        // must steer the tree away from it -- proving the shared cost map is read.
        let mut obstacles = GridObstacleMap::new(2);
        for y in (0..40).step_by(2) {
            obstacles.add_blocked_cell(10, y, 0);
            obstacles.add_blocked_cell(30, y, 0);
        }

        let mut mk = || {
            let mut r = make_request();
            r.pads = vec![(5, 5, 0), (35, 5, 0), (20, 20, 0)];
            r.pad_all_layer_reach = vec![false, false, false];
            // Phase-1 main edge (0,1) already routed.
            r.routed_pad_indices = vec![0, 1];
            r.pad_components = vec![0, 1, 2];
            r.mst_edges = vec![(0, 1), (1, 2)];
            r.initial_sources = vec![(5, 5, 0), (35, 5, 0)];
            r
        };

        let base = route_tree_with_threads(&[mk()], &obstacles, 2).unwrap();

        // Inject a high congestion cost along the diagonal corridor the base
        // path actually uses (the (x,x) diagonal from (5,5) to (20,20)).
        let mut congested = obstacles.clone_fresh();
        for x in 5..21 {
            congested.set_layer_proximity(x, x, 0, 50000);
            congested.set_layer_proximity(x, x, 1, 50000);
        }
        let cong = route_tree_with_threads(&[mk()], &congested, 2).unwrap();

        let base_path = base[0][0].1.clone().unwrap();
        let cong_path = cong[0][0].1.clone().unwrap();
        assert_ne!(base_path, cong_path,
                   "congestion cost must change the routed tree");
    }
}
