"""
Grading a board against its floorplan intent (issue #549).

The board half. Two properties carry this file:

ANTI-VACUITY. `emit_intent` then `grade` must return zero errors on every
tracked board -- and that round trip is worthless on its own, because a grader
that skips every rule also returns zero. So each rule is separately shown to
FIRE, on a real board, naming the right reference with the right measured
number. A rule that cannot be made to fail is not checking anything.

NO SECOND IMPLEMENTATION. Each rule measures with the geometry the optimizer
itself gates on -- `QuenchState.legality_metrics`, `BoardOutlineGate`,
`groups.decap_tethers`. Where the grader could plausibly have re-derived
something, a test asserts the two agree exactly, because a grader with its own
idea of "legal" grades the reimplementation rather than the board.

The outline is never graded around. A board whose Edge.Cuts did not parse into
something trustworthy makes the run REFUSE: the silent fallback is to the
bounding box, and a grader that inherits it reports a clean board because it
stopped checking.
"""

import copy
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb
from placement import groups as groups_mod
from placement import legality
from placement.floorplan import (ERROR, UntrustworthyOutline, emit_intent,
                                 grade, intent_from_dict, IntentError,
                                 outline_state, summary, to_json)
from placement.quench import QuenchState
import routing_defaults as defaults

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KF = os.path.join(ROOT, 'kicad_files')

# ulx3s is the workhorse: 234 parts, 168 on the back, 10 sheet blocks.
ULX = os.path.join(KF, 'ulx3s.kicad_pcb')
ROUND_TRIP = ['splitflap_driver', 'interf_u_unrouted', 'tigard', 'watchy',
              'ulx3s', 'glasgow_revC', 'sonde_u']
SOURCES = ('kicad', 'sheet')


def _board(name):
    return os.path.join(KF, name + '.kicad_pcb')


def _graded(raw, path=ULX, **kw):
    kw.setdefault('group_sources', SOURCES)
    return grade(intent_from_dict(raw, source_path='emitted.json'),
                 parse_kicad_pcb(path), path, **kw)


def _emit(path=ULX):
    return emit_intent(parse_kicad_pcb(path), path)


def _zoned_block(raw):
    for b in raw['blocks']:
        if 'zone' in b:
            return b
    raise AssertionError("emit produced no zoned block")


def test_an_emitted_intent_grades_clean_on_every_tracked_board():
    """The round trip. Also the only thing that proves the rules are wired to
    real geometry at all rather than raising on it."""
    checked = 0
    for name in ROUND_TRIP:
        p = _board(name)
        if not os.path.exists(p):
            continue
        raw = emit_intent(parse_kicad_pcb(p), p)
        r = _graded(raw, p)
        assert r.passed, (name, [v.message for v in r.errors[:4]])
        checked += 1
    assert checked >= 5, f"only {checked} boards round-tripped"
    print(f"  PASS: {checked} boards emit -> grade with zero errors")


def test_the_round_trip_is_not_vacuous():
    """Guards the failure the round trip cannot see: every rule skipped."""
    raw = _emit()
    r = _graded(raw)
    assert len(r.rules_run) >= 4, r.rules_run
    assert 'envelope' in r.rules_run
    assert r.blocks, "no block resolved on a board with 10 sheet blocks"
    covered = {ref for v in r.blocks.values() for ref in v}
    assert len(covered) > 50, len(covered)
    print(f"  PASS: {len(r.rules_run)} rules ran, {len(r.blocks)} blocks, "
          f"{len(covered)} parts covered")


def test_a_part_pushed_out_of_its_zone_is_caught_with_the_distance():
    raw = _emit()
    blk = _zoned_block(raw)
    shrink = 5.0
    for b in raw['blocks']:
        if b['name'] == blk['name']:
            b['zone'] = [b['zone'][0], b['zone'][1], b['zone'][2] - shrink,
                         b['zone'][3]]
    r = _graded(raw)
    hits = [v for v in r.violations if v.rule == 'zone_containment']
    assert hits, "shrinking a zone 5mm caught nothing"
    assert all(v.block == blk['name'] for v in hits)
    worst = max(v.measured['outside_mm'] for v in hits)
    assert worst <= shrink + 1e-6, worst
    assert all(v.measured['axis'] == 'east' for v in hits), \
        "the escape axis is not the edge that was moved"
    assert all(v.ref for v in hits), "a containment finding with no reference"
    print(f"  PASS: {len(hits)} part(s) caught, worst {worst:.2f}mm east")


