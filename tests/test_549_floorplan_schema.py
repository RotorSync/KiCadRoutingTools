"""
Floorplan intent: the schema, and what makes one invalid (issue #549).

Board-independent half only -- everything here runs without parsing a
`.kicad_pcb`, which is the point of keeping `validate_intent` separate from
`grade`. An intent that contradicts ITSELF (a zone outside the envelope, two
zones fighting over the same rectangle) should be reported as a broken intent,
not as a pile of board violations that sends the reader looking at footprints.

The load-bearing assertions here are the REJECTIONS. A grader whose intent file
silently accepts a typo is worse than no grader: a block that resolves to
nothing grades clean, and "0 violations" then means "nothing was checked". So
every malformed shape has to raise rather than degrade, and the error has to
name the thing that was wrong.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # placement split

from placement.floorplan import (ERROR, KIND, RULES, SCHEMA_VERSION, WARN,
                                 Intent, IntentError, _SEVERITY_KEYS,
                                 intent_from_dict, load_intent,
                                 validate_intent)


def _base(**over):
    d = {
        'schema': SCHEMA_VERSION,
        'kind': KIND,
        'units': 'mm',
        'envelope': {'rect': [0, 0, 100, 80]},
        'blocks': [
            {'name': 'power', 'refs': ['U3', 'C1*'], 'zone': [2, 2, 40, 30]},
            {'name': 'mcu', 'refs': ['U1'], 'zone': [50, 2, 90, 30]},
        ],
    }
    d.update(over)
    return d


def _rejects(raw, why):
    try:
        intent_from_dict(raw)
    except IntentError as exc:
        return str(exc)
    raise AssertionError(f"NOT rejected: {why}")


def test_a_wellformed_intent_loads():
    i = intent_from_dict(_base())
    assert isinstance(i, Intent)
    assert [z.name for z in i.blocks] == ['power', 'mcu']
    assert i.blocks[0].refs == ('U3', 'C1*')
    assert i.blocks[0].rect == (2.0, 2.0, 40.0, 30.0)
    assert i.envelope['rect'] == (0.0, 0.0, 100.0, 80.0)
    assert validate_intent(i) == []
    print(f"  PASS: {len(i.blocks)} blocks, envelope "
          f"{i.envelope['rect']}, no self-contradiction")


def test_rects_are_normalised_not_trusted():
    """A rect given corner-swapped is still a rect. Silently comparing against
    an inverted rect would make every containment test vacuously true."""
    i = intent_from_dict(_base(envelope={'rect': [100, 80, 0, 0]}))
    assert i.envelope['rect'] == (0.0, 0.0, 100.0, 80.0)
    print("  PASS: [100,80,0,0] normalised to (0,0,100,80)")


def test_the_wrong_file_is_refused_by_kind():
    """A round sidecar or a lock-advisor dump is valid JSON with a `schema` key.
    Without the `kind` discriminator it would load as an intent with no blocks
    and no rules -- i.e. grade perfectly clean."""
    sidecar = {'schema': 1, 'round': 3, 'accepted': True, 'moved': []}
    msg = _rejects(sidecar, "a loop round sidecar")
    assert 'kind' in msg, msg
    msg = _rejects(_base(kind='lock-advice'), "kind=lock-advice")
    assert KIND in msg, msg
    print("  PASS: a round sidecar cannot masquerade as an intent")


def test_schema_version_is_enforced():
    msg = _rejects(_base(schema=SCHEMA_VERSION + 1), "a future schema")
    assert str(SCHEMA_VERSION) in msg, msg
    _rejects(_base(schema=None), "a missing schema")
    print("  PASS: only schema %d loads" % SCHEMA_VERSION)


def test_unknown_keys_are_refused_not_ignored():
    """A typo'd key that is silently dropped is a constraint the author thinks
    they set and the grader never checks."""
    msg = _rejects(_base(keepout=[]), "keepout (singular) instead of keepouts")
    assert 'keepout' in msg and 'keepouts' in msg, msg
    msg = _rejects(_base(blocks=[{'name': 'x', 'refs': ['U1'], 'zones': [1]}]),
                   "zones instead of zone")
    assert 'zones' in msg, msg
    print("  PASS: unknown top-level and per-block keys both refused")


def test_unknown_nested_keys_are_refused_not_ignored():
    """#710: the same principle, one level down.

    The loader used to be strict at exactly two levels -- top-level and
    `blocks[]` -- and permissive everywhere else, so a typo'd key inside a
    keepout, an edge connector or `health` loaded clean and constrained
    nothing. Every case below is a REAL near-miss: `max_setback` for
    `max_setback_mm`, `bus_corridor` for `bus_corridors`, `exempts` for
    `exempt`. Each message must name the key that was wrong AND the one that
    was meant -- a bare "unknown key" leaves the author reading their own
    typo back.
    """
    cases = [
        (_base(keepouts=[{'name': 'k', 'rect': [0, 0, 1, 1], 'sids': ['F']}]),
         'sids', 'sides'),
        (_base(edge_connectors=[{'ref': 'J1', 'max_setback': 2.0}]),
         'max_setback', 'max_setback_mm'),
        (_base(edge_connectors=[{'ref': 'J1',
                                 'overhang_mm': {'min': 0.0, 'maximum': 1.0}}]),
         'maximum', 'max'),
        (_base(decaps={'max_distance_mm': 2.5, 'exempts': ['C9']}),
         'exempts', 'exempt'),
        (_base(defaults={'zone_tolerance': 0.5}),
         'zone_tolerance', 'zone_tolerance_mm'),
        (_base(health={'bus_corridor': []}),
         'bus_corridor', 'bus_corridors'),
        (_base(health={'bus_corridors': [{'nets': ['/D*'], 'width': 8.0}]}),
         'width', 'width_mm'),
        (_base(envelope={'rect': [0, 0, 100, 80], 'tolerance': 0.5}),
         'tolerance', 'tolerance_mm'),
        (_base(overlap_waivers=[{'pair': ['U1', 'U2'], 'because': 'by design'}]),
         'because', 'reason'),
    ]
    # Split on `Known:` before asserting. Checking both halves of the raw
    # message would pass vacuously on every pair here -- `width` is a
    # substring of `width_mm`, `ref` of `reff` -- so the typo has to appear
    # in the REFUSAL half and the correction in the KNOWN half.
    for raw, typo, meant in cases:
        msg = _rejects(raw, f"{typo} instead of {meant}")
        refused, _, known = msg.partition('Known:')
        assert known, f"message carries no `Known:` list: {msg}"
        assert typo in refused, (typo, msg)
        assert meant in known.replace(',', ' ').split(), (meant, msg)
    # A typo'd `ref` reports the unknown key, not a missing `ref` -- the
    # author is looking at the key they DID write.
    msg = _rejects(_base(edge_connectors=[{'reff': 'J1'}]), "reff")
    refused, _, known = msg.partition('Known:')
    assert 'reff' in refused, msg
    assert 'ref' in known.replace(',', ' ').split(), msg
    print(f"  PASS: {len(cases) + 1} nested typos refused, each naming the "
          f"key that was meant")


def test_a_nested_object_must_be_an_object():
    """`raw.get(k) or {}` handed a list straight on, so the schema error
    surfaced as an AttributeError inside a rule three frames later -- or, for
    a key nothing reads yet, not at all."""
    for key, value in (('envelope', []), ('defaults', []), ('decaps', 'none'),
                       ('health', []), ('context', 3), ('severity', []),
                       ('legality_budget', [])):
        msg = _rejects(_base(**{key: value}), f"{key}={value!r}")
        assert key in msg, (key, msg)
        assert 'object' in msg, (key, msg)
    print("  PASS: 7 nested objects refuse a list/scalar instead of crashing "
          "later")


def test_context_is_deliberately_open():
    """The read-only slot. `emit_intent` writes `cutouts` / `file_locked` /
    `budget_withheld` there, and run artifacts add their own provenance prose
    (17 distinct keys across the recorded runs). Nothing grades it, so
    refusing a key here would only push provenance into a key that IS
    graded -- the worse failure."""
    i = intent_from_dict(_base(context={
        'note': 'read-only', 'authored_by': 'run-20',
        'j7_evidence': 'six of seven pins interior',
        'budget_withheld': {'overlap_area': 'a blocking body pair'}}))
    assert i.budget_withheld == {'overlap_area': 'a blocking body pair'}
    print("  PASS: context accepts arbitrary keys; budget_withheld still read")


def test_severity_keys_are_checked_against_the_rule_names():
    """#710: `severity` validated its VALUES and never its keys.

    The vocabulary is 12 names, not the 9 in `RULES`: three findings are
    raised outside the rules loop (`validate_intent` raises two,
    `resolve_blocks` one) and an intent has always been allowed to set their
    severity. Validating against `RULES` alone would refuse
    `{"block_unresolved": "warn"}`, which is legal today -- so the test
    asserts every name in the real vocabulary still loads, not just that a
    typo is refused.
    """
    msg = _rejects(_base(severity={'decap_distanc': WARN}), "a typo'd rule")
    refused, _, known = msg.partition('Known:')
    assert 'decap_distanc' in refused, msg
    assert 'decap_distance' in known.replace(',', ' ').split(), msg
    # Stated here as a literal, NOT iterated off _SEVERITY_KEYS: a test that
    # loops over the set under test just tests fewer names when the set
    # shrinks, and passes. (It did.)
    expected = {n for n, _ in RULES} | {'intent_zone_outside_envelope',
                                        'intent_zone_overlap',
                                        'block_unresolved'}
    assert _SEVERITY_KEYS == expected, sorted(_SEVERITY_KEYS ^ expected)
    for name in sorted(expected):
        i = intent_from_dict(_base(severity={name: WARN}))
        assert i.severity_of(name) == WARN, name
    print(f"  PASS: {len(expected)} settable rule names accepted, "
          f"a typo refused (RULES has {len(RULES)})")


def test_every_violation_rule_name_is_settable():
    """A change detector for the next rule.

    A rule whose name is not in `_SEVERITY_KEYS` cannot be demoted to warn --
    and, now that severity keys are refused, an intent trying to would be
    told the name does not exist. Read off the SOURCE so a new
    `Violation(rule='...')` is caught wherever it is raised, including the
    paths that never reach the RULES loop.
    """
    import re
    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'py_placer', 'placement', 'floorplan.py')
    with open(src, encoding='utf-8') as fh:
        names = set(re.findall(r"rule='([a-z_]+)'", fh.read()))
    assert len(names) >= 10, names
    assert names <= _SEVERITY_KEYS, sorted(names - _SEVERITY_KEYS)
    print(f"  PASS: all {len(names)} violation rule names are settable")


def test_a_block_needs_refs_or_group():
    msg = _rejects(_base(blocks=[{'name': 'x', 'zone': [0, 0, 1, 1]}]),
                   "a block naming no members")
    assert 'refs' in msg and 'group' in msg, msg
    # refs alone is fine -- it is the PRIMARY form, since sheet group keys are
    # uuid paths nothing can author blind.
    assert intent_from_dict(_base(blocks=[{'name': 'x', 'refs': ['U1', 'U2']}]))
    assert intent_from_dict(_base(blocks=[{'name': 'x', 'group': 'decap:U1'}]))
    print("  PASS: refs or group required; either alone suffices")


def test_refs_as_a_bare_string_is_refused():
    """`"refs": "U1"` iterates as characters and would match nothing. Python
    would accept it silently; the schema must not."""
    msg = _rejects(_base(blocks=[{'name': 'x', 'refs': 'U1'}]), "refs as a str")
    assert 'list' in msg, msg
    print("  PASS: refs must be a list, not a string")


def test_enumerations_are_checked():
    assert 'side' in _rejects(
        _base(blocks=[{'name': 'x', 'refs': ['U1'], 'side': 'top'}]), "side=top")
    assert 'edge' in _rejects(
        _base(edge_connectors=[{'ref': 'J1', 'edge': 'left'}]), "edge=left")
    assert 'units' in _rejects(_base(units='mil'), "units=mil")
    assert 'severity' in _rejects(_base(severity={'envelope': 'maybe'}),
                                  "severity=maybe")
    print("  PASS: side / edge / units / severity enumerations enforced")


def test_malformed_rects_are_refused():
    for bad in ([0, 0, 100], [0, 0, 100, 80, 5], 'big', [0, 0, 100, 'x'],
                [0, 0, 100, True]):
        _rejects(_base(envelope={'rect': bad}), f"envelope.rect={bad!r}")
    print("  PASS: 5 malformed rect shapes refused (incl. bool-as-number)")


def test_duplicate_block_names_are_refused():
    msg = _rejects(_base(blocks=[{'name': 'a', 'refs': ['U1']},
                                 {'name': 'a', 'refs': ['U2']}]),
                   "two blocks named 'a'")
    assert 'duplicate' in msg, msg
    print("  PASS: a duplicate block name cannot silently shadow the first")


def test_keepout_needs_a_shape():
    msg = _rejects(_base(keepouts=[{'name': 'mount'}]), "keepout with no shape")
    assert 'rect' in msg and 'circle' in msg, msg
    i = intent_from_dict(_base(keepouts=[
        {'name': 'mount-NW', 'rect': [0, 0, 6, 6], 'allow': ['MH1']},
        {'name': 'antenna', 'circle': [50, 5, 8]}]))
    assert i.keepouts[0]['allow'] == ('MH1',)
    assert i.keepouts[0]['sides'] == ('F', 'B')          # defaulted, both faces
    assert i.keepouts[1]['circle'] == (50.0, 5.0, 8.0)
    print("  PASS: rect and circle keepouts load; sides default to both faces")


def test_zone_outside_the_envelope_is_a_self_contradiction():
    i = intent_from_dict(_base(blocks=[
        {'name': 'oops', 'refs': ['U9'], 'zone': [90, 70, 140, 120]}]))
    v = validate_intent(i)
    assert [x.rule for x in v] == ['intent_zone_outside_envelope'], v
    assert v[0].block == 'oops'
    assert v[0].measured['zone'] == [90.0, 70.0, 140.0, 120.0]
    print(f"  PASS: {v[0].message}")


def test_two_zones_fighting_over_one_rectangle():
    i = intent_from_dict(_base(blocks=[
        {'name': 'a', 'refs': ['U1'], 'zone': [0, 0, 40, 30]},
        {'name': 'b', 'refs': ['U2'], 'zone': [30, 0, 70, 30]}]))
    v = [x for x in validate_intent(i) if x.rule == 'intent_zone_overlap']
    assert len(v) == 1, v
    assert abs(v[0].measured['overlap_area_mm2'] - 300.0) < 1e-6, v[0].measured
    print(f"  PASS: {v[0].message}")


def test_zones_on_opposite_sides_may_share_a_rectangle():
    """F and B are different real estate. Flagging that pair would make a
    two-sided floorplan unexpressible."""
    i = intent_from_dict(_base(blocks=[
        {'name': 'front', 'refs': ['U1'], 'zone': [0, 0, 40, 30], 'side': 'F'},
        {'name': 'back', 'refs': ['U2'], 'zone': [0, 0, 40, 30], 'side': 'B'}]))
    assert [x.rule for x in validate_intent(i)] == []
    print("  PASS: same rect on F and B is legal")


def test_severity_override_reaches_the_violation():
    i = intent_from_dict(_base(
        severity={'intent_zone_overlap': WARN},
        blocks=[{'name': 'a', 'refs': ['U1'], 'zone': [0, 0, 40, 30]},
                {'name': 'b', 'refs': ['U2'], 'zone': [30, 0, 70, 30]}]))
    v = [x for x in validate_intent(i) if x.rule == 'intent_zone_overlap']
    assert v[0].severity == WARN, v[0]
    assert i.severity_of('envelope') == ERROR         # untouched rules default
    print("  PASS: per-rule severity downgrades one rule and no other")


def test_violation_order_is_a_property_of_the_finding():
    """#457: anything whose order can reach a report must not depend on dict
    iteration. sort_key is what the emitters sort on."""
    i = intent_from_dict(_base(blocks=[
        {'name': 'c', 'refs': ['U3'], 'zone': [0, 0, 40, 30]},
        {'name': 'a', 'refs': ['U1'], 'zone': [30, 0, 70, 30]},
        {'name': 'b', 'refs': ['U2'], 'zone': [200, 200, 240, 230]}]))
    keys = [v.sort_key() for v in sorted(validate_intent(i),
                                         key=lambda v: v.sort_key())]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys), "sort_key collides between findings"
    print(f"  PASS: {len(keys)} findings totally ordered by (rule, ref, block)")


def test_load_intent_reports_the_path_and_the_parse_error():
    d = tempfile.mkdtemp()
    good = os.path.join(d, 'intent.json')
    with open(good, 'w', encoding='utf-8') as fh:
        json.dump(_base(), fh)
    i = load_intent(good)
    assert i.source_path == good

    bad = os.path.join(d, 'broken.json')
    with open(bad, 'w', encoding='utf-8') as fh:
        fh.write('{"schema": 1, "kind": "floorplan-intent",')
    try:
        load_intent(bad)
        raise AssertionError("truncated JSON not rejected")
    except IntentError as exc:
        assert 'broken.json' in str(exc), exc

    try:
        load_intent(os.path.join(d, 'nope.json'))
        raise AssertionError("missing file not rejected")
    except IntentError as exc:
        assert 'nope.json' in str(exc), exc
    print("  PASS: truncated and missing files both name the path")


TESTS = [
    test_a_wellformed_intent_loads,
    test_rects_are_normalised_not_trusted,
    test_the_wrong_file_is_refused_by_kind,
    test_schema_version_is_enforced,
    test_unknown_keys_are_refused_not_ignored,
    test_unknown_nested_keys_are_refused_not_ignored,
    test_a_nested_object_must_be_an_object,
    test_context_is_deliberately_open,
    test_severity_keys_are_checked_against_the_rule_names,
    test_every_violation_rule_name_is_settable,
    test_a_block_needs_refs_or_group,
    test_refs_as_a_bare_string_is_refused,
    test_enumerations_are_checked,
    test_malformed_rects_are_refused,
    test_duplicate_block_names_are_refused,
    test_keepout_needs_a_shape,
    test_zone_outside_the_envelope_is_a_self_contradiction,
    test_two_zones_fighting_over_one_rectangle,
    test_zones_on_opposite_sides_may_share_a_rectangle,
    test_severity_override_reaches_the_violation,
    test_violation_order_is_a_property_of_the_finding,
    test_load_intent_reports_the_path_and_the_parse_error,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
