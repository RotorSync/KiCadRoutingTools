#!/usr/bin/env python3
"""Issue #630's own pin test, against the tool the issue names.

    "`place_seed` on a pile fixture must seat a part whose only pocket is
     blocked by one movable incumbent, without hand intervention."

Not `place_plan`, not a library call -- `place_seed`, because that is the
tool run 19 was using when it returned "no legal pose anywhere on the board"
three times and a teammate went off and wrote 221 lines of arithmetic.

The block is a THEOREM, not an observation. On a 16 x 14 board at 0.5mm edge
clearance, BIG's 10x10 courtyard confines its centre to x in [5.5, 10.5] and
y in [5.5, 8.5]. Clearing SMALL's 1x1 courtyard at the zone centre by 0.2mm
needs |cx - 8| >= 5.7 or |cy - 7| >= 5.7, and neither interval intersects
BIG's legal range. So while SMALL sits there BIG has exactly zero legal
poses, and a full rim of them once it moves.

Both parts are declared into the SAME zone, so the seeder packs them by
descending pin count -- SMALL first, because it has more pads. That is not a
contrivance: it is the ordering that produced run 19's failure (34 six-pad
diodes seated before 34 fifteen-millimetre switches, and the smalls took the
centre).

WHAT IS ASSERTED, AND WHY IT IS THE OUTPUT FILE. The first version of this
file checked `unseated == 0 and placed == 2` and passed while SMALL sat 100%
inside BIG's courtyard: the rung had re-seated the blocker with the part it
had just seated still in its exclude set. A count of seated parts is not a
board. So every accepting case here parses the WRITTEN board and proves
pairwise legality with the legality module and `candidate_valid`, never the
JSON. The JSON is checked too, for what it claims.

Two of the checks below are MUTATION KILLS, kept because the gate they
guard is cheap to weaken silently:
  * "the gate does not trust `_try_place`" injects the fault that shipped
    (the blocker returned on top of the part, reported as a seat) and
    demands a revert -- dropping the legality conjunct from
    `_evict_trade`'s acceptance turns it red;
  * the pin test's pairwise-legality check turns red if the blocker is
    re-seated with the part excluded again.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO,):
    if p not in sys.path:
        sys.path.insert(0, p)
        sys.path.insert(0, os.path.join(p, 'py_router'))
        sys.path.insert(0, os.path.join(p, 'py_tools'))
        sys.path.insert(0, os.path.join(p, 'py_placer'))

from kicad_parser import parse_kicad_pcb                        # noqa: E402
from placement import legality, seeder                          # noqa: E402
import pose_score                                               # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


def _part(ref, x, y, half_w, half_h, npads, pad_y=0.0):
    """A footprint with a (2*half_w x 2*half_h) courtyard and `npads` pads in
    a row at local y=`pad_y`. Pad 1 is on N1, the rest on N2."""
    pads = ''.join(
        f'\t\t(pad "{i + 1}" smd rect\n\t\t\t(at {i * 0.2 - 0.2} {pad_y})\n'
        f'\t\t\t(size 0.3 0.3)\n\t\t\t(layers "F.Cu")\n'
        f'\t\t\t(net {1 if i == 0 else 2} "N{1 if i == 0 else 2}")\n'
        f'\t\t\t(uuid "p{i}-{ref}")\n\t\t)\n' for i in range(npads))
    return f'''\t(footprint "test:P{ref}"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(fp_rect
\t\t\t(start {-half_w} {-half_h})
\t\t\t(end {half_w} {half_h})
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cy-{ref}")
\t\t)
{pads}\t)
'''


def board(path, parts, size=(16, 14)):
    """Every part stacked at one coordinate: the place-from-scratch task."""
    body = ('(kicad_pcb\n\t(version 20241229)\n'
            '\t(net 0 "")\n\t(net 1 "N1")\n\t(net 2 "N2")\n'
            '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end {} {})\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "e1")\n\t)\n'.format(*size)
            + ''.join(parts) + ')\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)


def pile_board(path):
    # SMALL has MORE pads than BIG, so the zone packer seats it first.
    board(path, [_part('BIG', 8, 7, 5.0, 5.0, 2),
                 _part('SMALL', 8, 7, 0.5, 0.5, 4)])


def pair_block_board(path):
    """#699: TWO blockers, neither of which is in the way BY ITSELF.

    The block is a THEOREM again. On the same 16 x 14 board at 0.5mm edge
    clearance, BIG's 10x10 courtyard confines its centre to x in [5.5, 10.5],
    y in [5.5, 8.5]. For every centre in that range |cx - 7| and |cx - 9| are
    both <= 5.5, so the x-gap to either small is <= 0 and legality reduces to
    the y axis: |cy - 7| >= 5.7, i.e. cy <= 1.3 or cy >= 12.7, which the range
    excludes. So S1 ALONE blocks every pose, and so does S2 -- lifting either
    one frees exactly zero, which is the verdict issue #699 reports as
    "immovable". Lifting BOTH frees the whole rim.

    (`rect_gap` is euclidean when both axes separate, so "|dx| >= 5.7 or
    |dy| >= 5.7" is SUFFICIENT for legality, not necessary -- the corner case
    |dx| = |dy| = 5.642 is legal too. It does not arise here because the x
    condition is already unreachable, but the lemma is one-directional and
    must not be copied into a tighter fixture.)

    With BIG seated at (8, 7) its courtyard is [3,13] x [2,12], so the smalls
    have the whole left and right strips to return to (centre x <= 2.3 or
    >= 13.7). The pair trade is therefore ACCEPTED, and the fixture tests the
    accepting path rather than a revert dressed up as one.

    BIG is 100mm2 of a 224mm2 board = 44.6%, under CONTAINER_RATIO, so it
    stays a real obstacle rather than being skipped as a frame.
    """
    board(path, [_part('BIG', 8, 7, 5.0, 5.0, 2),
                 _part('S1', 7, 7, 0.5, 0.5, 4),
                 _part('S2', 9, 7, 0.5, 0.5, 4)])


def two_bigs_board(path):
    """Two 10x10 courtyards on a 16x14 board: whichever seats first leaves
    the other no pose, and lifting it frees plenty -- but there is no pose
    to put it BACK to with the other in place (20.2mm of courtyard on a
    15mm usable span, either axis). The only trade must revert."""
    board(path, [_part('BIG1', 8, 7, 5.0, 5.0, 2),
                 _part('BIG2', 8, 7, 5.0, 5.0, 4)])


EDGE_SIZE = (20, 14)


def _big_pad_part(ref, x, y, half_cy, pads):
    """A courtyard of +/-half_cy with ARBITRARY pads: (num, net, px, py, sx, sy)."""
    body = ''.join(
        f'\t\t(pad "{n}" smd rect\n\t\t\t(at {px} {py})\n'
        f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "F.Cu")\n'
        f'\t\t\t(net {net} "N{net}")\n\t\t\t(uuid "p{n}-{ref}")\n\t\t)\n'
        for n, net, px, py, sx, sy in pads)
    return f'''	(footprint "test:P{ref}"
		(layer "F.Cu")
		(uuid "fp-{ref}")
		(at {x} {y})
		(property "Reference" "{ref}"
			(at 0 0)
		)
		(fp_rect
			(start {-half_cy} {-half_cy})
			(end {half_cy} {half_cy})
			(layer "F.CrtYd")
			(uuid "cy-{ref}")
		)
{body}	)
'''


def pad_overhang_board(path):
    """The pin geometry (BIG 10x10, SMALL 1x1, SMALL seats first), but the
    COPPER overhangs both courtyards by 1.0mm on every side: BIG carries one
    12x12 pad on N1, SMALL two 1.5x3 pads (N1 | N2). The courtyards are what
    the seat search clears, so SMALL's nearest re-seat sits 0.2mm outside
    BIG's courtyard with 1.8mm of its copper over BIG's pad -- a pad-only
    intersection, no courtyard overlap. `candidate_valid` tolerates it: both
    parts start stacked at one coordinate, which is a bucket of TWO (not the
    three that make a seed degenerate), so the pair's SEED baseline already
    holds a larger pad intersection and `pads_ok` is "no worse than the
    seed". Conjunct 2 therefore passes; only conjunct 3's absolute census
    refuses the trade."""
    board(path, [_big_pad_part('BIG', 8, 7, 5.0,
                               [(1, 1, 0, 0, 12.0, 12.0)]),
                 _big_pad_part('SMALL', 8, 7, 0.5,
                               [(1, 1, -0.75, 0, 1.5, 3.0),
                                (2, 2, 0.75, 0, 1.5, 3.0)])])


def edge_board(path):
    """J1 is a declared south-edge connector (4 x 3 courtyard, pads 0.5mm
    inside its inner edge). With a 0..1mm band the overhang walk puts it
    0.5mm past the edge-clearance inset, i.e. flush with the outline: centre
    y=12.5, courtyard y 11.0..14.0. BIG is 11 x 11 on a 20 x 14 board: its
    centre must lie in y [6.0, 8.0] at either rotation, and clearing J1 needs
    y + 5.5 <= 10.8. No pose -- and lifting J1 frees one. The board is 20 wide so BIG (121mm2)
    stays under the 50% container threshold (a container is skipped as an
    obstacle, which is what made a 16 x 14 version of this fixture vacuous)
    AND so that J1, rotated, WOULD fit inland beside BIG: an eviction of J1
    would succeed here, which is what makes the pose assertion below
    load-bearing rather than a revert dressed up as a refusal."""
    board(path, [_part('BIG', 10, 7, 5.5, 5.5, 2),
                 _part('J1', 10, 7, 2.0, 1.5, 2, pad_y=-1.0)],
          size=EDGE_SIZE)


def intent_for(refs, edge=None, size=(16, 14)):
    w, h = float(size[0]), float(size[1])
    it = {"schema": 1, "kind": "floorplan-intent", "units": "mm",
          "envelope": {"rect": [0.0, 0.0, w, h], "tolerance_mm": 0.5},
          "blocks": [{"name": "everything", "refs": list(refs),
                      "zone": [0.5, 0.5, w - 0.5, h - 0.5],
                      "tolerance_mm": 0.5}]}
    if edge:
        it["edge_connectors"] = [edge]
    return it


def run(workdir, make_board, intent, *extra, tag='seed'):
    bpath = os.path.join(workdir, f'{tag}-in.kicad_pcb')
    ipath = os.path.join(workdir, f'{tag}-fp.json')
    out = os.path.join(workdir, f'{tag}-out.kicad_pcb')
    make_board(bpath)
    with open(ipath, 'w', encoding='utf-8') as f:
        json.dump(intent, f)
    r = subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join('py_placer', 'place_seed.py'), bpath, out,
         '--intent', ipath, '--clearance', '0.2',
         '--board-edge-clearance', '0.5', '--no-polish', *extra],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=REPO, timeout=900,
        env=dict(os.environ, PYTHONHASHSEED='0', PYTHONIOENCODING='utf-8'))
    summary = None
    for line in r.stdout.splitlines():
        if line.startswith('JSON_SUMMARY:'):
            summary = json.loads(line.split(':', 1)[1])
    return r, summary, out


def poses(path):
    pcb = parse_kicad_pcb(path)
    return {ref: (round(fp.x, 3), round(fp.y, 3), round(fp.rotation, 1))
            for ref, fp in sorted(pcb.footprints.items())}


def pairwise_legal(path):
    """(ok, detail) -- every pair of the WRITTEN board clear of every other,
    by the legality module's courtyard census AND by `candidate_valid`
    against the full part set (no exclude) at the run's own floors."""
    pcb = parse_kicad_pcb(path)
    pairs = legality.body_overlap_pairs(
        legality.graded_parts_from_file(pcb, path))
    st = pose_score.make_state(pcb, path, clearance=0.2,
                               board_edge_clearance=0.5, grid_step=0.1)
    bad = []
    for ref in sorted(st.parts):
        p = st.parts[ref]
        if not st.candidate_valid(ref, p.x, p.y, p.rot, exclude=set()):
            bad.append(ref)
    detail = (f"courtyard overlaps {[(q.a, q.b, q.area_mm2) for q in pairs]}, "
              f"candidate_valid rejects {bad}")
    return (not pairs and not bad), detail


