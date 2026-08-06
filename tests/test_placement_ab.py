#!/usr/bin/env python3
"""Paired A/B for placement objective terms, graded by an INDEPENDENT check.

Every term in the quench objective is a claim about routability, and a term can
always be made to improve the number it is itself computed from. This runs the
same board through the optimizer twice -- flag off, flag on -- writes both, and
then grades both with `floorplan.grade(..., with_health=True)`, which
re-derives its corridors from the FINAL poses. The optimizer minimises against
rectangles frozen at construction; the grader does not use them. So a term that
only games its own model shows up here as "improved nothing".

Table-driven on purpose (`ROWS`): the next term under test adds a row, not a
file. Adding a row does not change how any existing row is judged.

The verdict is a PAIRED, DIRECTIONAL, NON-REGRESSION rule, never a per-board
absolute:

  * the claimed signal must improve on at least N-1 boards,
  * and must regress on NONE,
  * while `crossings` and `hpwl` -- the terms that already shipped -- do not
    get worse anywhere.

Unchanged boards count as neutral AND ARE PRINTED. A silently-dropped neutral
board is how a term with no effect on 3 of 4 boards reads as a clean sweep.

Usage:
    python3 -X utf8 tests/test_placement_ab.py            # the default table
    python3 -X utf8 tests/test_placement_ab.py --row corridor-ulx3s
    python3 -X utf8 tests/test_placement_ab.py --list
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARDS = os.path.join(ROOT, 'kicad_files')


# --- the table ------------------------------------------------------------
#
# `quench_on` is merged into the OFF kwargs to make the ON run, so a row states
# exactly one difference. `corridors` are the health.bus_corridors the intent
# declares; the grader reads the same declaration and re-derives the geometry.
#
# `signal` is the JSON_SUMMARY-style key the row claims to improve, and
# `guard` the keys that must not get worse. Lower is better for all of them.
# `expect` pins the mark that was MEASURED. Omit it for a term still on trial;
# a trial row must improve on >= N-1 boards and regress on none.
#
# `rejected` marks a term that was tried, measured, and NOT adopted. Its rows
# stay in the table as the evidence and as a change detector -- they are judged
# against their recorded marks rather than against "the term must help", so a
# rejected term does not leave a permanent red mark that someone eventually
# deletes along with the finding.
#
# The corridor globs name sub-buses SEPARATELY. Merging them halves the
# corridor's `cover` (address and data leave the part on different faces, so
# the endpoint average lands between them) and the resulting rectangle is a
# fiction: ulx3s SDRAM_A* scores cover 0.81, merged SDRAM_* scores 0.46. This
# is not a footnote -- the first run of this harness used the merged glob and
# reported the term INERT, because it had been pointed at a phantom.
ROWS = [
    {
        'name': 'corridor-ulx3s',
        'board': 'ulx3s.kicad_pcb',
        'corridors': [{'name': 'sdram_a', 'nets': ['SDRAM_A*'],
                       'width_mm': 8.0},
                      {'name': 'sdram_d', 'nets': ['SDRAM_D*'],
                       'width_mm': 8.0}],
        'ignore_nets': ['GND', '+3V3', '+5V'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'regress',
        'rejected': True,
        'why': ('MEASURED: the term moves the placement (crossings 2417 -> '
                '2390) but does NOT improve what it claims -- the re-derived '
                'bus_foreign_crossings went 62 -> 63 and hpwl 7512 -> 7634. '
                'The optimizer minimises against corridors frozen at '
                'construction; the grader re-derives them from the FINAL '
                'poses, and once the parts move the corridor moves with them, '
                'so the gain does not transfer. See '
                'docs/placement-optimization.md.'),
    },
    {
        'name': 'corridor-orangecrab',
        'board': 'orangecrab_ext_pll.kicad_pcb',
        'corridors': [{'name': 'ram_d', 'nets': ['RAM_D*'], 'width_mm': 6.0},
                      {'name': 'ram_a', 'nets': ['RAM_A*'], 'width_mm': 6.0}],
        'ignore_nets': ['GND', '+3V3', '+1V1', 'VCC*'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'improve',
        'rejected': True,
        'why': ('MEASURED: bus_foreign_crossings 32 -> 31 with both guards '
                'improved (crossings 1063 -> 1042, hpwl 2117.9 -> 2113.7). '
                'RE-PINNED at the placement-branch merge: the pad+drill '
                'legality layer (default on) changed the quench trajectory on '
                'this board and flipped the original regress mark '
                '(32 -> 33, one more intent violation). The term stays '
                'rejected -- 2 of 3 boards still disagree and the '
                'frozen-vs-re-derived divergence stands; this row is the '
                'change detector doing its job.'),
    },
    {
        'name': 'corridor-coldfire',
        'board': 'kit-dev-coldfire-xilinx_5213.kicad_pcb',
        'corridors': [{'name': 'an', 'nets': ['AN*'], 'width_mm': 8.0},
                      {'name': 'bdm', 'nets': ['DDAT*', 'PST*'],
                       'width_mm': 8.0}],
        'ignore_nets': ['GND', 'VCC*', '+3.3V', '+5V'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'improve',
        'rejected': True,
        'why': ('MEASURED: the one board of three where it helped '
                '(bus_foreign_crossings 148 -> 143, and both guards improved). '
                'Kept in the table BECAUSE it disagrees with the other two -- '
                'a term that helps on one board of three is not a term, and '
                'deleting the row that disagrees is how that gets forgotten.'),
    },
]

QUENCH_BASE = dict(
    max_displacement=3.0, step=1.0, grid_step=0.1, clearance=0.2,
    board_edge_clearance=0.55, crossing_penalty=30.0, length_weight=0.3,
    halo_base=0.5, halo_coef=0.15, halo_weight=2.0, edge_halo=2.0,
    edge_weight=2.0, max_passes=4, verbose=False)


def _intent_for(board_path, corridors, workdir):
    """An intent for `board_path` with `corridors` declared.

    Emitted from the board itself rather than hand-written, so the blocks the
    health signals need are the ones that board actually has, and the row only
    has to state the bus.
    """
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan
    path = os.path.join(workdir, 'intent.json')
    doc = floorplan.emit_intent(parse_kicad_pcb(board_path), board_path)
    doc['health'] = dict(doc.get('health') or {})
    doc['health']['bus_corridors'] = corridors
    with open(path, 'w') as fh:
        json.dump(doc, fh, indent=2)
    return floorplan.load_intent(path)


def _run(board_path, out_path, intent, corridors, quench_kw):
    """One quench + write + independent grade. Returns the measured row."""
    from kicad_parser import parse_kicad_pcb
    from placement.quench import quench
    from placement.writer import write_placed_output
    from placement import floorplan

    pcb = parse_kicad_pcb(board_path)
    metrics = {}
    t0 = time.time()
    placements = quench(pcb, pcb_file=board_path, metrics_out=metrics,
                        **quench_kw)
    write_placed_output(board_path, out_path, placements)
    for ext in ('.kicad_pro', '.kicad_dru'):
        src = os.path.splitext(board_path)[0] + ext
        if os.path.exists(src):
            shutil.copy2(src, os.path.splitext(out_path)[0] + ext)

    # The independent half: parse what was WRITTEN and grade it. The corridors
    # here come from the final poses, not from the frozen model the optimizer
    # minimised against.
    graded = parse_kicad_pcb(out_path)
    result = floorplan.grade(intent, graded, out_path, with_health=True)
    summary = floorplan.summary(result)
    after = metrics.get('after') or {}
    return {
        'seconds': round(time.time() - t0, 1),
        'crossings': after.get('crossings'),
        'hpwl': round(float(after.get('hpwl') or 0.0), 2),
        'corridor_cut': round(float(after.get('corridor_cut') or 0.0), 2),
        'health_bus_foreign_crossings':
            summary.get('health_bus_foreign_crossings'),
        'health_blocks_displaced': summary.get('health_blocks_displaced'),
        'intent_errors': summary.get('errors'),
    }


def _verdict(off, on, row):
    """Direction on one board. Returns (mark, notes)."""
    key = row['signal']
    a, b = off.get(key), on.get(key)
    notes = []
    if a is None or b is None:
        return 'skip', [f"{key} not measured on this board"]
    for g in row['guard']:
        ga, gb = off.get(g), on.get(g)
        if ga is not None and gb is not None and gb > ga + 1e-9:
            notes.append(f"GUARD {g} worsened {ga} -> {gb}")
    # PAIRED, not absolute. Both runs quench the board, so both walk parts out
    # of the zones the emitted intent recorded; grading the ON run against zero
    # errors would mark every row a regression for a reason the flag did not
    # cause. Only errors the flag ADDS are its fault.
    ea, eb = off.get('intent_errors') or 0, on.get('intent_errors') or 0
    if eb > ea:
        notes.append(f"intent errors {ea} -> {eb}")
    if notes:
        return 'regress', notes
    if b < a:
        return 'improve', [f"{key} {a} -> {b}"]
    if b > a:
        return 'regress', [f"{key} {a} -> {b}"]
    return 'neutral', [f"{key} unchanged at {a}"]


def run_row(row, workdir, keep=False):
    board = os.path.join(BOARDS, row['board'])
    if not os.path.exists(board):
        print(f"  SKIP {row['name']}: no {row['board']} in kicad_files/")
        return 'skip', None, None
    d = os.path.join(workdir, row['name'])
    os.makedirs(d, exist_ok=True)
    intent = _intent_for(board, row['corridors'], d)

    kw_off = dict(QUENCH_BASE)
    kw_off['ignore_nets'] = list(row.get('ignore_nets') or ())
    kw_on = dict(kw_off)
    kw_on.update(row['quench_on'])
    # The ON run needs the corridors the flag prices; the OFF run must NOT get
    # them, or "off" would mean "built and multiplied by zero" rather than
    # "the objective that shipped".
    if 'corridor_weight' in row['quench_on']:
        kw_on['corridor_specs'] = row['corridors']

    off = _run(board, os.path.join(d, 'off.kicad_pcb'), intent,
               row['corridors'], kw_off)
    on = _run(board, os.path.join(d, 'on.kicad_pcb'), intent,
              row['corridors'], kw_on)
    mark, notes = _verdict(off, on, row)
    expected = row.get('expect')
    tag = mark.upper()
    if expected and mark == expected:
        tag = f"{mark.upper()} (as measured)"
    elif expected:
        tag = f"{mark.upper()} != expected {expected.upper()}"
    print(f"  {row['name']:<24} {tag:<32} "
          f"({off['seconds']}s / {on['seconds']}s)")
    for k in ('crossings', 'hpwl', row['signal'], 'corridor_cut'):
        print(f"      {k:<32} {off.get(k)!s:>12} -> {on.get(k)!s:>12}")
    for n in notes:
        print(f"      {n}")
    if expected and mark != expected and row.get('why'):
        print(f"      recorded reason: {row['why']}")
    return mark, off, on


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--row', action='append',
                   help='Run only these table rows (repeatable)')
    p.add_argument('--list', action='store_true', help='List rows and exit')
    p.add_argument('--workdir', default=None,
                   help='Where to write boards (default: a temp dir)')
    args = p.parse_args(argv)

    if args.list:
        for r in ROWS:
            print(f"{r['name']:<24} {r['board']:<28} "
                  f"{r['quench_on']} -> {r['signal']}")
        return 0

    rows = [r for r in ROWS if not args.row or r['name'] in args.row]
    if not rows:
        print(f"no such row; try --list", file=sys.stderr)
        return 2

    workdir = args.workdir or tempfile.mkdtemp(prefix='placement_ab_')
    os.makedirs(workdir, exist_ok=True)
    print(f"A/B in {workdir}\n")

    marks = {}
    for r in rows:
        mark, _off, _on = run_row(r, workdir)
        marks[r['name']] = mark

    print()
    tally = {m: [n for n, v in marks.items() if v == m]
             for m in ('improve', 'neutral', 'regress', 'skip')}
    # Printed, not dropped: a term with no effect on 3 of 4 boards must not
    # read as a clean sweep.
    for m in ('improve', 'neutral', 'regress', 'skip'):
        if tally[m]:
            print(f"{m:<9} {len(tally[m])}: {', '.join(tally[m])}")

    # Rows carrying a measured `expect` are judged against THAT, not against
    # "the term must help": a term proven inert is a result, and re-deriving it
    # on every run is what makes people delete the check.
    pinned = {r['name']: r['expect'] for r in rows if r.get('expect')}
    trial = [r for r in rows if not r.get('expect')]
    mismatched = [n for n, e in pinned.items() if marks.get(n) != e]
    rejected = [r['name'] for r in rows if r.get('rejected')]

    ok = not mismatched
    if rejected:
        print(f"rejected: {len(rejected)} row(s) record a term that was tried "
              f"and NOT adopted ({', '.join(rejected)}). They are judged "
              f"against their recorded marks, not against 'the term helps'.")
    if trial:
        t_imp = [r['name'] for r in trial if marks.get(r['name']) == 'improve']
        t_reg = [r['name'] for r in trial if marks.get(r['name']) == 'regress']
        judged = len([r for r in trial if marks.get(r['name']) != 'skip'])
        ok = ok and not t_reg and len(t_imp) >= max(1, judged - 1)
        print(f"on trial: improved {len(t_imp)}/{judged}, "
              f"regressed {len(t_reg)} (rule: improve >= N-1, regress == 0)")
    if pinned:
        print(f"pinned:   {len(pinned) - len(mismatched)}/{len(pinned)} rows "
              f"match their measured verdict"
              + (f"; MISMATCH: {', '.join(mismatched)}" if mismatched else ""))
    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
