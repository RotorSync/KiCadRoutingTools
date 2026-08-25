//! Batch parallel routing entry point.
//!
//! `route_batch(requests, shared_snapshot)` routes N nets in parallel with
//! rayon against ONE immutable obstacle snapshot inside a single FFI call
//! (no GIL involvement). Each request is fully self-contained (its own
//! router config + route params), so per-net results are deterministic and
//! independent of thread count/scheduling -- a pure function of request +
//! snapshot. The core A* logic is NOT modified; this wraps it.

use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;
use rayon::prelude::*;

use crate::obstacle_map::GridObstacleMap;
use crate::router::{GridRouter, TrackMarginArg};

/// One net's routing request: everything route_with_frontier needs, plus the
/// per-net GridRouter config (proximity heuristic cost, attraction path, etc.).
#[pyclass]
#[derive(Clone)]
pub struct RouteRequest {
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
}

#[pymethods]
impl RouteRequest {
    #[new]
    #[pyo3(signature = (sources, targets, max_iterations, collinear_vias, via_exclusion_radius, start_direction=None, end_direction=None, direction_steps=2, track_margin=TrackMarginArg::Scalar(0.0), max_iterations_ceiling=0, quantum_cells=2.0, quantum_pct=2.0, grace_tranches=0, via_rung=0, via_cost=50, h_weight=1.0, turn_cost=1000, via_proximity_cost=1, vertical_attraction_radius=0, vertical_attraction_bonus=0, layer_costs=None, proximity_heuristic_cost=0, layer_direction_preferences=None, direction_preference_cost=0, attraction_radius=0, attraction_bonus=0, attraction_cross_layer_pct=0, attraction_potential=0, attraction_path=None))]
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
        }
    }
}

/// Route a batch of nets in parallel against ONE immutable obstacle snapshot.
/// Returns per-request (path, iterations, blocked_cells) in request order.
/// Deterministic: each request is a pure function of (request + snapshot),
/// and rayon's indexed par_iter preserves order regardless of thread count.
#[pyfunction]
fn route_batch(
    py: Python<'_>,
    requests: Vec<RouteRequest>,
    obstacles: &GridObstacleMap,
) -> PyResult<Vec<(Option<Vec<(i32, i32, u8)>>, u32, Vec<(i32, i32, u8)>)>> {
    // Cap rayon threads at min(12, cores-2).
    let threads = std::cmp::min(12, std::cmp::max(1, num_cpus() - 2));
    py.allow_threads(|| {
        route_batch_with_threads(&requests, obstacles, threads)
            .map_err(|e| PyRuntimeError::new_err(format!("failed to build rayon pool: {e}")))
    })
}

/// Core batch router parameterized by thread count (so tests can compare 1
/// thread vs N threads for determinism). Routes each request against the
/// shared immutable snapshot; results come back in request order.
pub(crate) fn route_batch_with_threads(
    requests: &[RouteRequest],
    obstacles: &GridObstacleMap,
    threads: usize,
) -> Result<Vec<(Option<Vec<(i32, i32, u8)>>, u32, Vec<(i32, i32, u8)>)>, rayon::ThreadPoolBuildError> {
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
            router.route_with_frontier(
                obstacles,
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
            )
        }).collect()
    }))
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

/// Register the batch module functions/classes with the grid_router module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RouteRequest>()?;
    m.add_function(wrap_pyfunction!(route_batch, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_request(sources: Vec<(i32, i32, u8)>, targets: Vec<(i32, i32, u8)>) -> RouteRequest {
        RouteRequest {
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
        }
    }

    #[test]
    fn determinism_1_vs_n_threads() {
        // Build an obstacle map with a few blocked cells (shared snapshot).
        let mut obstacles = GridObstacleMap::new(2);
        // A wall down the middle on layer 0 to make routing non-trivial.
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

        let single = route_batch_with_threads(&requests, &obstacles, 1).unwrap();
        let n_threads = std::cmp::min(8, std::cmp::max(2, num_cpus()));
        let multi = route_batch_with_threads(&requests, &obstacles, n_threads).unwrap();

        assert_eq!(single.len(), multi.len());
        for (i, (s_path, s_iters, s_blocked)) in single.iter().enumerate() {
            let (m_path, m_iters, m_blocked) = &multi[i];
            assert_eq!(s_path, m_path, "request {i}: path differs");
            assert_eq!(s_iters, m_iters, "request {i}: iterations differ");
            assert_eq!(s_blocked, m_blocked, "request {i}: blocked cells differ");
        }
    }

    #[test]
    fn determinism_with_attraction_path() {
        let mut obstacles = GridObstacleMap::new(2);
        for y in 0..40 {
            obstacles.add_blocked_cell(20, y, 0);
        }
        let mut req = make_request(vec![(5, 5, 0)], vec![(35, 5, 0)]);
        req.attraction_path = vec![(20, 5, 0)];
        req.attraction_radius = 10;
        req.attraction_bonus = 5000;
        let requests = vec![req.clone(), req.clone(), req.clone(), req.clone()];

        let single = route_batch_with_threads(&requests, &obstacles, 1).unwrap();
        let n_threads = std::cmp::min(8, std::cmp::max(2, num_cpus()));
        let multi = route_batch_with_threads(&requests, &obstacles, n_threads).unwrap();

        for (i, (s_path, s_iters, s_blocked)) in single.iter().enumerate() {
            let (m_path, m_iters, m_blocked) = &multi[i];
            assert_eq!(s_path, m_path, "request {i}: path differs");
            assert_eq!(s_iters, m_iters, "request {i}: iterations differ");
            assert_eq!(s_blocked, m_blocked, "request {i}: blocked cells differ");
        }
    }
}