# --------------------------------------------------------------------------
# THE PIN TEST (depth 1, opt-in)
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    proc, s, out = run(wd, pile_board, intent_for(['BIG', 'SMALL']),
                       '--evict-depth', '1')
    check("place_seed runs", s is not None,
          (proc.stdout[-600:] + proc.stderr[-600:]))
    if s:
        check("#630: it seats BOTH parts, with no hand intervention",
              s['unseated'] == 0 and s['placed'] == 2,
              f"placed {s['placed']}, unseated {s['unseated']} "
              f"{s.get('unseated_refs')}")
        check("and exits 0 rather than 4", proc.returncode == 0,
              f"rc={proc.returncode}")
        check("the JSON counts one KEPT eviction and no reverted one",
              s.get('evictions') == 1 and s.get('evictions_reverted') == 0,
              f"evictions {s.get('evictions')} reverted "
              f"{s.get('evictions_reverted')}")
    ok, detail = pairwise_legal(out)
    check("THE BOARD: every pair of the written output is legal (the first "
          "version shipped SMALL 100% inside BIG and said unseated 0)",
          ok, detail)
    check("the run says which part it evicted, and what that freed",
          'evicting' in proc.stdout and 'poses at its target' in proc.stdout,
          str([l for l in proc.stdout.splitlines() if "evict" in l][:2]))
    check("the eviction is RECORDED with the violation measure either side",
          any('evict' in l and 'violations' in l and 'hpwl' in l
              for l in proc.stdout.splitlines()),
          str([l for l in proc.stdout.splitlines() if 'evict' in l][:3]))

