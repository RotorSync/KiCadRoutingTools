//! Parallel negotiated-congestion routing core (v2, PathFinder-style).
//!
//! `route_negotiated(requests, shared_map, threads)` routes N unresolved nets in
//! PARALLEL (rayon) against ONE frozen shared cost map inside a single FFI call
//! (no GIL involvement). The shared map carries the BASE obstacles plus the
//! present-congestion and history costs injected into `layer_proximity_costs`
//! by the Python orchestrator between iterations. Costs are FROZEN during an
//! iteration and only updated between iterations, so each request's result is a
//! PURE FUNCTION of (request + frozen map) -- rayon scheduling cannot change
//! the output, and 1-thread vs N-thread runs are byte-identical.
//!
//! Per-net endpoint state (source/target overrides, allowed cells, endpoint
//! exemptions, free vias) is NOT stored in the shared map (it is per-net and
//! would be a data race). Instead each worker clones the shared map (measured
//! ~8ms on the carrier board) and applies its own request's overrides to the
//! clone. The clone is cheap because the shared map's dynamic refcount maps are
//! small after `freeze_dynamic_to_static`; the static bitmaps clone as bits.
//!
//! The core A* logic is NOT modified; this wraps it.

use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;
use rayon::prelude::*;

use crate::obstacle_map::GridObstacleMap;
use crate::router::{GridRouter, TrackMarginArg};

/// One net's negotiated routing request: everything route_with_frontier needs,
/// plus the per-net GridRouter config and the per-net obstacle-map overrides.
#[pyclass]
#[derive(Clone)]
pub struct NegotiatedRequest {
    #[pyo3(get, set)]
    pub sources: Vec<(i32, i32, u8)>,
    #[pyo3(get, set)]
    pub targets: Vec<(i32, i32, u8)>,
    #[pyo3(get, set)]
    pub max_iterations: u32,
    #[pyo3(get, set)]
    pub collinear_vias: bool,
    #[pyo3(get, set)]
    pub via_exclusion_radius: i32,
    #[pyo3(get, set)]
    pub start_direction: Option<(i32, i32)>,
    #[pyo3(get, set)]
    pub end_direction: Option<(f64, f64)>,
    #[pyo3(get, set)]
    pub direction_steps: i32,
    pub track_margin: TrackMarginArg,
    #[pyo3(get, set)]
    pub max_iterations_ceiling: u32,
    #[pyo3(get, set)]
    pub quantum_cells: f64,
    #[pyo3(get, set)]
    pub quantum_pct: f64,
    #[pyo3(get, set)]
    pub grace_tranches: u32,
    #[pyo3(get, set)]
    pub via_rung: usize,
    // Router config
    #[pyo3(get, set)]
    pub via_cost: i32,
    #[pyo3(get, set)]
    pub h_weight: f32,
    #[pyo3(get, set)]
    pub turn_cost: i32,
    #[pyo3(get, set)]
    pub via_proximity_cost: i32,
    #[pyo3(get, set)]
    pub vertical_attraction_radius: i32,
    #[pyo3(get, set)]
    pub vertical_attraction_bonus: i32,
    #[pyo3(get, set)]
    pub layer_costs: Vec<i32>,
    #[pyo3(get, set)]
    pub proximity_heuristic_cost: i32,
    #[pyo3(get, set)]
    pub layer_direction_preferences: Vec<u8>,
    #[pyo3(get, set)]
    pub direction_preference_cost: i32,
    #[pyo3(get, set)]
    pub attraction_radius: i32,
    #[pyo3(get, set)]
    pub attraction_bonus: i32,
    #[pyo3(get, set)]
    pub attraction_cross_layer_pct: i32,
    #[pyo3(get, set)]
    pub attraction_potential: i32,
    #[pyo3(get, set)]
    pub attraction_path: Vec<(i32, i32, u8)>,
    // Per-net obstacle-map overrides applied to the worker's clone.
    #[pyo3(get, set)]
    pub source_target_cells: Vec<(i32, i32, u8)>,
    #[pyo3(get, set)]
    pub allowed_cells: Vec<(i32, i32)>,
    #[pyo3(get, set)]
    pub endpoint_exempt_positions: Vec<(i32, i32)>,
    #[pyo3(get, set)]
    pub endpoint_exempt_radius: i32,
    #[pyo3(get, set)]
    pub free_via_positions: Vec<(i32, i32)>,
    // Cells of THIS net's committed copper to remove from the worker's clone
    // (so the net can route through / tap into its own trunk). The shared map
    // keeps every net's committed copper as hard obstacles; each worker removes
    // only its own.
    #[pyo3(get, set)]
    pub remove_blocked_cells: Vec<(i32, i32, u8)>,
    #[pyo3(get, set)]
    pub remove_blocked_vias: Vec<(i32, i32)>,
}