def test_an_unresolved_block_is_an_error_not_a_silent_pass():
    """The failure this whole file exists to prevent: a typo'd block matches
    nothing, every rule over it iterates an empty set, and the board grades
    clean while nothing was checked."""
    raw = _emit()
    raw['blocks'].append({'name': 'typo', 'refs': ['ZZ99*']})
    r = _graded(raw)
    hits = [v for v in r.violations if v.rule == 'block_unresolved']
    assert len(hits) == 1, [v.message for v in hits]
    assert hits[0].block == 'typo'
    assert hits[0].severity == ERROR
    assert r.blocks['typo'] == []
    print(f"  PASS: {hits[0].message[:88]}")


def test_a_group_name_resolves_in_both_its_raw_and_short_form():
    """`--list-groups` prints short_name, so that is what anyone copies into an
    intent. The raw uuid key has to keep working too."""
    pcb = parse_kicad_pcb(ULX)
    derived = groups_mod.derive_groups(pcb, SOURCES)
    key = sorted(derived, key=lambda k: -len(derived[k]))[0]
    short = groups_mod.short_name(key)
    assert short != key, "picked a block whose short form is identical"
    base = _emit()
    out = {}
    for name, g in (('raw', key), ('short', short)):
        raw = copy.deepcopy(base)
        raw['blocks'] = [{'name': 'b', 'group': g}]
        r = _graded(raw)
        assert not [v for v in r.violations if v.rule == 'block_unresolved'], g
        out[name] = r.blocks['b']
    assert out['raw'] == out['short'] == sorted(derived[key])
    print(f"  PASS: {short} and its raw uuid both resolve to "
          f"{len(out['raw'])} refs")


def test_a_side_violation_names_the_part():
    pcb = parse_kicad_pcb(ULX)
    state = QuenchState(pcb, ULX, clearance=defaults.CLEARANCE,
                        board_edge_clearance=0.55, crossing_penalty=10.0,
                        halo_base=0.5, halo_coef=0.25, halo_weight=2.0,
                        edge_halo=2.0, edge_weight=2.0,
                        grid_step=defaults.GRID_STEP, length_weight=1.0)
    back = sorted(p.ref for p in state.graded_parts() if p.side == 'B')
    assert len(back) > 20, len(back)
    raw = _emit()
    raw['blocks'] = [{'name': 'wrong', 'refs': back[:5], 'side': 'F'}]
    r = _graded(raw)
    hits = [v for v in r.violations if v.rule == 'zone_side']
    assert sorted(v.ref for v in hits) == back[:5], [v.ref for v in hits]
    assert all(v.measured['side'] == 'B' for v in hits)
    print(f"  PASS: 5 back-side parts flagged against a side:F block")


def test_a_keepout_catches_a_through_hole_part_from_the_other_side():
    """A THT part occupies BOTH faces: its leads pass through a keep-out even
    when its body is on the far side. Using only the body side would let a
    mounting-hole keep-out be walked through from the back."""
    pcb = parse_kicad_pcb(ULX)
    state = QuenchState(pcb, ULX, clearance=defaults.CLEARANCE,
                        board_edge_clearance=0.55, crossing_penalty=10.0,
                        halo_base=0.5, halo_coef=0.25, halo_weight=2.0,
                        edge_halo=2.0, edge_weight=2.0,
                        grid_step=defaults.GRID_STEP, length_weight=1.0)
    tht = [p for p in state.graded_parts() if p.has_tht]
    assert tht, "no through-hole part on ulx3s"
    part = sorted(tht, key=lambda p: p.ref)[0]
    x0, y0, x1, y1 = part.rect
    raw = _emit()
    raw['keepouts'] = [{'name': 'over-tht',
                        'rect': [x0 + 0.1, y0 + 0.1, x1 - 0.1, y1 - 0.1],
                        'sides': ['F' if part.side == 'B' else 'B']}]
    r = _graded(raw)
    hits = [v for v in r.violations
            if v.rule == 'keepout' and v.ref == part.ref]
    assert hits, (f"{part.ref} (side {part.side}, THT) not caught by a keep-out "
                  f"on the opposite face")
    assert sorted(hits[0].measured['sides_occupied']) == ['B', 'F']
    print(f"  PASS: {part.ref} on {part.side} caught by an opposite-face "
          f"keep-out (occupies both)")