# --------------------------------------------------------------------------
# The default is OFF: census only, nothing moves (opt-in per CLAUDE.md until
# an A/B row exists)
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    proc, s, out = run(wd, pile_board, intent_for(['BIG', 'SMALL']))
    check("with no flag, the seed fails the way it always did (default 0)",
          s and s['unseated'] == 1 and proc.returncode == 4,
          f"unseated {s and s['unseated']}, rc={proc.returncode}")
    check("and nothing was evicted",
          s and s.get('evictions') == 0 and 'evicting' not in proc.stdout,
          str(s and s.get('evictions')))
    check("#629: the JSON_SUMMARY names the unseated ref, not just a count",
          s and s.get('unseated_refs') == ['BIG'],
          str(s and s.get('unseated_refs')))
    check("#629: `no_pose_blockers` is in the JSON_SUMMARY",
          s and 'no_pose_blockers' in s, str(sorted(s or {})))
    check("#629: it names the blocker and the poses that blocker frees",
          s and s.get('no_pose_blockers', {}).get('BIG', {}).get('SMALL', 0)
          > 0,
          str(s and s.get('no_pose_blockers')))
    check("the bare verdict is still printed, now with the census beside it",
          'no legal pose' in proc.stdout and 'would free' in proc.stdout,
          proc.stdout[-300:])
    proc2, s2, out2 = run(wd, pile_board, intent_for(['BIG', 'SMALL']),
                          '--evict-depth', '0', tag='d0')
    check("--evict-depth 0 is the same as the default",
          s2 and s2['unseated'] == 1 and poses(out2) == poses(out),
          f"{s2 and s2['unseated']} {poses(out2)} vs {poses(out)}")
    proc3, s3, out3 = run(wd, pile_board, intent_for(['BIG', 'SMALL']),
                          '--evict-depth', '3', tag='d3')
    check("--evict-depth 3 is refused (nothing deeper is defined)",
          proc3.returncode == 2 and s3 is None,
          f"rc={proc3.returncode}")
    # #699: depth 2 is ACCEPTED now, and on a board a SINGLE lift already
    # solves it must behave exactly like depth 1 -- the pair sweep only runs
    # when no single lift frees a pose, so it never fires here.
    proc4, s4, out4 = run(wd, pile_board, intent_for(['BIG', 'SMALL']),
                          '--evict-depth', '2', tag='d2')
    proc5, s5, out5 = run(wd, pile_board, intent_for(['BIG', 'SMALL']),
                          '--evict-depth', '1', tag='d1')
    check("#699: --evict-depth 2 is accepted, not refused",
          proc4.returncode == 0 and s4 is not None,
          f"rc={proc4.returncode}")
    check("#699: depth 2 on a single-blocker board is depth 1 exactly "
          "(the pair sweep does not run when one lift is enough)",
          s4 and s5 and poses(out4) == poses(out5)
          and s4.get('evictions') == 1 and s5.get('evictions') == 1,
          f"{poses(out4)} vs {poses(out5)}")

