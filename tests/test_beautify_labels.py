"""
Tests for the beautify_labels engine and writer (#481).

Grading discipline (CLAUDE.md): results are compared as VALUES, never as
file hashes or whole-file diffs. The one byte-level assertion is the
writer's acceptance property -- masking every Reference sub-node from input
and output yields byte-identical files -- which is exactly what "the writer
edits ONLY Reference sub-nodes" means, and is stable because this writer
generates no new UUIDs.
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import find_matching_paren, local_to_global, parse_kicad_pcb
from placement.labels import (LabelConfig, LabelResult, beautify_labels,
                              label_box)
from placement.legality import BoardOutlineGate, build_part_pads, rect_gap
from placement.writer import write_label_output

KICAD_FILES = os.path.join(os.path.dirname(__file__), '..', 'kicad_files')
SPLITFLAP = os.path.join(KICAD_FILES, 'splitflap_driver.kicad_pcb')

_REF_HEADERS = (r'\(property\s+"Reference"\s+"[^"]+"',
                r'\(fp_text\s+reference\s+"[^"]+"')


def _mask_reference_nodes(content):
    """Replace every Reference text sub-node (both forms) with a marker."""
    spans = []
    for pat in _REF_HEADERS:
        for m in re.finditer(pat, content):
            spans.append((m.start(), find_matching_paren(content, m.start())))
    out, pos = [], 0
    for start, end in sorted(spans):
        out.append(content[pos:start])
        out.append('<REF>')
        pos = end
    out.append(content[pos:])
    return ''.join(out)


def _reference_node(content, ref):
    """The verbatim Reference sub-node text for `ref` (either form)."""
    for pat in (r'\(property\s+"Reference"\s+"%s"' % re.escape(ref),
                r'\(fp_text\s+reference\s+"%s"' % re.escape(ref)):
        m = re.search(pat, content)
        if m:
            return content[m.start():find_matching_paren(content, m.start())]
    return None


def _label_rect(res):
    """World rect of a placed result, from the shared estimator."""
    w, h = label_box(res.reference, res.size, res.thickness)
    if res.world_angle == 90.0:
        w, h = h, w
    return (res.world_x - w / 2.0, res.world_y - h / 2.0,
            res.world_x + w / 2.0, res.world_y + h / 2.0)


def _tmp_out():
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    os.close(fd)
    return path


# --- splitflap: writer byte-faithfulness + engine acceptance -----------------

_SPLITFLAP_EDGE = 0.3  # explicit so the acceptance checks grade what ran

def _run_splitflap():
    pcb = parse_kicad_pcb(SPLITFLAP)
    config = LabelConfig(board_edge_clearance=_SPLITFLAP_EDGE)
    out = beautify_labels(pcb, SPLITFLAP, config)
    return pcb, config, out


def test_writer_masked_byte_identity():
    """Masking every Reference sub-node from input and output must yield
    byte-identical files: the writer touches NOTHING else."""
    _pcb, _config, out = _run_splitflap()
    out_path = _tmp_out()
    try:
        write_label_output(SPLITFLAP, out_path, out['results'])
        src = open(SPLITFLAP, encoding='utf-8').read()
        dst = open(out_path, encoding='utf-8').read()
    finally:
        os.unlink(out_path)
    assert _mask_reference_nodes(src) == _mask_reference_nodes(dst)
    # UUIDs are preserved wholesale -- this writer generates none.
    assert (re.findall(r'\(uuid\s+"[^"]+"\)', src)
            == re.findall(r'\(uuid\s+"[^"]+"\)', dst))
    print("PASS test_writer_masked_byte_identity")


def test_writer_roundtrip_and_hidden_untouched():
    pcb, _config, out = _run_splitflap()
    out_path = _tmp_out()
    try:
        write_label_output(SPLITFLAP, out_path, out['results'])
        src = open(SPLITFLAP, encoding='utf-8').read()
        dst = open(out_path, encoding='utf-8').read()
        reparsed = parse_kicad_pcb(out_path)
    finally:
        os.unlink(out_path)

    changed = [r for r in out['results'] if r.changed]
    assert changed, "expected at least one placed label on splitflap"
    for res in changed:
        label = reparsed.footprints[res.reference].ref_label
        assert abs(label.at_x - res.at_x) < 1e-6, res.reference
        assert abs(label.at_y - res.at_y) < 1e-6, res.reference
        assert label.rotation == res.file_rotation % 360, res.reference
        assert abs(label.size_h - res.size) < 1e-9
        assert abs(label.size_w - res.size) < 1e-9
        assert abs(label.thickness - res.thickness) < 1e-9
        # File-frame -> world round-trip lands on the searched center.
        fp = reparsed.footprints[res.reference]
        wx, wy = local_to_global(fp.x, fp.y, fp.rotation,
                                 label.at_x, label.at_y)
        assert abs(wx - res.world_x) < 1e-5 and abs(wy - res.world_y) < 1e-5
        # hide/layer/uuid pass through
        assert label.hidden == res.old['hidden']
        assert label.layer == res.old['layer']

    # The 19 hidden labels were skipped: their nodes are byte-identical.
    hidden = [r.reference for r in out['results']
              if r.action == 'skipped_hidden']
    assert len(hidden) == 19
    for ref in hidden:
        assert _reference_node(src, ref) == _reference_node(dst, ref), ref
    print("PASS test_writer_roundtrip_and_hidden_untouched")


def test_engine_acceptance_splitflap():
    """Every placed label: legal against pads/labels/outline, uniform size,
    world angle in {0, 90}; and the whole run is deterministic."""
    pcb, config, out = _run_splitflap()
    changed = [r for r in out['results'] if r.changed]
    assert out['summary']['unchanged'] == 0, out['summary']

    # world angles folded by construction; 'placed' means the TARGET size
    for res in changed:
        assert res.world_angle in (0.0, 90.0), res
        if res.action == 'placed':
            assert res.size == config.size
        assert res.file_rotation == res.world_angle  # absolute storage

    # pad / hole clearance, graded against the same obstacle model
    parts = build_part_pads(pcb.footprints, config.silk_pad_clearance)
    rects = {'F': [], 'B': []}
    for ref, pp in parts.items():
        fp = pcb.footprints[ref]
        for (x0, y0, x1, y1, _n, pside) in pp.pad_rects(fp.x, fp.y,
                                                        fp.rotation):
            for s in (('F', 'B') if pside is None else (pside,)):
                rects[s].append((x0, y0, x1, y1))
    for res in changed:
        side = 'B' if res.old['layer'].startswith('B') else 'F'
        rect = _label_rect(res)
        worst = min((rect_gap(rect, r) for r in rects[side]), default=99.0)
        assert worst >= config.silk_pad_clearance - 1e-6, (res.reference,
                                                           worst)

    # pairwise label clearance (same side)
    for i, a in enumerate(changed):
        for b in changed[i + 1:]:
            sa = 'B' if a.old['layer'].startswith('B') else 'F'
            sb = 'B' if b.old['layer'].startswith('B') else 'F'
            if sa != sb:
                continue
            g = rect_gap(_label_rect(a), _label_rect(b))
            assert g >= config.label_clearance - 1e-6, (a.reference,
                                                        b.reference, g)

    # board containment at the margin the run used
    gate = BoardOutlineGate(pcb.board_info, _SPLITFLAP_EDGE)
    for res in changed:
        rect = _label_rect(res)
        if gate.active:
            assert not gate.rect_blocked(rect), res.reference
        else:
            u = gate.usable
            assert (rect[0] >= u[0] and rect[1] >= u[1]
                    and rect[2] <= u[2] and rect[3] <= u[3]), res.reference

    # deterministic: a second run produces identical decisions (values,
    # never file bytes)
    out2 = beautify_labels(parse_kicad_pcb(SPLITFLAP), SPLITFLAP,
                           LabelConfig(board_edge_clearance=_SPLITFLAP_EDGE))
    key = lambda o: [(r.reference, r.action, r.at_x, r.at_y,
                      r.file_rotation, r.size, r.thickness)
                     for r in o['results']]
    assert key(out) == key(out2)
    print("PASS test_engine_acceptance_splitflap")


# --- degradation ladder on synthetic mini-boards -----------------------------

def _mini_board(width, height, cx, cy, ref='U1', courtyard=1.0,
                reference_node=None):
    """A board with one centered part: courtyard +/-`courtyard`, one 0.5mm
    pad, a stock Reference label. Returns the temp file path."""
    if reference_node is None:
        reference_node = f'''\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "ref-{ref}")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1 1)
\t\t\t\t\t(thickness 0.15)
\t\t\t\t)
\t\t\t)
\t\t)
'''
    body = f'''(kicad_pcb
\t(version 20241229)
\t(net 0 "")
\t(gr_rect
\t\t(start 0 0)
\t\t(end {width} {height})
\t\t(layer "Edge.Cuts")
\t\t(uuid "e1")
\t)
\t(footprint "test:FP"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {cx} {cy})
{reference_node}\t\t(fp_rect
\t\t\t(start -{courtyard} -{courtyard})
\t\t\t(end {courtyard} {courtyard})
\t\t\t(stroke
\t\t\t\t(width 0.05)
\t\t\t\t(type default)
\t\t\t)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "crtyd-{ref}")
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
)
'''
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


_MINI_CONFIG = dict(board_edge_clearance=0.1)


def test_ladder_shrunk():
    """1.2mm of space on every side fits a 0.7 label (box 0.875) but not a
    1.0 one (box 1.25) in either orientation -> the ladder shrinks."""
    path = _mini_board(4.4, 4.4, 2.2, 2.2)
    try:
        out = beautify_labels(parse_kicad_pcb(path), path,
                              LabelConfig(**_MINI_CONFIG))
    finally:
        os.unlink(path)
    (res,) = out['results']
    assert res.action == 'shrunk', res
    assert abs(res.size - 0.7) < 1e-9
    assert abs(res.thickness - 0.105) < 1e-9  # ratio-scaled with the size
    assert res.world_angle == 0.0
    print("PASS test_ladder_shrunk")


def test_ladder_rotated():
    """A 2.2mm-wide usable channel blocks the 2.55mm horizontal box at full
    size, but the vertical one (1.25mm wide) fits above -> rotated, NOT
    shrunk (size is preserved before the ladder gives up width)."""
    path = _mini_board(2.4, 8.0, 1.2, 4.0)
    try:
        out = beautify_labels(parse_kicad_pcb(path), path,
                              LabelConfig(**_MINI_CONFIG))
    finally:
        os.unlink(path)
    (res,) = out['results']
    assert res.action == 'rotated', res
    assert res.size == 1.0
    assert res.world_angle == 90.0
    assert res.file_rotation == 90.0
    print("PASS test_ladder_rotated")


def test_ladder_unchanged_bytes_untouched():
    """0.7mm of space fits nothing (smallest box is 0.875mm): exactly one
    `unchanged` with a human reason, and the OUTPUT FILE is byte-identical
    to the input -- giving up must not half-edit anything."""
    path = _mini_board(3.4, 3.4, 1.7, 1.7)
    out_path = _tmp_out()
    try:
        out = beautify_labels(parse_kicad_pcb(path), path,
                              LabelConfig(**_MINI_CONFIG))
        (res,) = out['results']
        assert res.action == 'unchanged', res
        assert 'no legal position' in res.reason
        assert out['summary']['unchanged'] == 1
        assert out['summary']['unchanged_refs'] == ['U1']
        write_label_output(path, out_path, out['results'])
        assert (open(path, encoding='utf-8').read()
                == open(out_path, encoding='utf-8').read())
    finally:
        os.unlink(path)
        os.unlink(out_path)
    print("PASS test_ladder_unchanged_bytes_untouched")


# --- writer: legacy node form, effects insertion, justify handling -----------

_LEGACY_NODE = '''\t\t(fp_text reference "U1"
\t\t\t(at 0 0)
\t\t\t(layer "F.SilkS")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1 1)
\t\t\t\t\t(thickness 0.15)
\t\t\t\t)
\t\t\t)
\t\t)
'''


def test_writer_legacy_fp_text():
    """KiCad 6/7 form: alternate node header, same inner grammar."""
    path = _mini_board(10, 10, 5, 5, reference_node=_LEGACY_NODE)
    out_path = _tmp_out()
    try:
        out = beautify_labels(parse_kicad_pcb(path), path,
                              LabelConfig(**_MINI_CONFIG))
        (res,) = out['results']
        assert res.action == 'placed'
        write_label_output(path, out_path, out['results'])
        src = open(path, encoding='utf-8').read()
        dst = open(out_path, encoding='utf-8').read()
        assert _mask_reference_nodes(src) == _mask_reference_nodes(dst)
        label = parse_kicad_pcb(out_path).footprints['U1'].ref_label
        assert not label.is_property_node  # header untouched
        assert abs(label.at_x - res.at_x) < 1e-6
        assert abs(label.at_y - res.at_y) < 1e-6
        assert label.size_h == res.size and label.thickness == res.thickness
    finally:
        os.unlink(path)
        os.unlink(out_path)
    print("PASS test_writer_legacy_fp_text")


def test_writer_inserts_effects_and_prunes_justify():
    """A node with no effects gains a full block; alignment justify tokens
    are dropped while `mirror` survives."""
    bare = '''\t\t(property "Reference" "U1"
\t\t\t(at 0 0 90)
\t\t\t(layer "B.SilkS")
\t\t\t(uuid "ref-U1")
\t\t)
'''
    justified = bare.replace(
        '\t\t\t(uuid "ref-U1")\n',
        '\t\t\t(uuid "ref-U1")\n\t\t\t(effects\n\t\t\t\t(font\n'
        '\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n'
        '\t\t\t\t(justify left bottom mirror)\n\t\t\t)\n')

    for node, expect_mirror in ((bare, False), (justified, True)):
        path = _mini_board(10, 10, 5, 5, reference_node=node)
        out_path = _tmp_out()
        try:
            result = LabelResult('U1', 'placed', at_x=0.0, at_y=-1.6,
                                 file_rotation=0.0, size=0.9,
                                 thickness=0.12)
            write_label_output(path, out_path, [result])
            dst = open(out_path, encoding='utf-8').read()
            ref_node = _reference_node(dst, 'U1')
            assert '(size 0.9 0.9)' in ref_node
            assert '(thickness 0.12)' in ref_node
            assert '(at 0 -1.6)' in ref_node
            assert '(uuid "ref-U1")' in ref_node        # passes through
            assert '(layer "B.SilkS")' in ref_node
            for tok in ('left', 'bottom', 'top', 'right'):
                assert f'justify {tok}' not in ref_node
            assert ('(justify mirror)' in ref_node) == expect_mirror
            label = parse_kicad_pcb(out_path).footprints['U1'].ref_label
            assert label.size_h == 0.9 and label.thickness == 0.12
            assert label.justify == ('mirror' if expect_mirror else '')
        finally:
            os.unlink(path)
            os.unlink(out_path)
    print("PASS test_writer_inserts_effects_and_prunes_justify")


def test_glasgow_mirror_preserved():
    """Real-board mirror survival: beautify one of glasgow's 94 mirrored
    B-side labels (all hidden, so --include-hidden) and the written node
    still carries BOTH (justify mirror) and its (hide yes)."""
    board = os.path.join(KICAD_FILES, 'glasgow_revC.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    out = beautify_labels(pcb, board,
                          LabelConfig(refs=['C23'], include_hidden=True,
                                      board_edge_clearance=0.3))
    changed = [r for r in out['results'] if r.changed]
    assert len(changed) == 1 and changed[0].reference == 'C23', \
        [(r.reference, r.action, r.reason) for r in out['results']]
    out_path = _tmp_out()
    try:
        write_label_output(board, out_path, out['results'])
        node = _reference_node(open(out_path, encoding='utf-8').read(), 'C23')
        assert '(justify mirror)' in node
        assert 'left' not in node and 'bottom' not in node
        assert '(hide yes)' in node  # include_hidden tidies, never unhides
    finally:
        os.unlink(out_path)
    print("PASS test_glasgow_mirror_preserved")


if __name__ == '__main__':
    test_writer_masked_byte_identity()
    test_writer_roundtrip_and_hidden_untouched()
    test_engine_acceptance_splitflap()
    test_ladder_shrunk()
    test_ladder_rotated()
    test_ladder_unchanged_bytes_untouched()
    test_writer_legacy_fp_text()
    test_writer_inserts_effects_and_prunes_justify()
    test_glasgow_mirror_preserved()
    print("All beautify_labels tests passed")
