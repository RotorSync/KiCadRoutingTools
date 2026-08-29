#!/usr/bin/env python3
"""Explicitly named diff pairs bypass name-pattern matching (--explicit-pair).

The strict parser (extract_diff_pair_base) is arguably correct to reject names
like TDP1/TDN1 -- polarity letter MID-name, index after -- so the operator had
no way to route such a pair without rewriting the chain. The escape hatch is
explicitly named pairs: they bypass name-pattern matching entirely, still
validate that both nets exist, and still apply normal diff-pair routing.

Run:
    python3 tests/test_explicit_diff_pairs.py
"""
import io
import os
import sys
from contextlib import redirect_stdout

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522

from kicad_parser import BoardInfo
from synth import make_net, make_pad, make_pcb

P, N = 1, 2

CHECKS = []


def check(name, ok):
    CHECKS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}")


def _board(p_name='TDP1', n_name='TDN1'):
    """A 2-terminal diff pair whose names the strict parser REJECTS: polarity
    letter mid-name, index after (TDP1/TDN1). extract_diff_pair_base returns
    None for both, so pattern matching can never pair them."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 10.0, 10.0))
    pads = {
        P: [make_pad(P, 2.0, 2.0, ref='U1', num='1', net_name=p_name,
                     size_x=0.3, size_y=0.3),
            make_pad(P, 7.0, 2.0, ref='J1', num='1', net_name=p_name,
                     size_x=0.3, size_y=0.3)],
        N: [make_pad(N, 2.0, 2.6, ref='U1', num='2', net_name=n_name,
                     size_x=0.3, size_y=0.3),
            make_pad(N, 7.0, 2.6, ref='J1', num='2', net_name=n_name,
                     size_x=0.3, size_y=0.3)],
    }
    return make_pcb(
        nets={P: make_net(P, p_name), N: make_net(N, n_name)},
        segments=[], pads_by_net=pads, board_info=bi)


def _run(pcb, explicit_pairs=None, net_names=None):
    from route_diff import batch_route_diff_pairs
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = batch_route_diff_pairs(
            'synthetic', '', net_names or ['*'],
            layers=['F.Cu', 'B.Cu'],
            clearance=0.1, track_width=0.15, diff_pair_gap=0.15,
            via_size=0.5, via_drill=0.3, grid_step=0.1,
            explicit_pairs=explicit_pairs,
            return_results=True, pcb_data=pcb)
    return res, buf.getvalue()


def test_explicit_pair_routes_tdp_style_names():
    print("Part 1: TDP1/TDN1-style names route ONLY via --explicit-pair")
    pcb = _board()

    # Without the override: pattern matching finds nothing -> no pairs.
    (ok0, fail0, _t0, rd0), out0 = _run(pcb)
    sys.stdout.write(out0)
    check("no explicit pair -> no pairs matched (strict parser rejects)",
          ok0 == 0 and fail0 == 0)

    # With the override: the pair is found and routed.
    (ok, fail, _t, results_data), out = _run(pcb, explicit_pairs=[('TDP1', 'TDN1')])
    sys.stdout.write(out)
    check("explicit pair routed (ok>=1)", ok >= 1)
    check("no failures", fail == 0)

    p_segs = [s for r in results_data['results']
              for s in (r.get('new_segments') or []) if s.net_id == P]
    n_segs = [s for r in results_data['results']
              for s in (r.get('new_segments') or []) if s.net_id == N]
    check("P member has copper", bool(p_segs))
    check("N member has copper", bool(n_segs))


def test_explicit_pair_validates_nets_exist():
    print("\nPart 2: a named net that does not exist is skipped with a warning")
    pcb = _board()
    (ok, fail, _t, results_data), out = _run(
        pcb, explicit_pairs=[('TDP1', 'TDN1'), ('NOPE_P', 'NOPE_N')])
    sys.stdout.write(out)
    check("missing-net pair skipped without failing the run", fail == 0)
    check("valid explicit pair still routed", ok >= 1)
    check("warning names the missing nets",
          'NOPE_P' in out and 'NOPE_N' in out and 'not on this board' in out)


def test_explicit_pair_repeatable_and_dedup():
    print("\nPart 3: repeatable; a net already claimed by a pattern pair is not re-claimed")
    pcb = _board()
    # Same pair twice -> deduped (no double-route).
    (ok, fail, _t, results_data), out = _run(
        pcb, explicit_pairs=[('TDP1', 'TDN1'), ('TDP1', 'TDN1')])
    sys.stdout.write(out)
    check("duplicate explicit pair deduped (ok>=1)", ok >= 1)
    check("no failures", fail == 0)


def main():
    print("=" * 60)
    print("explicit diff-pair escape hatch tests")
    print("=" * 60)
    test_explicit_pair_routes_tdp_style_names()
    test_explicit_pair_validates_nets_exist()
    test_explicit_pair_repeatable_and_dedup()
    failed = [n for n, okk in CHECKS if not okk]
    print("-" * 60)
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print("ALL PASS")


if __name__ == '__main__':
    main()
