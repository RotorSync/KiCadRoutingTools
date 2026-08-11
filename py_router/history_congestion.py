"""History-based negotiated congestion (#590) -- PathFinder-style.

Every soft cost this router already has measures the board's state NOW:
track/stub/BGA proximity price copper that exists, congestion v1/v2 price
density and demand, the ripped-route ghosts price a corridor that was just
vacated -- and vanish again the moment their owner reroutes (the C1 filter in
``routing_context.filter_ripped_ghosts``).

Nothing prices conflict over TIME. That is what PathFinder's history cost is:
a small PERMANENT bump on every cell that participates in a conflict, so
repeatedly-contested ground gets progressively more expensive and later
attempts route around it without anyone arbitrating whom to rip. The 0802
rip-arbitration study concluded exactly this by elimination -- every
frame-local rip gate was refuted, "soft rra pricing beats refusal" -- and
history is the principled version of that finding: per-CELL and cumulative
where the rip ghosts are per-NET and transient.

Conflict events (both weighted in mm-equivalent, composed like every other
soft source):

  * a RIP -- the ripped copper's own footprint was contested ground: one net
    wanted it enough to tear another net out of it. Full increment. Recorded
    from ``rip_up_reroute.rip_up_net``, which every rip in the engine
    (single-ended ladder, reroute loop, phase 3, diff-pair loop, leg rips)
    funnels through, so both front-ends get it for free.
  * a FAILED search's blocked frontier -- the cells a search stalled against.
    Weaker signal (a frontier is large and includes plenty of static copper
    no rip can clear), so it carries a fractional weight. Recorded from
    ``blocking_analysis.analyze_frontier_blocking``.

Scope is ONE routing call: the field is created/reset at batch start and
attached to the config, like congestion v2's bins. No decay (v1) -- decay is
an FPGA-ism for hundred-iteration convergence; a PCB run has a handful of rip
rounds.

Experimental knobs via environment (no CLI flag / GUI control until the lever
proves itself on the corpus -- promotion needs the full parity wiring per
CLAUDE.md):

  KICAD_HISTORY_COST            mm-equivalent added to each contested cell per
                                conflict event (0 = disabled, the default)
  KICAD_HISTORY_CAP             ceiling on the accumulated per-cell history in
                                mm-equivalent (0 = uncapped; a cap keeps
                                PRODUCTIVE churn -- the fine-pitch BGA escape
                                field, where the 0802 study found 15 rips
                                converge -- from walling itself off)
  KICAD_HISTORY_RADIUS          mm added to the copper half-width when
                                stamping a rip (default 0.25 ~ one clearance:
                                a retry that dodges by one cell has not
                                actually found different ground)
  KICAD_HISTORY_BLOCKED_WEIGHT  fraction of the increment charged to a failed
                                search's blocked frontier cells (default 0.25;
                                0 = rips only)
  KICAD_HISTORY_MAX_CELLS       growth guard: stop accepting NEW cells beyond
                                this many (existing cells keep accumulating)
"""
from __future__ import annotations

from time import perf_counter as _perf
from typing import Dict, Optional

import numpy as np

import env_knobs
from routing_config import GridCoord, GridRouteConfig, REFERENCE_GRID_STEP

# Cell packing, identical in shape to merge_track_proximity_costs' sum-mode
# key: grid coords are well inside +/-2^23.
_OFF = 1 << 23


def history_knobs() -> dict:
    # Copy so a caller mutating its dict cannot poison the read-once values.
    return dict(env_knobs.HISTORY)


def history_enabled() -> bool:
    return env_knobs.HISTORY['cost'] > 0


def _pack(layer, gx, gy) -> np.ndarray:
    return ((np.asarray(layer, dtype=np.int64) << 48)
            | ((np.asarray(gx, dtype=np.int64) + _OFF) << 24)
            | (np.asarray(gy, dtype=np.int64) + _OFF))