# --------------------------------------------------------------------------
# #699 THE PAIR PIN TEST (depth 2). A part two neighbours JOINTLY block is
# reported immovable by a rung that only ever lifts one of them -- and that
# verdict is true only of the basin the board happens to be in.
#
# Driven at the library level, and that is forced, not a shortcut: the
# fixture needs S1/S2 at KNOWN coordinates with only BIG to seat, which is
# `seed_refs`, and `place_seed` has no --seed-refs (it derives the scope from
# pile detection, and three parts at three distinct coordinates is not a
# pile). The written board is still what the accepting case is graded on.
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    import random as _rnd
    from placement import floorplan as _fp
    from placement.writer import write_placed_output as _wpo
    bpath = os.path.join(wd, 'pair.kicad_pcb')
    ipath = os.path.join(wd, 'pair-fp.json')
    pair_block_board(bpath)
    with open(ipath, 'w', encoding='utf-8') as f:
        json.dump(intent_for(['BIG', 'S1', 'S2']), f)
    pintent = _fp.load_intent(ipath)

    def _seed(depth):
        return seeder.seed_from_intent(
            parse_kicad_pcb(bpath), bpath, pintent, _rnd.Random("0"),
            clearance=0.2, board_edge_clearance=0.5, grid_step=0.1,
            seed_refs={'BIG'}, evict_depth=depth)

    r1 = _seed(1)
    # THE CONTROL, and without it the depth-2 row below proves nothing: it is
    # what says the pair sweep did the work rather than a single lift.
    check("#699: the fixture reproduces the issue -- at depth 1 BIG is "
          "unseated and NEITHER neighbour frees a pose on its own",
          r1['unseated'] == ['BIG']
          and r1['no_pose_blockers'].get('BIG') == {'S1': 0, 'S2': 0}
          and not r1['evictions'],
          f"unseated {r1['unseated']} blockers {r1['no_pose_blockers']} "
          f"evictions {r1['evictions']}")
    check("#699: and the depth-1 note claims only what a k=1 sweep measured "
          "(not 'they are not what is in the way')",
          any('lifting any ONE of them frees no pose' in n
              for n in r1['notes'])
          and not any('not what is in the way' in n for n in r1['notes']),
          str([n for n in r1['notes'] if 'censused' in n]))

    r2 = _seed(2)
    ev = r2['evictions']
    check("#699: at depth 2 BIG is seated", r2['unseated'] == [],
          f"unseated {r2['unseated']}")
    check("#699: by ONE trade that lifted the PAIR, recorded as depth 2",
          len(ev) == 1 and ev[0]['accepted'] is True
          and sorted(ev[0]['blockers']) == ['S1', 'S2']
          and ev[0]['depth'] == 2,
          str(ev))
    check("#699: the record names what the trade COST each evicted part",
          len(ev) == 1 and sorted(ev[0].get('moved') or {}) == ['S1', 'S2']
          and all(d > 0 for d in (ev[0].get('moved') or {}).values()),
          str(ev and ev[0].get('moved')))
    check("#699: and the run SAYS it evicted a pair",
          any('seated after evicting S1, S2' in n for n in r2['notes']),
          str([n for n in r2['notes'] if 'evicting' in n]))
    # THE BOARD. A count of seated parts is not a board -- the same rule the
    # depth-1 pin test above is written to.
    pout = os.path.join(wd, 'pair-out.kicad_pcb')
    _wpo(bpath, pout, r2['placements'])
    ok, detail = pairwise_legal(pout)
    check("#699 THE BOARD: every pair of the written depth-2 output is "
          "legal (BIG seated, both blockers re-seated clear of it)",
          ok, detail)
    check("#699: and all three parts really are on the board",
          len(r2['placements']) == 3,
          str([p['reference'] for p in r2['placements']]))

