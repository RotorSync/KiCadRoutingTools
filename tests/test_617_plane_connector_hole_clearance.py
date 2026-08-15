#!/usr/bin/env python3
"""Issue #617, engine 1/3: plane_region_connector must read min_hole_clearance.

#616 gave the router `obstacle_map.resolve_hole_clearance` -- the board's own
declared copper-to-hole floor -- and wired it into `add_drill_hole_obstacles`
and `plane_obstacle_builder`. Its docstring named the engines it did NOT reach,
and this is the first of them: the plane-repair connector (taps / region joins
/ reconnects) priced every NPTH keep-out at a flat
`max(config.clearance, NPTH_TO_TRACK_CLEARANCE)` -- 0.20 mm, never reading the
board -- at THREE independent sites:

    npth_floor_ok        the non-relaxable seed/anchor floor (#390)
    wide_route_clear     the per-leg legality predicate for width upgrades
    build_base_obstacles the repair router's own NPTH track keep-out stamp

`check_drc` DOES read `min_hole_clearance` (check_drc.py:2390), so on a board
declaring 0.25 all three approved plane-repair copper inside the 0.20-0.25
band its own checker then flagged -- measured on neo6502 at 0.2263 mm against
a declared 0.25.

All three are SEARCH constraints over new copper, never one-shot repairs: each
case therefore asserts BOTH halves -- copper inside the declared band is
refused, AND copper at/outside the declared floor is still accepted, so the
seed/route/cell search still has somewhere to go. (The passes whose only
alternative to their single candidate is doing nothing are deliberately NOT
changed by #617; see test_617_pcb_modification_hole_clearance.py.)

Every case is built on a board whose sibling project DECLARES
`min_hole_clearance: 0.25`, and geometry placed at exactly 0.22 mm of
copper-to-hole-wall: legal at the flat 0.20 fab floor, a 0.03 mm violation of
what the board actually asks for. A silent-board control pins that the change
is RAISE-ONLY (a board declaring nothing keeps every old decision).

    python3 tests/test_617_plane_connector_hole_clearance.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import obstacle_map
import plane_region_connector as prc
from kicad_parser import BoardInfo
from routing_config import GridRouteConfig
from synth import make_pad, make_pcb

# The board's NPTH mounting hole and the geometry under test.
HOLE_X, HOLE_Y = 10.0, 10.0
DRILL = 1.0                 # -> hole wall at radius 0.5
TRACK_W = 0.2               # -> half-width 0.1
DECLARED = 0.25             # what the board asks for
BAND = 0.22                 # copper-to-hole-WALL gap under test
# Centreline offset that puts the copper edge BAND off the hole wall.
CENTRE = HOLE_X + DRILL / 2.0 + BAND + TRACK_W / 2.0     # 10.82
# Centreline offset that CLEARS the declared floor (the alternative the search
# still has once the band is refused).
CLEAR_CENTRE = HOLE_X + DRILL / 2.0 + DECLARED + TRACK_W / 2.0 + 0.01
CLEARANCE = 0.15            # routing clearance, below both floors


def _board_dir(tmp, name, **rules):
    """A board file whose sibling project declares `rules` (KiCad's
    board.design_settings.rules block, where min_hole_clearance lives)."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    pcb = os.path.join(d, 'b.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as f:
        f.write('(kicad_pcb (version 20240108))\n')
    with open(os.path.join(d, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump({'board': {'design_settings': {'rules': rules}}}, f)
    return pcb


def _npth_pad():
    return make_pad(net_id=0, x=HOLE_X, y=HOLE_Y, ref='BUS1', num='H1',
                    size_x=DRILL, size_y=DRILL, shape='circle',
                    layers=['*.Cu', '*.Mask'], drill=DRILL,
                    pad_type='np_thru_hole')


def _pcb(path, bounds=(0.0, 0.0, 20.0, 20.0)):
    """A one-mounting-hole board rooted at `path` (so the engine's board read
    resolves through PCBData.source_path, the #498 mechanism)."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=bounds)
    return make_pcb(board_info=bi, pads_by_net={0: [_npth_pad()]},
                    source_path=path)


def _config(grid_step=0.1):
    cfg = GridRouteConfig()
    cfg.clearance = CLEARANCE
    cfg.track_width = TRACK_W
    cfg.grid_step = grid_step
    cfg.layers = ['F.Cu', 'B.Cu']
    cfg.board_edge_clearance = 0.0
    return cfg


def run():
    fails = []

    def check(name, cond):
        print(('PASS' if cond else 'FAIL') + f': {name}')
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        declares = _board_dir(tmp, 'declares', min_hole_clearance=DECLARED)
        silent = _board_dir(tmp, 'silent')
        obstacle_map._HOLE_CLR_CACHE.clear()

        print(f'board declares min_hole_clearance = {DECLARED}mm; geometry '
              f'sits {BAND}mm off the hole wall')
        print(f'  hole  ({HOLE_X}, {HOLE_Y}) drill {DRILL} -> wall at r='
              f'{DRILL / 2.0}')
        print(f'  track width {TRACK_W} centred at x={CENTRE:.4g} -> copper '
              f'edge {BAND}mm from the wall')
        print()

        _seed_floor(check, declares, silent)
        _leg_legality(check, declares, silent)
        _base_map_stamp(check, declares, silent)

    print()
    if fails:
        print(f'{len(fails)} FAILURE(S): {fails}')
        return 1
    print('all checks passed')
    return 0


# --- site 1: npth_floor_ok (route seed / anchor points, #390) ---------------
def _seed_floor(check, declares, silent):
    print('site 1: npth_floor_ok -- the non-relaxable seed floor')
    half = TRACK_W / 2.0

    ok_declared = prc.npth_floor_ok(CENTRE, HOLE_Y, _pcb(declares), half)
    print(f'        seed at x={CENTRE:.4g} on the DECLARING board: '
          f'accepted={ok_declared}')
    check('declared 0.25 rejects a seed 0.22 off the hole wall',
          not ok_declared)

    ok_silent = prc.npth_floor_ok(CENTRE, HOLE_Y, _pcb(silent), half)
    print(f'        same seed on a board declaring NOTHING: '
          f'accepted={ok_silent}')
    check('a board declaring nothing still accepts it (raise-only)',
          ok_silent)

    # No repair is given up: the seeder still has candidates -- everything at
    # or beyond the declared floor is accepted on the declaring board.
    ok_alt = prc.npth_floor_ok(CLEAR_CENTRE, HOLE_Y, _pcb(declares), half)
    print(f'        alternative seed at x={CLEAR_CENTRE:.4g} '
          f'({DECLARED + 0.01:.2f}mm off the wall): accepted={ok_alt}')
    check('a seed outside the DECLARED floor stays accepted', ok_alt)
    print()


# --- site 2: wide_route_clear (per-leg legality for the width upgrade) ------
def _leg_legality(check, declares, silent):
    print('site 2: wide_route_clear -- per-leg legality')
    cfg = _config()
    pts = [(CENTRE, 8.0, 'F.Cu'), (CENTRE, 12.0, 'F.Cu')]

    clear_declared = prc.wide_route_clear(pts, TRACK_W, _pcb(declares), 1, cfg)
    print(f'        leg past the hole on the DECLARING board: '
          f'clear={clear_declared}')
    check('declared 0.25 rejects a leg 0.22 off the hole wall',
          not clear_declared)

    clear_silent = prc.wide_route_clear(pts, TRACK_W, _pcb(silent), 1, cfg)
    print(f'        same leg on a board declaring NOTHING: '
          f'clear={clear_silent}')
    check('a board declaring nothing still passes the leg (raise-only)',
          clear_silent)

    alt = prc.wide_route_clear([(CLEAR_CENTRE, 8.0, 'F.Cu'),
                                (CLEAR_CENTRE, 12.0, 'F.Cu')],
                               TRACK_W, _pcb(declares), 1, cfg)
    print(f'        alternative leg at x={CLEAR_CENTRE:.4g}: clear={alt}')
    check('a leg outside the DECLARED floor stays clear', alt)
    print()


# --- site 3: build_base_obstacles (the repair router's NPTH keep-out) -------
def _base_map_stamp(check, declares, silent):
    """Run the REAL stamp and query the REAL map, rather than intercepting the
    clearance argument: what matters is which cells the repair router may use.

    Grid step 0.01 over a 6x6mm board so the 0.22 band is exactly on-grid and
    well clear of the board-edge keep-out:
      cell x=10.82  -> copper edge 0.22 off the wall  (inside a declared 0.25)
      cell x=10.86  -> copper edge 0.26 off the wall  (the alternative)
    """
    print('site 3: build_base_obstacles -- the NPTH track keep-out stamp')
    step = 0.01
    cfg = _config(grid_step=step)
    band_gx = round(CENTRE / step)                      # 1082 -> 10.82mm
    alt_gx = round((HOLE_X + DRILL / 2.0 + 0.26 + TRACK_W / 2.0) / step)
    gy = round(HOLE_Y / step)
    bounds = (HOLE_X - 3.0, HOLE_Y - 3.0, HOLE_X + 3.0, HOLE_Y + 3.0)

    got = {}
    for path, label in ((declares, 'declared 0.25'), (silent, 'nothing declared')):
        obstacles, _lm = prc.build_base_obstacles(
            {1}, ['F.Cu', 'B.Cu'], _pcb(path, bounds), cfg, TRACK_W, 0.2)
        got[label] = (bool(obstacles.is_blocked(band_gx, gy, 0)),
                      bool(obstacles.is_blocked(alt_gx, gy, 0)))
        print(f'        {label}: cell at {BAND}mm off the wall '
              f'blocked={got[label][0]}; cell at 0.26mm off the wall '
              f'blocked={got[label][1]}')

    check('the declared 0.25 blocks a track cell 0.22 off the hole wall',
          got['declared 0.25'][0])
    check('a board declaring nothing leaves that cell free (raise-only)',
          not got['nothing declared'][0])
    check('the keep-out stays minimal -- a cell 0.26 off the wall is free on '
          'BOTH boards, so the repair router still has a way past the hole',
          not got['declared 0.25'][1] and not got['nothing declared'][1])


if __name__ == '__main__':
    sys.exit(run())