#[pymethods]
impl NegotiatedRequest {
    #[new]
    #[pyo3(signature = (sources, targets, max_iterations, collinear_vias, via_exclusion_radius, start_direction=None, end_direction=None, direction_steps=2, track_margin=TrackMarginArg::Scalar(0.0), max_iterations_ceiling=0, quantum_cells=2.0, quantum_pct=2.0, grace_tranches=0, via_rung=0, via_cost=50, h_weight=1.0, turn_cost=1000, via_proximity_cost=1, vertical_attraction_radius=0, vertical_attraction_bonus=0, layer_costs=None, proximity_heuristic_cost=0, layer_direction_preferences=None, direction_preference_cost=0, attraction_radius=0, attraction_bonus=0, attraction_cross_layer_pct=0, attraction_potential=0, attraction_path=None, source_target_cells=None, allowed_cells=None, endpoint_exempt_positions=None, endpoint_exempt_radius=0, free_via_positions=None, remove_blocked_cells=None, remove_blocked_vias=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        sources: Vec<(i32, i32, u8)>,
        targets: Vec<(i32, i32, u8)>,
        max_iterations: u32,
        collinear_vias: bool,
        via_exclusion_radius: i32,
        start_direction: Option<(i32, i32)>,
        end_direction: Option<(f64, f64)>,
        direction_steps: i32,
        track_margin: TrackMarginArg,
        max_iterations_ceiling: u32,
        quantum_cells: f64,
        quantum_pct: f64,
        grace_tranches: u32,
        via_rung: usize,
        via_cost: i32,
        h_weight: f32,
        turn_cost: i32,
        via_proximity_cost: i32,
        vertical_attraction_radius: i32,
        vertical_attraction_bonus: i32,
        layer_costs: Option<Vec<i32>>,
        proximity_heuristic_cost: i32,
        layer_direction_preferences: Option<Vec<u8>>,
        direction_preference_cost: i32,
        attraction_radius: i32,
        attraction_bonus: i32,
        attraction_cross_layer_pct: i32,
        attraction_potential: i32,
        attraction_path: Option<Vec<(i32, i32, u8)>>,
        source_target_cells: Option<Vec<(i32, i32, u8)>>,
        allowed_cells: Option<Vec<(i32, i32)>>,
        endpoint_exempt_positions: Option<Vec<(i32, i32)>>,
        endpoint_exempt_radius: i32,
        free_via_positions: Option<Vec<(i32, i32)>>,
        remove_blocked_cells: Option<Vec<(i32, i32, u8)>>,
        remove_blocked_vias: Option<Vec<(i32, i32)>>,
    ) -> Self {
        Self {
            sources, targets, max_iterations, collinear_vias,
            via_exclusion_radius, start_direction, end_direction,
            direction_steps, track_margin, max_iterations_ceiling,
            quantum_cells, quantum_pct, grace_tranches, via_rung,
            via_cost, h_weight, turn_cost, via_proximity_cost,
            vertical_attraction_radius, vertical_attraction_bonus,
            layer_costs: layer_costs.unwrap_or_default(),
            proximity_heuristic_cost,
            layer_direction_preferences: layer_direction_preferences.unwrap_or_default(),
            direction_preference_cost,
            attraction_radius, attraction_bonus, attraction_cross_layer_pct,
            attraction_potential, attraction_path: attraction_path.unwrap_or_default(),
            source_target_cells: source_target_cells.unwrap_or_default(),
            allowed_cells: allowed_cells.unwrap_or_default(),
            endpoint_exempt_positions: endpoint_exempt_positions.unwrap_or_default(),
            endpoint_exempt_radius,
            free_via_positions: free_via_positions.unwrap_or_default(),
            remove_blocked_cells: remove_blocked_cells.unwrap_or_default(),
            remove_blocked_vias: remove_blocked_vias.unwrap_or_default(),
        }
    }
}