class HistoryField:
    """Per-cell accumulated conflict history for one routing call.

    Stored as parallel sorted arrays (packed key -> mm-equivalent weight)
    rather than a dict: an event bumps thousands of cells at once, and the
    per-prepare consumer wants an (N, 4) rows array anyway.
    """

    __slots__ = ('keys', 'weights', 'version', '_rows', '_rows_version',
                 'rips', 'frontiers', 'capped_at', 'last_frontier',
                 'record_s', 'rows_s')

    def __init__(self):
        self.keys = np.empty(0, dtype=np.int64)
        self.weights = np.empty(0, dtype=np.float64)
        self.version = 0
        self._rows = None
        self._rows_version = -1
        self.rips = 0
        self.frontiers = 0
        self.capped_at = 0          # cells refused by the growth guard
        self.last_frontier = None   # signature of the last frontier charged
        self.record_s = 0.0         # time in the event path (disclosed)
        self.rows_s = 0.0           # time building composition rows

    def accumulate(self, cells: np.ndarray, inc: float) -> None:
        """Add ``inc`` mm-equivalent to every packed cell key in ``cells``.

        Kept O(field) per event, not O(field log field): ``keys`` is sorted,
        ``np.unique`` returns the event's cells sorted, so the new cells go in
        by merge-insert instead of re-sorting the whole field.
        """
        if inc <= 0 or cells is None or len(cells) == 0:
            return
        t0 = _perf()
        cells = np.unique(np.asarray(cells, dtype=np.int64))
        if self.keys.size == 0:
            if cells.size > env_knobs.HISTORY['max_cells']:
                # Refuse the whole event's cells rather than a prefix: the keys
                # are packed coordinates, so a prefix is a spatially BIASED
                # slice of the board (low layer, low x). Chronological refusal
                # keeps whatever is in the field unbiased.
                self.capped_at += cells.size
                self.version += 1
                self.record_s += _perf() - t0
                return
            self.keys = cells
            self.weights = np.full(cells.size, float(inc), dtype=np.float64)
        else:
            idx = np.searchsorted(self.keys, cells)
            hit = np.zeros(cells.size, dtype=bool)
            inside = idx < self.keys.size
            hit[inside] = self.keys[idx[inside]] == cells[inside]
            if hit.any():
                # `cells` is unique, so the hit targets are distinct and this
                # fancy-index += cannot lose an update to aliasing.
                self.weights[idx[hit]] += float(inc)
            fresh_mask = ~hit
            if fresh_mask.any():
                fresh = cells[fresh_mask]
                if (self.keys.size + fresh.size
                        > env_knobs.HISTORY['max_cells']):
                    self.capped_at += fresh.size      # see the note above
                else:
                    self.keys = np.insert(self.keys, idx[fresh_mask], fresh)
                    self.weights = np.insert(self.weights, idx[fresh_mask],
                                             float(inc))
        self.version += 1
        self.record_s += _perf() - t0

    def rows(self) -> Optional[np.ndarray]:
        """(N, 4) [layer, gx, gy, cost] rows, or None when empty.

        Memoized on the event version: the field only changes at a conflict,
        while this is read on every per-net prepare.
        """
        if self._rows_version == self.version:
            return self._rows
        t0 = _perf()
        rows = None
        if self.keys.size:
            cap = env_knobs.HISTORY['cap']
            w = self.weights if cap <= 0 else np.minimum(self.weights, cap)
            # cell_cost is linear in mm (int(cost_mm * 1000 / REFERENCE)); do
            # it vectorized, with the same truncation.
            cost = (w * (1000.0 / REFERENCE_GRID_STEP)).astype(np.int64)
            keep = cost > 0
            if keep.any():
                k = self.keys[keep]
                out = np.empty((k.size, 4), dtype=np.int32)
                out[:, 0] = (k >> 48).astype(np.int32)
                out[:, 1] = ((k >> 24) & ((1 << 24) - 1)).astype(np.int32) - _OFF
                out[:, 2] = (k & ((1 << 24) - 1)).astype(np.int32) - _OFF
                out[:, 3] = np.minimum(cost[keep],
                                       np.iinfo(np.int32).max).astype(np.int32)
                rows = out
        self._rows = rows
        self._rows_version = self.version
        self.rows_s += _perf() - t0
        return rows

    def summary(self) -> str:
        k = env_knobs.HISTORY
        peak = float(self.weights.max()) if self.weights.size else 0.0
        capped = f", {self.capped_at} cell(s) refused by the growth guard" \
            if self.capped_at else ""
        return (f"History congestion (#590): {self.rips} rip + "
                f"{self.frontiers} blocked-frontier event(s) over "
                f"{self.keys.size} cell(s), peak "
                f"{min(peak, k['cap']) if k['cap'] > 0 else peak:.2f}mm-equiv"
                f" (inc {k['cost']}mm, cap "
                f"{k['cap'] if k['cap'] > 0 else 'none'}, frontier weight "
                f"{k['blocked_weight']}), {self.record_s + self.rows_s:.2f}s"
                f" of field upkeep{capped}")


