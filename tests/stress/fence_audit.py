#!/usr/bin/env python3
"""Audit a perturbed-corpus work dir for GROUND-TRUTH LEAKS, by content.

A recovery experiment measures how close a repaired placement lands to the
original. That number is only meaningful if the tools never saw the original.
The protocol fences the obvious carriers by NAME -- the control board, the
perturbation record, the human reference -- and a run-7 watcher found the hole
that naming leaves open:

    the STRIPPED board (the human placement with its copper removed) sat in
    every work dir. It is the perturbation's INPUT, so nobody thought of it as
    truth. Its footprint poses ARE the human placement, exactly.

Naming cannot close that hole, because the next carrier will have a different
name (a `.bak`, a `board_orig.kicad_pcb`, a candidate copied out of an archive).
So this audit ignores names and asks the only question that matters:

    does any board in this work dir carry the control's poses?

Two modes, because the same content means opposite things before and after a run:

  --mode create   Run at work-dir creation, BEFORE any tool touches the board.
                  Every board here is an INPUT. Any board whose poses match the
                  control (other than the explicitly allowed carriers) is a
                  leak -> exit 4. Writes `.fence-manifest.json` recording the
                  files present and their hashes.

  --mode audit    Run at watcher time, AFTER the run. A board matching the
                  control now has two possible causes, and the manifest tells
                  them apart:
                    * present at creation  -> LEAK (it was an input)
                    * created by the run   -> RECOVERED, which is the experiment
                      succeeding, not a breach (a perfect reconstruction lands
                      exactly on truth -- one run-7 board did, at d1 = 0.000000)
                  Exit 4 only for the first kind.

Match is on the pose set, not on file bytes: copper, UUIDs, netclasses and
zone fills all differ between a stripped board and its control while the
placement is identical.

Usage:
    python3 -X utf8 tests/stress/fence_audit.py --control C.kicad_pcb \\
        --workdir wk/run8/<board>/ --mode create
    python3 -X utf8 tests/stress/fence_audit.py --control C.kicad_pcb \\
        --workdir wk/run8/<board>/ --mode audit [--json]

Exit codes: 0 clean, 2 usage/IO problem, 4 leak found.
"""
import argparse
import fnmatch
import hashlib
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(, 'py_tools'))  # #522/py_placer layout
    os.path.abspath(__file__)))))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import recovery  # noqa: E402

MANIFEST_NAME = '.fence-manifest.json'

# Carriers the protocol declares as truth ON PURPOSE. They are audit-only
# inputs the workers may not read; this audit's job is finding the ones nobody
# declared, so these are excluded from the leak report by name.
DEFAULT_ALLOW = (
    '*.control.kicad_pcb',
    '*.perturb.json',
)

# A board counts as truth-carrying when this fraction of its shared refs sit
# within POSE_TOL of the control. Not 1.0: a leak is still a leak when one
# footprint was nudged, and a repaired board that genuinely reaches truth is
# separated by the manifest (see --mode audit), not by this threshold.
MATCH_FRAC = 0.98
POSE_TOL_MM = 1e-6
POSE_TOL_DEG = 1e-3


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def pose_match(poses_a, poses_b):
    """(match_frac, shared_ref_count, worst_mm) between two pose maps."""
    shared = sorted(set(poses_a) & set(poses_b))
    if not shared:
        return 0.0, 0, None
    hits = 0
    worst = 0.0
    for ref in shared:
        ax, ay, arot = poses_a[ref][0], poses_a[ref][1], poses_a[ref][2]
        bx, by, brot = poses_b[ref][0], poses_b[ref][1], poses_b[ref][2]
        d = math.hypot(ax - bx, ay - by)
        worst = max(worst, d)
        drot = abs((arot - brot) % 360.0)
        drot = min(drot, 360.0 - drot)
        if d <= POSE_TOL_MM and drot <= POSE_TOL_DEG:
            hits += 1
    return hits / len(shared), len(shared), worst