def test_a_keepout_allow_list_clears_its_own_part():
    raw = _emit()
    r0 = _graded(raw)
    pcb = parse_kicad_pcb(ULX)
    state = QuenchState(pcb, ULX, clearance=defaults.CLEARANCE,
                        board_edge_clearance=0.55, crossing_penalty=10.0,
                        halo_base=0.5, halo_coef=0.25, halo_weight=2.0,
                        edge_halo=2.0, edge_weight=2.0,
                        grid_step=defaults.GRID_STEP, length_weight=1.0)
    part = sorted(state.graded_parts(), key=lambda p: p.ref)[0]
    raw['keepouts'] = [{'name': 'k', 'rect': list(part.rect)}]
    hit = [v for v in _graded(raw).violations if v.ref == part.ref]
    assert hit, f"{part.ref} not caught by a keep-out over its own courtyard"
    raw['keepouts'][0]['allow'] = [part.ref]
    cleared = [v for v in _graded(raw).violations if v.ref == part.ref]
    assert not cleared, f"allow list did not clear {part.ref}"
    print(f"  PASS: {part.ref} caught, then cleared by allow")


def test_decap_distance_agrees_with_the_grouper_exactly():
    """The no-second-implementation test. If the grader re-derived which IC a
    cap belongs to, it would be grading its own reimplementation."""
    pcb = parse_kicad_pcb(ULX)
    tethers = groups_mod.decap_tethers(pcb)
    assert tethers, "no decap tethers on ulx3s"
    flat = {cap: (ic, d) for ic in tethers for cap, d in tethers[ic]}
    raw = _emit()
    raw['decaps'] = {'max_distance_mm': 0.0}      # flag every tether
    r = _graded(raw)
    hits = {v.ref: v for v in r.violations if v.rule == 'decap_distance'}
    assert hits, "a 0mm limit flagged no cap"
    for cap, v in hits.items():
        ic, dist = flat[cap]
        assert v.measured['ic'] == ic, (cap, v.measured['ic'], ic)
        assert abs(v.measured['distance_mm'] - round(dist, 4)) < 1e-9, cap
    print(f"  PASS: {len(hits)} cap distances identical to decap_tethers")


