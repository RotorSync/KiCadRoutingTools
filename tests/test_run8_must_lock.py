#!/usr/bin/env python3
"""One word, one meaning: a file-locked part is never the tool's to move.

`must_lock` had drifted into five meanings across the placement tools, and two
of them were opposites:

  emit-intent   filled it with the board's OWN file-locked set (an observation)
  grade         demanded every listed ref carry (locked yes)  (a requirement)
  seed          seat these first, in place where possible     (a priority)
  repair        "seeder-owned": lift the lock and MOVE them   (a licence)
  reconstruct   did not look at it at all

Composed, the first and fourth resolve to "unlock exactly the parts the user
locked, and move them" -- which is what `--emit-intent` followed by `--repair`
did on two run-7 boards, one of which walked a locked part 31mm.

The collapse: the FILE's locks win everywhere. must_lock keeps its grading
meaning and its seeding priority, loses the licence, and is now honoured by
reconstruct's frame tier as well. Emitted intents record the observation under
`context.file_locked`, which nothing acts on.

Run: python3 -X utf8 tests/test_run8_must_lock.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
os.environ.setdefault('KRT_NO_BANNER', '1')

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import floorplan, seeder, reconstruct          # noqa: E402
from placement.parser import extract_locked_refs              # noqa: E402
import pose_score                                             # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def lock_ref_in_file(path, ref):
    """Stamp (locked yes) on one footprint, the way KiCad does."""
    text = open(path, encoding='utf-8').read()
    m = re.search(r'\(footprint\s', text)
    idx, found = 0, None
    while True:
        m = re.compile(r'\(footprint\s').search(text, idx)
        if not m:
            break
        nxt = re.compile(r'\(footprint\s').search(text, m.end())
        chunk_end = nxt.start() if nxt else len(text)
        chunk = text[m.start():chunk_end]
        if re.search(r'\(property\s+"Reference"\s+"%s"' % re.escape(ref), chunk):
            found = (m.start(), chunk_end, chunk)
            break
        idx = m.end()
    assert found, f'{ref} not found'
    s, e, chunk = found
    patched = chunk.replace('(footprint ', '(footprint ', 1)
    ins = patched.index('\n') + 1
    patched = patched[:ins] + '\t\t(locked yes)\n' + patched[ins:]
    open(path, 'w', encoding='utf-8', newline='').write(text[:s] + patched + text[e:])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        board = os.path.join(tmp, 'b.kicad_pcb')
        shutil.copy(BOARD, board)

        # Damage the board so there is something to repair, then lock one of
        # the parts that ends up violating.
        from placement.perturb import perturb
        dmg = os.path.join(tmp, 'dmg.kicad_pcb')
        perturb(board, dmg, kind='translate', seed=12, dose_mm=6.0)

        pcb = parse_kicad_pcb(dmg)
        state = pose_score.make_state(pcb, dmg, clearance=0.1,
                                      board_edge_clearance=0.2, grid_step=0.1)
        metrics = state.pad_legality_metrics()
        pairs = metrics.get('pad_pairs') or metrics.get('pairs') or []
        victim = None
        for entry in pairs:
            cand = entry[0] if isinstance(entry, (list, tuple)) else None
            if cand in pcb.footprints:
                victim = cand
                break
        if victim is None:
            victim = sorted(pcb.footprints)[0]

        lock_ref_in_file(dmg, victim)
        check(f'{victim} is now file-locked', victim in extract_locked_refs(dmg))

        print('emit-intent no longer writes a requirement it merely observed')
        intent_path = os.path.join(tmp, 'auto.json')
        data = floorplan.emit_intent(parse_kicad_pcb(dmg), dmg)
        json.dump(data, open(intent_path, 'w', encoding='utf-8'))
        check('must_lock is empty on an emitted intent', data['must_lock'] == [],
              str(data['must_lock'])[:200])
        check('the locked set is recorded as an OBSERVATION',
              victim in (data.get('context') or {}).get('file_locked', []),
              str((data.get('context') or {}).get('file_locked'))[:200])

        print('repair will not move a file-locked part')
        pcb2 = parse_kicad_pcb(dmg)
        intent = floorplan.load_intent(intent_path)
        rep = seeder.repair_placement(pcb2, dmg, intent, group_sources=(),
                                      clearance=0.1, board_edge_clearance=0.2,
                                      grid_step=0.1)
        moved = {m['reference'] for m in rep['moves']}
        check('the locked part is not among the moves', victim not in moved,
              str(sorted(moved))[:200])
        check('it is reported as unrepairable, not silently skipped',
              victim in rep['unrepairable'] or victim not in
              (set(rep['repaired']) | moved),
              f"unrepairable={rep['unrepairable']}")

        print('...even when an intent lists it in must_lock')
        hand = dict(data)
        hand['must_lock'] = [victim]
        hand_path = os.path.join(tmp, 'hand.json')
        json.dump(hand, open(hand_path, 'w', encoding='utf-8'))
        rep2 = seeder.repair_placement(parse_kicad_pcb(dmg), dmg,
                                       floorplan.load_intent(hand_path),
                                       group_sources=(), clearance=0.1,
                                       board_edge_clearance=0.2, grid_step=0.1)
        moved2 = {m['reference'] for m in rep2['moves']}
        check('must_lock is not a licence to move a locked part',
              victim not in moved2, str(sorted(moved2))[:200])

        print('reconstruct puts a must_lock ref in the frame tier')
        free_ref = next(r for r in sorted(parse_kicad_pcb(dmg).footprints)
                        if r != victim)
        hand2 = dict(data)
        hand2['must_lock'] = [free_ref]
        p2 = os.path.join(tmp, 'hand2.json')
        json.dump(hand2, open(p2, 'w', encoding='utf-8'))
        st = pose_score.make_state(parse_kicad_pcb(dmg), dmg, clearance=0.1,
                                   board_edge_clearance=0.2, grid_step=0.1)
        tiers_without = reconstruct.classify(st, None)
        tiers_with = reconstruct.classify(st, floorplan.load_intent(p2))
        check('a must_lock ref is locked for reconstruct',
              free_ref in tiers_with.locked, str(sorted(tiers_with.locked))[:160])
        check('...and was not, before this fix',
              free_ref not in tiers_without.locked)

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