# --------------------------------------------------------------------------
# A trade that cannot be completed REVERTS, says so, and leaves the board as
# it was
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    proc, s, out = run(wd, two_bigs_board, intent_for(['BIG1', 'BIG2']),
                       '--evict-depth', '1')
    proc0, s0, out0 = run(wd, two_bigs_board, intent_for(['BIG1', 'BIG2']),
                          '--evict-depth', '0', tag='ctrl')
    check("the reverted case runs (both depths)",
          s is not None and s0 is not None,
          (proc.stdout[-400:] + proc.stderr[-400:]))
    if s and s0:
        check("the census found the blocker (so the trade was attempted)",
              s['no_pose_blockers'].get('BIG1', {}).get('BIG2', 0) > 0,
              str(s['no_pose_blockers']))
        check("the trade REVERTED: JSON says evictions 0, reverted 1",
              s.get('evictions') == 0 and s.get('evictions_reverted') == 1,
              f"evictions {s.get('evictions')} reverted "
              f"{s.get('evictions_reverted')}")
        check("the part stays unseated and the exit code is 4",
              s['unseated_refs'] == ['BIG1'] and proc.returncode == 4,
              f"{s['unseated_refs']} rc={proc.returncode}")
        check("the NOTE names the revert and the conjunct that failed",
              any('REVERTED' in l and 'no legal pose to return' in l
                  for l in proc.stdout.splitlines()),
              str([l for l in proc.stdout.splitlines() if 'REVERT' in l]))
        check("THE BOARD equals the pre-eviction poses (depth 0 output)",
              poses(out) == poses(out0),
              f"{poses(out)} vs {poses(out0)}")