def test_decap_exempt_and_limit_both_bite():
    pcb = parse_kicad_pcb(ULX)
    tethers = groups_mod.decap_tethers(pcb)
    dists = sorted(d for ic in tethers for _c, d in tethers[ic])
    mid = dists[len(dists) // 2]
    raw = _emit()
    raw['decaps'] = {'max_distance_mm': mid}
    hits = [v for v in _graded(raw).violations if v.rule == 'decap_distance']
    assert all(v.measured['distance_mm'] > mid for v in hits)
    assert 0 < len(hits) < len(dists), (len(hits), len(dists))
    worst = max(hits, key=lambda v: v.measured['distance_mm'])
    raw['decaps']['exempt'] = [worst.ref]
    after = [v for v in _graded(raw).violations if v.rule == 'decap_distance']
    assert len(after) == len(hits) - 1
    assert worst.ref not in {v.ref for v in after}
    print(f"  PASS: limit {mid:.2f}mm flags {len(hits)}/{len(dists)}; "
          f"exempting {worst.ref} drops exactly one")


def test_must_lock_reports_a_part_the_file_does_not_pin():
    raw = _emit()
    raw['must_lock'] = ['R1']
    hits = [v for v in _graded(raw).violations if v.rule == 'must_lock']
    assert [v.ref for v in hits] == ['R1'], [v.message for v in hits]
    assert hits[0].measured['locked'] is False
    # A pattern matching nothing is its own finding: silence would read as "all
    # locked" when it means "you named something that is not on this board".
    raw['must_lock'] = ['QQ*']
    hits = [v for v in _graded(raw).violations if v.rule == 'must_lock']
    assert len(hits) == 1 and hits[0].measured['matched'] == 0
    print("  PASS: unlocked part and unmatched pattern both reported")


def test_legality_numbers_are_the_optimizers_own():
    pcb = parse_kicad_pcb(ULX)
    state = QuenchState(pcb, ULX, clearance=defaults.CLEARANCE,
                        board_edge_clearance=0.55, crossing_penalty=10.0,
                        halo_base=0.5, halo_coef=0.25, halo_weight=2.0,
                        edge_halo=2.0, edge_weight=2.0,
                        grid_step=defaults.GRID_STEP, length_weight=1.0)
    want = state.legality_metrics()
    got = _graded(_emit()).legality
    for k, v in want.items():
        assert abs(float(got[k]) - float(v)) < 1e-3, (k, got[k], v)
    print(f"  PASS: {sorted(want)} identical to QuenchState.legality_metrics")


def test_oob_area_is_refused_as_a_budget_because_it_is_cutout_blind():
    """out_of_board_area measures against the bounding-box inset, so a part
    entirely inside a milled slot scores 0.0 and would grade clean. Refused at
    load time rather than ignored, so the reason reaches whoever wrote it."""
    raw = _emit()
    raw['legality_budget']['oob_area'] = 0.0
    try:
        intent_from_dict(raw)
        raise AssertionError("oob_area accepted as a legality budget")
    except IntentError as exc:
        assert 'cutout' in str(exc).lower(), exc
        assert 'oob_count' in str(exc), exc
    raw['legality_budget'].pop('oob_area')
    raw['legality_budget']['made_up'] = 1
    try:
        intent_from_dict(raw)
        raise AssertionError("an unknown budget key was accepted")
    except IntentError:
        pass
    print("  PASS: oob_area refused with the cutout reason; unknown key refused")


def test_a_legality_budget_bites_when_exceeded():
    raw = _emit()
    raw['legality_budget'] = {'overlap_area': 0.0, 'oob_count': 0}
    r = _graded(raw)
    hits = [v for v in r.violations if v.rule == 'legality']
    # ulx3s has real overhanging connectors, so a zero budget must catch them.
    assert hits, "a zero legality budget caught nothing on ulx3s"
    print(f"  PASS: {len(hits)} budget violation(s): "
          f"{[v.message[:52] for v in hits]}")


def test_the_envelope_rule_refuses_to_licence_a_resize():
    """The board outline is fixed. A mismatch is a finding about the INTENT."""
    raw = _emit()
    x0, y0, x1, y1 = raw['envelope']['rect']
    raw['envelope']['rect'] = [x0, y0, x1 + 10.0, y1]
    hits = [v for v in _graded(raw).violations if v.rule == 'envelope']
    assert len(hits) == 1, hits
    assert abs(hits[0].measured['worst_corner_mm'] - 10.0) < 1e-6
    assert 'do not resize' in hits[0].message, hits[0].message
    print(f"  PASS: {hits[0].message[-58:]}")


def test_emit_never_claims_a_zone_it_cannot_defend():
    """A schematic sheet is a FUNCTIONAL grouping -- its members scatter across
    the board, so per-sheet bounding boxes mutually overlap (all 10 of ulx3s's
    do, up to 4508mm2). Emitting those as zones produces an intent no placement
    could satisfy, and it would be the emitter that was wrong."""
    raw = _emit()
    zones = [b['zone'] for b in raw['blocks'] if 'zone' in b]
    assert zones, "no zone emitted at all"
    assert len(zones) < len(raw['blocks']), \
        "every block got a zone; the overlap filter did not engage"
    for i, a in enumerate(zones):
        for b in zones[i + 1:]:
            assert legality.rect_overlap_area(tuple(a), tuple(b)) <= legality.EPS
    env = raw['envelope']['rect']
    for z in zones:
        assert (z[0] >= env[0] - 1e-6 and z[1] >= env[1] - 1e-6
                and z[2] <= env[2] + 1e-6 and z[3] <= env[3] + 1e-6), z
    unzoned = [b for b in raw['blocks'] if 'zone' not in b]
    assert all('no zone is claimed' in b['note'] for b in unzoned)
    print(f"  PASS: {len(zones)} disjoint zones of {len(raw['blocks'])} blocks, "
          f"all inside the envelope; {len(unzoned)} say why they have none")


def test_emit_records_the_overhanging_parts_as_edge_connectors():
    """Parts already leaving the outline are edge connectors by observation.
    Recording them is what stops oob_count reporting them forever."""
    raw = _emit()
    conns = raw['edge_connectors']
    assert conns, "ulx3s has overhanging parts and none were recorded"
    for c in conns:
        assert c['edge'] in ('north', 'south', 'east', 'west')
        assert c['overhang_mm']['max'] > 0
    r = _graded(raw)
    assert not [v for v in r.violations if v.rule == 'edge_connector']
    print(f"  PASS: {len(conns)} overhanging parts recorded, none flagged")


def test_an_edge_connector_outside_its_declared_overhang_is_caught():
    raw = _emit()
    assert raw['edge_connectors']
    c = raw['edge_connectors'][0]
    c['overhang_mm'] = {'min': 0.0, 'max': 0.0}
    hits = [v for v in _graded(raw).violations
            if v.rule == 'edge_connector' and v.ref == c['ref']]
    assert hits, f"{c['ref']} overhang not caught against a 0mm maximum"
    assert hits[0].measured['overhang_mm'] > 0
    print(f"  PASS: {hits[0].message[:84]}")


def test_a_board_whose_outline_did_not_parse_is_refused_not_graded():
    """#550 in miniature: a round board parses its ring fine but reports
    board_bounds None, and every containment check would silently degrade to a
    bounding box that does not exist."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'round.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write('(kicad_pcb (version 20221018) (generator pcbnew)\n'
                 '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
                 ' (44 "Edge.Cuts" user))\n'
                 '  (gr_circle (center 50 50) (end 70 50)'
                 ' (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))\n'
                 '  (net 0 "")\n)\n')
    pcb = parse_kicad_pcb(p)
    st = outline_state(pcb, p)
    assert not st['trustworthy'], st
    assert st['bounds'] is None and st['outlines'] == 1, st
    assert any('550' in s for s in st['problems']), st['problems']
    try:
        emit_intent(pcb, p)
        raise AssertionError("emit_intent graded a board with no usable bounds")
    except UntrustworthyOutline as exc:
        assert '550' in str(exc), exc
    print(f"  PASS: refused -- {st['problems'][0][:80]}")


def test_a_plain_rectangular_board_is_trustworthy_with_no_rings():
    """extract_board_contours returns ([], []) for THREE reasons, and only one
    is a defect: a simple axis-aligned rectangle IS its bounding box, exactly.
    Refusing that would refuse most of the corpus."""
    rect_boards = 0
    for name in ROUND_TRIP:
        p = _board(name)
        if not os.path.exists(p):
            continue
        st = outline_state(parse_kicad_pcb(p), p)
        assert st['trustworthy'], (name, st['problems'])
        if st['outlines'] == 0:
            assert st['simple_rectangle'], (name, st)
            rect_boards += 1
    assert rect_boards, "no rectangular board in the sample"
    print(f"  PASS: {rect_boards} rectangular board(s) trusted with 0 rings")


def test_the_summary_says_how_many_rules_RAN():
    """Anti-vacuity, for machines: `0 violations` and `0 rules ran` must not
    look the same."""
    s = summary(_graded(_emit()))
    assert s['rules_run'] >= 4 and s['violations'] == 0
    assert s['rules_skipped'] >= 1
    assert s['parts_total'] > 0 and s['blocks_resolved'] == s['blocks']
    for k in ('overlap_area', 'oob_count', 'state_unplaced', 'cutouts'):
        assert k in s, k
    import json
    json.dumps(s)                      # every value must be JSON-round-trippable
    json.dumps(to_json(_graded(_emit())))
    print(f"  PASS: rules_run={s['rules_run']} rules_skipped={s['rules_skipped']} "
          f"blocks={s['blocks']}")


def test_state_signals_reach_the_summary():
    """placement_state computes duplicate_fraction / spread_ratio /
    outside_fraction / stacked_refs and, before this, nothing emitted them --
    they were unreachable from any CLI."""
    r = _graded(_emit())
    assert r.state['unplaced'] is False
    assert r.state['n_footprints'] > 0
    assert 0.0 <= r.state['duplicate_fraction'] <= 1.0
    assert r.state['spread_ratio'] is None or r.state['spread_ratio'] > 0
    print(f"  PASS: dup={r.state['duplicate_fraction']} "
          f"spread={r.state['spread_ratio']} "
          f"outside={r.state['outside_fraction']}")


TESTS = [
    test_an_emitted_intent_grades_clean_on_every_tracked_board,
    test_the_round_trip_is_not_vacuous,
    test_a_part_pushed_out_of_its_zone_is_caught_with_the_distance,
    test_an_unresolved_block_is_an_error_not_a_silent_pass,
    test_a_group_name_resolves_in_both_its_raw_and_short_form,
    test_a_side_violation_names_the_part,
    test_a_keepout_catches_a_through_hole_part_from_the_other_side,
    test_a_keepout_allow_list_clears_its_own_part,
    test_decap_distance_agrees_with_the_grouper_exactly,
    test_decap_exempt_and_limit_both_bite,
    test_must_lock_reports_a_part_the_file_does_not_pin,
    test_legality_numbers_are_the_optimizers_own,
    test_oob_area_is_refused_as_a_budget_because_it_is_cutout_blind,
    test_a_legality_budget_bites_when_exceeded,
    test_the_envelope_rule_refuses_to_licence_a_resize,
    test_emit_never_claims_a_zone_it_cannot_defend,
    test_emit_records_the_overhanging_parts_as_edge_connectors,
    test_an_edge_connector_outside_its_declared_overhang_is_caught,
    test_a_board_whose_outline_did_not_parse_is_refused_not_graded,
    test_a_plain_rectangular_board_is_trustworthy_with_no_rings,
    test_the_summary_says_how_many_rules_RAN,
    test_state_signals_reach_the_summary,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