/// Route a batch of unresolved nets in parallel against ONE frozen shared cost
/// map. Returns per-request (path, iterations) in request order. Deterministic:
/// each request is a pure function of (request + frozen map), and rayon's
/// indexed par_iter preserves order regardless of thread count.
#[pyfunction]
fn route_negotiated(
    py: Python<'_>,
    requests: Vec<NegotiatedRequest>,
    shared_map: &GridObstacleMap,
) -> PyResult<Vec<(Option<Vec<(i32, i32, u8)>>, u32)>> {
    // Thread cap min(12, cores-2).
    let threads = std::cmp::min(12, std::cmp::max(1, num_cpus() - 2));
    py.allow_threads(|| {
        route_negotiated_with_threads(&requests, shared_map, threads)
            .map_err(|e| PyRuntimeError::new_err(format!("failed to build rayon pool: {e}")))
    })
}

/// Core negotiated router parameterized by thread count (so tests can compare 1
/// thread vs N threads for determinism). Each worker clones the shared frozen
/// map and applies its own request's endpoint overrides to the clone.
pub(crate) fn route_negotiated_with_threads(
    requests: &[NegotiatedRequest],
    shared_map: &GridObstacleMap,
    threads: usize,
) -> Result<Vec<(Option<Vec<(i32, i32, u8)>>, u32)>, rayon::ThreadPoolBuildError> {
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(threads)
        .build()?;
    Ok(pool.install(|| {
        requests.par_iter().map(|req| {
            let mut router = GridRouter::new(
                req.via_cost, req.h_weight, Some(req.turn_cost),
                Some(req.via_proximity_cost),
                req.vertical_attraction_radius, req.vertical_attraction_bonus,
                Some(req.layer_costs.clone()), Some(req.proximity_heuristic_cost),
                Some(req.layer_direction_preferences.clone()),
                req.direction_preference_cost,
                req.attraction_radius, req.attraction_bonus,
                req.attraction_cross_layer_pct, req.attraction_potential);
            if !req.attraction_path.is_empty() {
                router.set_attraction_path(req.attraction_path.clone());
            }
            // Clone the shared frozen map and apply this request's overrides.
            let mut obstacles = shared_map.clone_fresh();
            // Remove THIS net's committed copper so it can route through / tap
            // into its own trunk (the shared map keeps it as a hard obstacle).
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
            let (path, iterations, _blocked) = router.route_with_frontier(
                &obstacles,
                req.sources.clone(),
                req.targets.clone(),
                req.max_iterations,
                req.collinear_vias,
                req.via_exclusion_radius,
                req.start_direction,
                req.end_direction,
                req.direction_steps,
                req.track_margin.clone(),
                req.max_iterations_ceiling,
                req.quantum_cells,
                req.quantum_pct,
                req.grace_tranches,
                req.via_rung,
            );
            (path, iterations)
        }).collect()
    }))
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

/// Register the negotiated module classes/functions with grid_router.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NegotiatedRequest>()?;
    m.add_function(wrap_pyfunction!(route_negotiated, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_request(sources: Vec<(i32, i32, u8)>, targets: Vec<(i32, i32, u8)>) -> NegotiatedRequest {
        NegotiatedRequest {
            sources,
            targets,
            max_iterations: 100000,
            collinear_vias: false,
            via_exclusion_radius: 0,
            start_direction: None,
            end_direction: None,
            direction_steps: 2,
            track_margin: TrackMarginArg::Scalar(0.0),
            max_iterations_ceiling: 0,
            quantum_cells: 2.0,
            quantum_pct: 2.0,
            grace_tranches: 0,
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
            attraction_radius: 0,
            attraction_bonus: 0,
            attraction_cross_layer_pct: 0,
            attraction_potential: 0,
            attraction_path: vec![],
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
        // Several spatially separated requests.
        let requests = vec![
            make_request(vec![(5, 5, 0)], vec![(35, 5, 0)]),
            make_request(vec![(5, 15, 0)], vec![(35, 15, 0)]),
            make_request(vec![(5, 25, 0)], vec![(35, 25, 0)]),
            make_request(vec![(5, 35, 0)], vec![(35, 35, 0)]),
        ];

        let single = route_negotiated_with_threads(&requests, &obstacles, 1).unwrap();
        let n_threads = std::cmp::min(8, std::cmp::max(2, num_cpus()));
        let multi = route_negotiated_with_threads(&requests, &obstacles, n_threads).unwrap();

        assert_eq!(single.len(), multi.len());
        for (i, (s_path, s_iters)) in single.iter().enumerate() {
            let (m_path, m_iters) = &multi[i];
            assert_eq!(s_path, m_path, "request {i}: path differs");
            assert_eq!(s_iters, m_iters, "request {i}: iterations differ");
        }
    }

    #[test]
    fn determinism_with_endpoint_overrides() {
        // Shared map with a wall; each request has its own source/target override
        // cells so the per-worker clone path is exercised.
        let mut obstacles = GridObstacleMap::new(2);
        for y in 0..40 {
            obstacles.add_blocked_cell(20, y, 0);
        }
        let mut req = make_request(vec![(5, 5, 0)], vec![(35, 5, 0)]);
        req.source_target_cells = vec![(5, 5, 0), (35, 5, 0)];
        req.allowed_cells = vec![(5, 5), (35, 5)];
        req.endpoint_exempt_positions = vec![(5, 5), (35, 5)];
        req.endpoint_exempt_radius = 2;
        let requests = vec![req.clone(), req.clone(), req.clone(), req.clone()];

        let single = route_negotiated_with_threads(&requests, &obstacles, 1).unwrap();
        let n_threads = std::cmp::min(8, std::cmp::max(2, num_cpus()));
        let multi = route_negotiated_with_threads(&requests, &obstacles, n_threads).unwrap();

        for (i, (s_path, s_iters)) in single.iter().enumerate() {
            let (m_path, m_iters) = &multi[i];
            assert_eq!(s_path, m_path, "request {i}: path differs");
            assert_eq!(s_iters, m_iters, "request {i}: iterations differ");
        }
    }

    #[test]
    fn congestion_cost_changes_path() {
        // A corridor with a congestion cost injected into layer_proximity_costs
        // must steer the path away from it -- proving the shared cost map is
        // actually read by the parallel core.
        let mut obstacles = GridObstacleMap::new(2);
        // Two walls forcing a detour through a middle corridor.
        for y in (0..40).step_by(2) {
            obstacles.add_blocked_cell(10, y, 0);
            obstacles.add_blocked_cell(30, y, 0);
        }
        // Baseline route (no congestion cost) through the corridor.
        let req = make_request(vec![(5, 5, 0)], vec![(35, 5, 0)]);
        let base = route_negotiated_with_threads(&[req.clone()], &obstacles, 2).unwrap();
        let base_path = base[0].0.clone().unwrap();

        // Inject a high congestion cost along the straight corridor (y=5).
        let mut congested = obstacles.clone_fresh();
        for x in 5..36 {
            congested.set_layer_proximity(x, 5, 0, 50000);
        }
        let cong = route_negotiated_with_threads(&[req], &congested, 2).unwrap();
        let cong_path = cong[0].0.clone().unwrap();

        assert_ne!(base_path, cong_path,
                   "congestion cost must change the routed path");
    }
}