# --------------------------------------------------------------------------
# CONJUNCT 3 IS THE DECIDING ONE: a re-seat that is courtyard-clear but
# pad-intersecting, which the baseline-relative pad gate tolerates
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    proc, s, out = run(wd, pad_overhang_board, intent_for(['BIG', 'SMALL']),
                       '--evict-depth', '1')
    proc0, s0, out0 = run(wd, pad_overhang_board,
                          intent_for(['BIG', 'SMALL']),
                          '--evict-depth', '0', tag='ctrl')
    check("the pad-overhang case runs (both depths)",
          s is not None and s0 is not None,
          (proc.stdout[-400:] + proc.stderr[-400:]))
    if s and s0:
        check("the fixture reproduces: BIG blocked by SMALL, census names it",
              s0['unseated_refs'] == ['BIG']
              and s['no_pose_blockers'].get('BIG', {}).get('SMALL', 0) > 0,
              f"{s0['unseated_refs']} {s['no_pose_blockers']}")
        check("the trade was REVERTED by conjunct 3 (violations rose), "
              "having passed both seats and the legality re-check",
              s.get('evictions') == 0 and s.get('evictions_reverted') == 1
              and any('REVERTED' in l and 'violations rose' in l
                      for l in proc.stdout.splitlines()),
              str([l for l in proc.stdout.splitlines() if 'REVERT' in l]))
        check("the board equals the depth-0 poses",
              poses(out) == poses(out0), f"{poses(out)} vs {poses(out0)}")
        # Absolute, on the written file: no pad of BIG intersects a pad of
        # SMALL. This is what the mutation `accepted = bool(ok and legal)`
        # ships, and the JSON above would not be the only thing to say so.
        pcb_o = parse_kicad_pcb(out)
        st_o = pose_score.make_state(pcb_o, out, clearance=0.2,
                                     board_edge_clearance=0.5, grid_step=0.1)
        sf = st_o.legality_ctx.pair_shortfall('BIG', 'SMALL')
        claims_seated = not s.get('unseated_refs')
        check("and if the JSON claims both seated, no BIG pad intersects a "
              "SMALL pad in the written board",
              (not claims_seated) or not (sf.pad_overlap or sf.stack),
              f"unseated_refs={s.get('unseated_refs')} "
              f"pad_overlap={sf.pad_overlap} stack={sf.stack} {poses(out)}")