def reset_history(config: GridRouteConfig) -> Optional[HistoryField]:
    """Fresh field for one routing call (no-op when disabled).

    Called at batch start. Scoping to the call mirrors the ripped-route
    ghosts; cross-step persistence is a v2 question (#590).
    """
    if not history_enabled():
        config._history_cong = None
        return None
    field = HistoryField()
    config._history_cong = field
    k = history_knobs()
    print(f"History congestion (#590) armed: +{k['cost']}mm-equiv per conflict"
          f", cap {k['cap'] if k['cap'] > 0 else 'none'}, rip radius "
          f"+{k['radius']}mm, frontier weight {k['blocked_weight']}")
    return field


def _field(config) -> Optional[HistoryField]:
    """The live field, or None when the feature is off for this call.

    Lazily created so a config that never went through reset_history (a
    cloned config for an oracle sub-route, a direct engine call) still
    records instead of silently dropping its conflicts.
    """
    if not history_enabled():
        return None
    field = getattr(config, '_history_cong', None)
    if field is None:
        field = HistoryField()
        config._history_cong = field
    return field


def _route_cell_keys(saved_result: dict, config: GridRouteConfig,
                     layer_map: Dict[str, int]) -> np.ndarray:
    """Packed cell keys covering a route's copper footprint.

    Segments stamp a disk of (half width + KICAD_HISTORY_RADIUS) along their
    centerline, sampled densely enough that consecutive disks overlap; vias
    stamp (half size + radius) on EVERY layer (a barrel contests all of them).
    """
    coord = GridCoord(config.grid_step)
    extra = env_knobs.HISTORY['radius']
    chunks = []

    for seg in saved_result.get('new_segments') or []:
        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue
        r = coord.to_grid_dist((seg.width or 0.0) / 2.0 + extra)
        off = _disk_offsets(r)
        gx1, gy1 = coord.to_grid(seg.start_x, seg.start_y)
        gx2, gy2 = coord.to_grid(seg.end_x, seg.end_y)
        # Sample the centerline every r/2 cells, not every cell: disks that
        # overlap by half still cover the full-radius corridor (worst-case
        # scallop 0.13 r, inside the same cell at any realistic r), and the
        # emitted row count is otherwise QUADRATIC in 1/grid_step -- a 20 mm
        # segment at the 0.025 fine-pitch grid would emit ~10^5 rows on its
        # own. Same trick as the ripped-route ghost's 1 mm sampling.
        #
        # Sampled by linspace, NOT walk_line: at this spacing the exact
        # Bresenham cells are irrelevant (each sample stamps a disk of radius
        # r around it), and a per-cell Python generator is the single most
        # expensive thing this module could do on a long power trunk.
        span = max(abs(gx2 - gx1), abs(gy2 - gy1))
        n = max(1, -(-span // max(1, r // 2)))       # ceil div, >= 1 interval
        t = np.linspace(0.0, 1.0, n + 1)
        px = np.rint(gx1 + (gx2 - gx1) * t).astype(np.int64)
        py = np.rint(gy1 + (gy2 - gy1) * t).astype(np.int64)
        gx = (px[:, None] + off[:, 0]).ravel()
        gy = (py[:, None] + off[:, 1]).ravel()
        chunks.append(_pack(layer_idx, gx, gy))

    vias = saved_result.get('new_vias') or []
    if vias:
        n_layers = max(1, len(config.layers))
        for via in vias:
            r = coord.to_grid_dist((via.size or 0.0) / 2.0 + extra)
            off = _disk_offsets(r)
            gx0, gy0 = coord.to_grid(via.x, via.y)
            gx = gx0 + off[:, 0]
            gy = gy0 + off[:, 1]
            for li in range(n_layers):
                chunks.append(_pack(li, gx, gy))

    if not chunks:
        return np.empty(0, dtype=np.int64)
    return np.concatenate(chunks)


_DISK_CACHE: Dict[int, np.ndarray] = {}


def _disk_offsets(radius_grid: int) -> np.ndarray:
    """(O, 2) integer offsets within ``radius_grid`` cells (always incl. 0,0)."""
    r = max(0, int(radius_grid))
    off = _DISK_CACHE.get(r)
    if off is None:
        if r == 0:
            off = np.zeros((1, 2), dtype=np.int64)
        else:
            ax = np.arange(-r, r + 1, dtype=np.int64)
            ex, ey = np.meshgrid(ax, ax, indexing='ij')
            m = (ex * ex + ey * ey) <= r * r
            off = np.stack((ex[m], ey[m]), axis=1)
        _DISK_CACHE[r] = off
    return off


def record_rip(config: GridRouteConfig, saved_result: dict,
               layer_map: Optional[Dict[str, int]]) -> None:
    """Conflict event: this copper was contested enough to be torn out."""
    field = _field(config)
    if field is None or not saved_result or not layer_map:
        return
    keys = _route_cell_keys(saved_result, config, layer_map)
    if keys.size == 0:
        return
    field.rips += 1
    # The board just changed: a frontier seen after this is a NEW conflict,
    # not the re-analysis the duplicate guard suppresses.
    field.last_frontier = None
    field.accumulate(keys, env_knobs.HISTORY['cost'])


def record_blocked_frontier(config: GridRouteConfig, blocked_cells) -> None:
    """Weaker conflict event: cells a FAILED search stalled against.

    ``blocked_cells`` are the router's (gx, gy, layer) frontier triples.
    """
    field = _field(config)
    if field is None or blocked_cells is None or len(blocked_cells) == 0:
        return
    w = env_knobs.HISTORY['blocked_weight']
    if w <= 0:
        return
    arr = np.asarray(blocked_cells, dtype=np.int64)
    if arr.ndim != 2 or arr.shape[1] < 3:
        return
    keys = np.unique(_pack(arr[:, 2], arr[:, 0], arr[:, 1]))
    # ONE failure is often analyzed twice back to back (a re-analysis with a
    # different exclude set, or `fresh_blockers` after a rip). That is one
    # conflict, not two -- charge consecutive identical frontiers once. A
    # frontier that recurs LATER, with other events in between, is a genuine
    # repeat and does accrue.
    sig = (keys.size, int(keys[0]), int(keys[-1]), int(keys.sum()))
    if sig == field.last_frontier:
        return
    field.last_frontier = sig
    field.frontiers += 1
    field.accumulate(keys, env_knobs.HISTORY['cost'] * w)


# Composition-pass key for the history field. A tuple, like congestion v2's,
# so it can never collide with the int net ids the same dict carries.
HISTORY_SOURCE_KEY = ('history',)


def add_history_source(ghost_costs, config):
    """Fold the history field into a ``merge_track_proximity_costs`` call's
    ghost_costs (returns it unchanged when the feature is off).

    Every obstacle builder in the engine goes through this one helper, so a
    search path cannot silently drop the field: single-ended (fresh /
    incremental / in-place prepare), diff pair, and the diff-pair layer-swap
    fallback's retry / rip / reroute maps.
    """
    rows = history_rows(config)
    if rows is None:
        return ghost_costs
    merged = dict(ghost_costs or {})
    merged[HISTORY_SOURCE_KEY] = rows
    return merged


def history_rows(config) -> Optional[np.ndarray]:
    """Composition source for merge_track_proximity_costs (None when off).

    Rows are unique per (layer, cell) by construction = within-source dedupe,
    so sum mode counts history exactly once like every other source.
    """
    field = getattr(config, '_history_cong', None)
    if field is None or not history_enabled():
        return None
    return field.rows()


def print_history_summary(config) -> None:
    field = getattr(config, '_history_cong', None)
    if field is not None and (field.rips or field.frontiers):
        print(f"  {field.summary()}")