def scan(workdir, control_poses, allow):
    """Every .kicad_pcb in workdir, scored against the control's poses."""
    rows = []
    for root, _dirs, files in os.walk(workdir):
        for name in sorted(files):
            if not name.endswith('.kicad_pcb'):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, workdir).replace('\\', '/')
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat)
                   for pat in allow):
                continue
            try:
                poses = recovery.board_poses(parse_kicad_pcb(path))
            except Exception as exc:                    # unparseable is not a leak
                rows.append({'file': rel, 'error': str(exc)[:120]})
                continue
            frac, shared, worst = pose_match(poses, control_poses)
            rows.append({
                'file': rel,
                'match_frac': round(frac, 6),
                'refs_shared': shared,
                'worst_mm': None if worst is None else round(worst, 6),
                'carries_truth': frac >= MATCH_FRAC and shared > 0,
                'sha256': _sha256(path),
                'mtime': os.path.getmtime(path),
            })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--control', required=True,
                    help='the ground-truth board (the perturbation control)')
    ap.add_argument('--workdir', required=True, help='work dir to audit')
    ap.add_argument('--mode', choices=('create', 'audit'), default='audit')
    ap.add_argument('--allow', action='append', default=[],
                    help='extra glob of a DECLARED truth carrier (repeatable)')
    ap.add_argument('--json', action='store_true', help='machine-readable report')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.control):
        print(f'fence_audit: control not found: {args.control}', file=sys.stderr)
        return 2
    if not os.path.isdir(args.workdir):
        print(f'fence_audit: workdir not found: {args.workdir}', file=sys.stderr)
        return 2

    allow = list(DEFAULT_ALLOW) + list(args.allow)
    control_poses = recovery.board_poses(parse_kicad_pcb(args.control))
    # The control may itself live inside the work dir under any name; never
    # report the audit's own reference as a leak.
    control_real = os.path.realpath(args.control)

    rows = [r for r in scan(args.workdir, control_poses, allow)
            if os.path.realpath(os.path.join(args.workdir, r['file'])) != control_real]

    manifest_path = os.path.join(args.workdir, MANIFEST_NAME)
    manifest = None
    if args.mode == 'audit' and os.path.isfile(manifest_path):
        try:
            manifest = json.load(open(manifest_path, encoding='utf-8'))
        except Exception as exc:
            print(f'fence_audit: manifest unreadable ({exc}); every '
                  f'truth-carrying board is reported as a LEAK', file=sys.stderr)

    at_creation = set((manifest or {}).get('files', []))
    leaks, recovered, errors = [], [], []
    for row in rows:
        if 'error' in row:
            errors.append(row)
        elif not row['carries_truth']:
            continue
        elif args.mode == 'create':
            leaks.append(row)
        elif manifest is None or row['file'] in at_creation:
            row['reason'] = ('present at work-dir creation' if manifest
                             else 'no creation manifest -- provenance unknown')
            leaks.append(row)
        else:
            row['reason'] = 'produced by the run (recovery reaching truth)'
            recovered.append(row)

    if args.mode == 'create':
        with open(manifest_path, 'w', encoding='utf-8') as fh:
            json.dump({
                'control': os.path.relpath(args.control, args.workdir
                                           ).replace('\\', '/'),
                'control_sha256': _sha256(args.control),
                'allow': allow,
                'files': [r['file'] for r in rows],
                'shas': {r['file']: r.get('sha256') for r in rows},
            }, fh, indent=1, sort_keys=True)

    report = {
        'mode': args.mode,
        'workdir': args.workdir.replace('\\', '/'),
        'control': args.control.replace('\\', '/'),
        'boards_scanned': len(rows),
        'leaks': leaks,
        'recovered_to_truth': recovered,
        'unparseable': errors,
        'verdict': 'LEAK' if leaks else 'CLEAN',
    }
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(f'fence_audit [{args.mode}] {args.workdir}: '
              f'{len(rows)} board(s) scanned against {args.control}')
        for row in errors:
            print(f'  UNPARSEABLE {row["file"]}: {row["error"]}')
        for row in recovered:
            print(f'  ok  {row["file"]}: matches the control '
                  f'({row["match_frac"]:.3f}) -- {row["reason"]}')
        for row in leaks:
            print(f'  LEAK {row["file"]}: carries the control\'s placement '
                  f'({row["match_frac"]:.3f} of {row["refs_shared"]} refs '
                  f'within {POSE_TOL_MM}mm)'
                  + (f' -- {row["reason"]}' if row.get('reason') else ''))
        if leaks:
            print(f'  VERDICT: LEAK -- {len(leaks)} board(s) carry ground truth '
                  f'inside the work dir. Move them outside the fence before the '
                  f'run, or declare them with --allow.')
        else:
            print('  VERDICT: CLEAN (no undeclared board carries the control\'s '
                  'placement)')
    return 4 if leaks else 0


if __name__ == '__main__':
    sys.exit(main())