# --------------------------------------------------------------------------
# A declared edge connector is never evicted inland
# --------------------------------------------------------------------------
EDGE = {"ref": "J1", "edge": "south",
        "overhang_mm": {"min": 0.0, "max": 1.0}}
with tempfile.TemporaryDirectory() as wd:
    proc, s, out = run(wd, edge_board,
                       intent_for(['BIG'], edge=EDGE, size=EDGE_SIZE),
                       '--evict-depth', '1')
    check("the edge case runs", s is not None,
          (proc.stdout[-400:] + proc.stderr[-400:]))
    if s:
        pz = poses(out)
        check("J1 was seated on its south edge by stage 1",
              abs(pz['J1'][0] - 10.0) < 1e-6
              and abs(pz['J1'][1] - 12.5) < 0.05,
              str(pz))
        check("BIG has no pose while J1 sits there (the fixture reproduces)",
              s['unseated_refs'] == ['BIG'], str(s['unseated_refs']))
        check("the census does NOT name J1 as a blocker: a declared edge "
              "connector is not this rung's to lift",
              'J1' not in s['no_pose_blockers'].get('BIG', {'J1': 1}),
              str(s['no_pose_blockers']))
        check("J1 was never evicted (no trade attempted, kept or reverted)",
              'evicting J1' not in proc.stdout
              and s.get('evictions') == 0
              and s.get('evictions_reverted') == 0,
              str([l for l in proc.stdout.splitlines() if 'evict' in l]))
        check("and J1 is still ON ITS EDGE in the written board, not inland",
              abs(pz['J1'][1] - 12.5) < 0.05 and pz['J1'][2] == 0.0,
              str(pz))

# --------------------------------------------------------------------------
# The --reseat path carries the census too: a scope ref with no legal pose
# names what is in its way in ITS JSON_SUMMARY, not only in a NOTE line
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    def placed_blocked(path):
        # A PLACED board (distinct coordinates, so assess_placement does not
        # call it a pile): SMALL seated at the centre, BIG beside it,
        # overlapping. --reseat BIG lifts BIG and re-seats it at its net
        # centroid, which is SMALL's pads -- no legal pose while SMALL sits
        # there.
        board(path, [_part('BIG', 8, 7, 5.0, 5.0, 2),
                     _part('SMALL', 9.5, 8.5, 0.5, 0.5, 4)])
    bpath = os.path.join(wd, 'placed.kicad_pcb')
    ipath = os.path.join(wd, 'placed-fp.json')
    outp = os.path.join(wd, 'placed-out.kicad_pcb')
    placed_blocked(bpath)
    with open(ipath, 'w', encoding='utf-8') as f:
        json.dump(intent_for(['BIG', 'SMALL']), f)
    r = subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join('py_placer', 'place_seed.py'), bpath, outp,
         '--intent', ipath, '--clearance', '0.2',
         '--board-edge-clearance', '0.5', '--reseat', 'BIG'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=REPO, timeout=900,
        env=dict(os.environ, PYTHONHASHSEED='0', PYTHONIOENCODING='utf-8'))
    sr = None
    for line in r.stdout.splitlines():
        if line.startswith('JSON_SUMMARY:'):
            sr = json.loads(line.split(':', 1)[1])
    check("--reseat runs on the placed fixture and emits a summary",
          sr is not None and sr.get('reseat') is True,
          (r.stdout[-500:] + r.stderr[-500:]))
    if sr:
        check("--reseat: BIG is in scope and unseated (the fixture reproduces)",
              sr.get('scope') == ['BIG'] and sr.get('unseated') == ['BIG'],
              f"scope {sr.get('scope')} unseated {sr.get('unseated')}")
        check("--reseat: the JSON_SUMMARY carries no_pose_blockers naming "
              "SMALL, not only a NOTE line",
              sr.get('no_pose_blockers', {}).get('BIG', {}).get('SMALL', 0)
              > 0,
              str(sr.get('no_pose_blockers')))
        check("--reseat: the eviction counts are present and 0 (depth 0)",
              sr.get('evictions') == 0 and sr.get('evictions_reverted') == 0,
              f"{sr.get('evictions')} {sr.get('evictions_reverted')}")

