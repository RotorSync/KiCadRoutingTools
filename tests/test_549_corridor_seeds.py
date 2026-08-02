#!/usr/bin/env python3
"""#549 B-1: corridor seeds from committed copper (inert half).

Run 5's one-way-door measurement: pour taps sealed the QSPI wrap corridors
(10,903/10,939 static frontier cells), so no post-pour lever could improve
SCLK/SD3. seed_corridor_ghosts builds a SEPARATE soft registry from the
COMMITTED copper of nets whose path matters -- auto-derived from the board's
protected/impedance records, or named with globs -- consumed only through
via_site_clear (a preference, never an exclusion). These tests pin the seeding
modes, the site_margin arithmetic (clearance + via_size/2), the in/out
threshold, and the own-net exemption.
"""
import json
import os
import shutil
import sys
import tempfile

_TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_TESTS)
for p in (ROOT, _TESTS, os.path.join(ROOT, 'rust_router')):
    if p not in sys.path:
        sys.path.insert(0, p)

from synth import make_seg, make_via, make_net, make_pcb  # noqa: E402
from plane_corridor_ghosts import CorridorGhosts, seed_corridor_ghosts  # noqa: E402


def _pcb():
    return make_pcb(nets={1: make_net(1, 'SIG'), 2: make_net(2, 'OTHER')},
                    segments=[make_seg(10.0, 10.0, 20.0, 10.0, net_id=1,
                                       width=0.2),
                              make_seg(10.0, 30.0, 20.0, 30.0, net_id=2,
                                       width=0.2)])


def test_glob_seeding_and_threshold():
    g = seed_corridor_ghosts(_pcb(), ['SIG'], via_size=0.6, clearance=0.2)
    assert g is not None and g.net_ids() == [1]
    assert abs(g.site_margin - 0.5) < 1e-9, g.site_margin  # 0.2 + 0.6/2
    # threshold about the seg centerline: via_size/2 + w/2 + margin = 0.9
    assert not g.via_site_clear(15.0, 10.8, 0.6)   # 0.8 < 0.9: inside
    assert g.via_site_clear(15.0, 11.0, 0.6)       # 1.0 > 0.9: clear
    # the OTHER net was not seeded: right on top of it is still "clear"
    assert g.via_site_clear(15.0, 30.0, 0.6)
    print("  PASS: glob seeds SIG only; margin 0.5; in/out at 0.9mm")


def test_own_net_exempt():
    g = seed_corridor_ghosts(_pcb(), ['SIG'], via_size=0.6, clearance=0.2)
    assert g.via_site_clear(15.0, 10.0, 0.6, exclude_net_ids={1})
    print("  PASS: a corridor net's own tap is never repelled")


def test_none_sentinel_and_no_match():
    assert seed_corridor_ghosts(_pcb(), ['none'], 0.6, 0.2) is None
    assert seed_corridor_ghosts(_pcb(), ['NO_SUCH*'], 0.6, 0.2) is None
    # matched net with NO committed copper seeds nothing
    pcb = make_pcb(nets={1: make_net(1, 'SIG')})
    assert seed_corridor_ghosts(pcb, ['SIG'], 0.6, 0.2) is None
    print("  PASS: ['none'], no match, and copperless nets all return None")


def test_auto_mode_reads_protection_and_impedance():
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'b.kicad_pcb')
        open(board, 'w').write('(kicad_pcb)')
        open(os.path.join(td, 'b.kicad_pro'), 'w').write(json.dumps(
            {'kicad_routing_tools': {'protected_nets': {'SIG': 'diff-pair'}}}))
        g = seed_corridor_ghosts(_pcb(), None, 0.6, 0.2, input_file=board)
        assert g is not None and g.net_ids() == [1], g
        # no records at all -> None
        os.remove(os.path.join(td, 'b.kicad_pro'))
        assert seed_corridor_ghosts(_pcb(), None, 0.6, 0.2,
                                    input_file=board) is None
    print("  PASS: auto mode = the board's protected/impedance records")


def test_default_margin_preserves_517():
    g = CorridorGhosts('soft')
    assert abs(g.site_margin - 0.2) < 1e-9
    print("  PASS: the rip registry's default margin stays 0.2 (#517 exact)")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
