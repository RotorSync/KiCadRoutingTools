#!/usr/bin/env python3
"""#521 carry gap: protection recording must not be coupled to DRC-floor fixing.

The owner-reported defect: "the tool records protected nets per-run, and the
CALLER must union them across a chain or the bulk step drives through prior
diff-pair copper." Investigation found two concrete gaps that force that manual
union:

1. `passthrough_copy` (the "nothing to route" early-return path in route.py /
   route_diff.py / the fanout tools) copied ONLY the .kicad_pcb, stranding the
   sibling .kicad_pro -- so the next chain step read no floor and no #521
   protections (#441: never cp a board without its project).

2. The #521 writeback in route.py / route_diff.py main() sat INSIDE the
   `if not args.no_fix_drc_settings` guard, so a step that routed pairs under
   --no-fix-drc-settings recorded NO protections at all.

Both are fixed: passthrough_copy now carries the .kicad_pro/.kicad_dru siblings,
and the protection writeback is decoupled from DRC-floor fixing. This test pins
both behaviors.

Run:
    python3 tests/test_protected_carry_no_fix.py [-v]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _p in ('py_router', 'py_placer'):
    _d = os.path.join(ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)

BOARD = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
DIFF_GEOM = ['--track-width', '0.2', '--diff-pair-gap', '0.25',
             '--clearance', '0.1778', '--via-size', '0.45', '--via-drill', '0.3',
             '--no-gnd-vias']


def _run(args, verbose):
    r = subprocess.run([sys.executable, '-X', 'utf8', *args], cwd=ROOT,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    txt = r.stdout + r.stderr
    if verbose:
        print(txt)
    return r.returncode, txt


def _read_protected(pro_path):
    if not pro_path or not os.path.isfile(pro_path):
        return {}
    try:
        with open(pro_path, encoding='utf-8') as f:
            d = json.load(f)
        return (d.get('kicad_routing_tools') or {}).get('protected_nets') or {}
    except Exception:
        return {}


def _seg_count(path, net_name):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(path)
    ids = {i for i, n in pcb.nets.items() if n.name == net_name}
    return sum(1 for s in pcb.segments if s.net_id in ids)


def test_passthrough_carries_siblings(verbose):
    """passthrough_copy must carry .kicad_pro/.kicad_dru (#441)."""
    from pcb_io_utils import passthrough_copy
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 'a.kicad_pcb')
        pro = os.path.join(td, 'a.kicad_pro')
        dru = os.path.join(td, 'a.kicad_dru')
        open(src, 'w').write('pcb')
        open(pro, 'w').write('{"kicad_routing_tools": {"protected_nets": {"X": "diff-pair"}}}')
        open(dru, 'w').write('rules')
        dst = os.path.join(td, 'b.kicad_pcb')
        assert passthrough_copy(src, dst) is True
        assert os.path.exists(dst)
        assert os.path.exists(os.path.join(td, 'b.kicad_pro')),             'passthrough_copy stranded the .kicad_pro (#441)'
        assert os.path.exists(os.path.join(td, 'b.kicad_dru')),             'passthrough_copy stranded the .kicad_dru'
        # existing dst sibling must NOT be clobbered
        open(os.path.join(td, 'b2.kicad_pro'), 'w').write('{"mine": true}')
        dst2 = os.path.join(td, 'b2.kicad_pcb')
        passthrough_copy(src, dst2)
        assert open(os.path.join(td, 'b2.kicad_pro')).read() == '{"mine": true}',             'passthrough_copy clobbered an existing output .kicad_pro'
        # same-file skip
        assert passthrough_copy(src, src) is False
    if verbose:
        print('  passthrough_copy carries siblings: PASS')


def test_no_fix_records_protections(verbose):
    """A diff step under --no-fix-drc-settings must still record protections."""
    if not os.path.isfile(BOARD):
        print('  SKIP: fixture missing')
        return
    with tempfile.TemporaryDirectory() as td:
        staged = os.path.join(td, 'in.kicad_pcb')
        shutil.copyfile(BOARD, staged)
        # seed a project with one prior protection to prove union-carry
        pro = os.path.join(td, 'in.kicad_pro')
        with open(pro, 'w') as f:
            json.dump({'kicad_routing_tools': {'protected_nets': {'PRIOR': 'user'}}}, f)
        out = os.path.join(td, 'd1.kicad_pcb')
        rc, txt = _run(['py_router/route_diff.py', staged, out,
                        '--nets', '/D_P', '/D_N', *DIFF_GEOM,
                        '--no-fix-drc-settings'], verbose)
        assert rc == 0, f'route_diff failed: {txt[-2000:]}'
        out_pro = os.path.join(td, 'd1.kicad_pro')
        assert os.path.isfile(out_pro),             'output .kicad_pro missing after --no-fix-drc-settings run'
        prot = _read_protected(out_pro)
        assert prot.get('PRIOR') == 'user',             f'prior protection not carried: {prot}'
        assert prot.get('/D_P') == 'diff-pair' and prot.get('/D_N') == 'diff-pair',             f'routed pair not protected under --no-fix-drc-settings: {prot}'
    if verbose:
        print('  --no-fix-drc-settings records protections: PASS')


def test_bulk_respects_carried_protection(verbose):
    """A bulk step must not rip copper protected by a prior --no-fix step."""
    if not os.path.isfile(BOARD):
        print('  SKIP: fixture missing')
        return
    with tempfile.TemporaryDirectory() as td:
        staged = os.path.join(td, 'in.kicad_pcb')
        shutil.copyfile(BOARD, staged)
        pro = os.path.join(td, 'in.kicad_pro')
        with open(pro, 'w') as f:
            json.dump({'kicad_routing_tools': {'protected_nets': {}}}, f)
        d1 = os.path.join(td, 'd1.kicad_pcb')
        rc, txt = _run(['py_router/route_diff.py', staged, d1,
                        '--nets', '/D_P', '/D_N', *DIFF_GEOM,
                        '--no-fix-drc-settings'], verbose)
        assert rc == 0, f'route_diff failed: {txt[-2000:]}'
        d_before = _seg_count(d1, '/D_P') + _seg_count(d1, '/D_N')
        assert d_before > 0, 'setup: diff pair routed no copper'

        routed = os.path.join(td, 'routed.kicad_pcb')
        rc2, txt2 = _run(['py_router/route.py', d1, routed,
                          '--nets', '/D_P', '/D_N',
                          '--clearance', '0.1778', '--track-width', '0.2',
                          '--via-size', '0.45', '--via-drill', '0.3',
                          '--rip-existing-nets', '/D*'], verbose)
        assert rc2 == 0, f'route.py failed: {txt2[-2000:]}'
        # the protected pair must survive the bulk rip (copper preserved)
        d_after = _seg_count(routed, '/D_P') + _seg_count(routed, '/D_N')
        assert d_after >= d_before,             f'bulk step ripped protected diff-pair copper: before={d_before} after={d_after}'
    if verbose:
        print('  bulk step respects carried protection: PASS')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    fails = []
    for fn in (test_passthrough_carries_siblings,
               test_no_fix_records_protections,
               test_bulk_respects_carried_protection):
        try:
            fn(args.verbose)
        except AssertionError as e:
            fails.append(f'{fn.__name__}: {e}')
        except Exception as e:
            fails.append(f'{fn.__name__}: {type(e).__name__}: {e}')
    if fails:
        print('FAIL: ' + '; '.join(fails))
        return 1
    print('PASS: protected-nets carry survives --no-fix-drc-settings and '
          'nothing-to-route passthrough')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