# --------------------------------------------------------------------------
# A locked incumbent is never evicted -- it is not this tool's to move
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    def locked_pile(path):
        pile_board(path)
        src = open(path, encoding='utf-8').read().replace(
            '(footprint "test:PSMALL"\n\t\t(layer "F.Cu")',
            '(footprint "test:PSMALL"\n\t\t(layer "F.Cu")\n\t\t(locked yes)')
        open(path, 'w', encoding='utf-8').write(src)
    proc, s, out = run(wd, locked_pile, intent_for(['BIG', 'SMALL']),
                       '--evict-depth', '1')
    check("a file-locked incumbent is never evicted",
          'evicting SMALL' not in proc.stdout
          and s is not None and s.get('evictions') == 0,
          "the rung moved a part the file locked")

# --------------------------------------------------------------------------
# THE GATE DOES NOT TRUST `_try_place`. Inject the fault that shipped: the
# blocker's re-seat "succeeds" by landing it on the part just seated. The
# acceptance rule's legality conjunct must refuse it and restore both.
# --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as wd:
    import random
    from placement import floorplan
    bpath = os.path.join(wd, 'inj.kicad_pcb')
    pile_board(bpath)
    ipath = os.path.join(wd, 'inj.json')
    with open(ipath, 'w', encoding='utf-8') as f:
        json.dump(intent_for(['BIG', 'SMALL']), f)
    intent = floorplan.load_intent(ipath)
    pcb = parse_kicad_pcb(bpath)
    real_try_place = seeder._try_place
    injected = {'n': 0}

    def faulty_try_place(state, ref, tx, ty, exclude, *a, **kw):
        # Only the rung calls _try_place for SMALL with BIG seated and NOT
        # excluded; that is the re-seat of the blocker. Stack it on BIG.
        if ref == 'SMALL' and 'BIG' not in exclude \
                and 'BIG' in state.parts and injected['n'] == 0:
            injected['n'] += 1
            b = state.parts['BIG']
            state.apply_move('SMALL', b.x, b.y, state.parts['SMALL'].rot)
            return state.clearance
        return real_try_place(state, ref, tx, ty, exclude, *a, **kw)

    # The control: the same seed at depth 0 gives SMALL's pre-trade seat.
    res0 = seeder.seed_from_intent(
        pcb, bpath, intent, random.Random("0"), clearance=0.2,
        board_edge_clearance=0.5, grid_step=0.1, evict_depth=0)
    seeder._try_place = faulty_try_place
    try:
        res = seeder.seed_from_intent(
            pcb, bpath, intent, random.Random("0"), clearance=0.2,
            board_edge_clearance=0.5, grid_step=0.1, evict_depth=1)
    finally:
        seeder._try_place = real_try_place
    ev = res.get('evictions') or []
    check("the fault was injected (the blocker was 'seated' on the part)",
          injected['n'] == 1, str(injected))
    check("the trade was REVERTED by the legality conjunct, not accepted",
          len(ev) == 1 and ev[0]['accepted'] is False
          and 'not legal' in ev[0]['reason'],
          str(ev))
    check("BIG is reported unseated again, not placed on top of SMALL",
          res['unseated'] == ['BIG']
          and all(p['reference'] != 'BIG' for p in res['placements']),
          f"unseated {res['unseated']}")
    placed = {p['reference']: (p['new_x'], p['new_y'], p['new_rotation'])
              for p in res['placements']}
    placed0 = {p['reference']: (p['new_x'], p['new_y'], p['new_rotation'])
               for p in res0['placements']}
    check("SMALL is back at its pre-trade seat (the snapshot restore)",
          placed.get('SMALL') is not None
          and placed.get('SMALL') == placed0.get('SMALL'),
          f"{placed} vs depth-0 {placed0}")

# --------------------------------------------------------------------------
# The seeder refuses an undefined depth too (the CLI's choices= is not the
# only guard)
# --------------------------------------------------------------------------
try:
    seeder.seed_from_intent(pcb, bpath, intent, random.Random("0"),
                            evict_depth=3)
    check("seed_from_intent(evict_depth=3) raises", False, "no error")
except ValueError as exc:
    check("seed_from_intent(evict_depth=3) raises", 'evict_depth' in str(exc),
          str(exc))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
