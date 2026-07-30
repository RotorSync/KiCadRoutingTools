"""End-to-end meander demo chains (#501): SE trombone + diff centerline meanders.

Drives the REAL CLIs (route.py / route_diff.py) over two boards and asserts the
routed copper, not the logs:

  1. A synthetic 7-net board (generated here; the same generator that produced
     the ~/Downloads meander demo): two coupled diff pairs of different lengths
     (pair-to-pair centerline meanders) + three SE nets of different lengths
     (trombone group meanders). Asserts 2/2 coupled, 3/3 routed, DRC-clean,
     fully connected, and — the #501 regression — the SE meander riser pitch on
     copper is >= --meander-spacing x the track width (default 2W; also checked
     at an explicit non-default 3W to prove the flag reaches the geometry).
  2. kicad_files/lvds_converter_dualclk.kicad_pcb with --diff-pair-intra-match:
     asserts the intra-pair pass adds bumps and closes the P/N delta.

KiCad-10 format note (learned the hard way): a version >= 20250000 board has NO
top-level net table — nets exist only as (net "name") refs inside pads. A
top-level (net ...) block is parsed by KiCad as the legacy NUMERIC table and the
board refuses to load ("need a number for 'net number'"), while our parser
yields pcb.nets == {} for numeric declarations. Emit no table at all.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NETS = ["DP_A_P", "DP_A_N", "DP_B_P", "DP_B_N", "DP_C_P", "DP_C_N",
        "SE1", "SE2", "SE3"]

# (ref, x, y, net). DP_A is a straight 40mm pair; DP_B ~32mm (needs +8mm of
# centerline meanders to match); DP_C is a MULTIPOINT pair (3 terminals, two
# 20mm legs -- matched on its longest MST leg, #520); SE1/SE2/SE3 are
# 40/32/24mm (SE2+SE3 meander).
PADS = [
    ("P1", 10, 10, "DP_A_P"), ("P2", 10, 12, "DP_A_N"),
    ("P3", 50, 10, "DP_A_P"), ("P4", 50, 12, "DP_A_N"),
    ("P5", 14, 16, "DP_B_P"), ("P6", 14, 18, "DP_B_N"),
    ("P7", 46, 20, "DP_B_P"), ("P8", 46, 22, "DP_B_N"),
    ("P9", 10, 28, "SE1"), ("P10", 50, 28, "SE1"),
    ("P11", 14, 32, "SE2"), ("P12", 46, 32, "SE2"),
    ("P13", 18, 36, "SE3"), ("P14", 42, 36, "SE3"),
    ("P15", 10, 40, "DP_C_P"), ("P16", 10, 42, "DP_C_N"),
    ("P17", 30, 40, "DP_C_P"), ("P18", 30, 42, "DP_C_N"),
    ("P19", 50, 40, "DP_C_P"), ("P20", 50, 42, "DP_C_N"),
]


def write_synth_board(path):
    """Minimal KiCad-10 board: single-pad footprints + rectangular outline."""
    parts = ["""(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(25 "Edge.Cuts" user)
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)"""]
    for ref, x, y, name in PADS:
        parts.append(f'''\t(footprint "DemoPad"
\t\t(layer "F.Cu")
\t\t(uuid "{uuid.uuid4()}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 -1.5 0)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t\t(effects (font (size 1 1) (thickness 0.15)))
\t\t)
\t\t(property "Value" "DemoPad"
\t\t\t(at 0 1.5 0)
\t\t\t(layer "F.Fab")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t\t(effects (font (size 1 1) (thickness 0.15)))
\t\t)
\t\t(attr smd)
\t\t(pad "1" smd circle
\t\t\t(at 0 0)
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu" "F.Mask")
\t\t\t(net "{name}")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t)
\t)''')
    for x1, y1, x2, y2 in [(5, 5, 65, 5), (65, 5, 65, 45), (65, 45, 5, 45), (5, 45, 5, 5)]:
        parts.append(f'''\t(gr_line
\t\t(start {x1} {y1})
\t\t(end {x2} {y2})
\t\t(stroke (width 0.1) (type default))
\t\t(layer "Edge.Cuts")
\t\t(uuid "{uuid.uuid4()}")
\t)''')
    parts.append(")")
    with open(path, "w") as f:
        f.write("\n".join(parts) + "\n")


def _fail(msg):
    print("FAIL  " + msg)
    sys.exit(1)


def _run(args):
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        _fail(f"{args[0]} rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


def _json_summary(out):
    m = re.search(r"JSON_SUMMARY: (\{.*\})", out)
    return json.loads(m.group(1)) if m else {}


def min_riser_pitch(board, net_name, run_dir, min_len=0.2):
    """Min FINITE distance between same-net meander ARMS (#501's guarantee).

    Arms are the risers: segments PERPENDICULAR to the straight run they
    meander (run_dir), at least min_len long. The direction filter is what
    excludes the 45-degree chamfers — at wide pitches their diagonals grow
    long enough to pass any length cutoff and sit ~pitch/sqrt(2) apart, which
    is expected geometry, not an arm pair. Finite segment-to-segment distance
    (not line offset): welded joints measure ~0 (one electrical shape,
    skipped) and near-colinear copper far apart along the trace measures far."""
    from geometry_utils import segment_to_segment_distance
    ux, uy = run_dir
    n = math.hypot(ux, uy)
    ux, uy = ux / n, uy / n
    pcb = parse_kicad_pcb(board)
    nid = {n.name: i for i, n in pcb.nets.items()}[net_name]
    risers = []
    for s in pcb.segments:
        if s.net_id != nid:
            continue
        dx, dy = s.end_x - s.start_x, s.end_y - s.start_y
        ln = math.hypot(dx, dy)
        if ln >= min_len and abs((dx * ux + dy * uy) / ln) < 0.1:
            risers.append(s)
    worst = None
    for i, a in enumerate(risers):
        for b in risers[i + 1:]:
            d = segment_to_segment_distance(a.start_x, a.start_y, a.end_x, a.end_y,
                                            b.start_x, b.start_y, b.end_x, b.end_y)
            if d > 1e-6 and (worst is None or d < worst):
                worst = d
    return worst


def _grade(board, nets_arg=None):
    scope = ["--nets"] + nets_arg if nets_arg else []
    drc = _run(["check_drc.py", board] + scope)
    if "NO DRC VIOLATIONS" not in drc:
        _fail(f"DRC violations on {board}:\n{drc[-1500:]}")
    conn = _run(["check_connected.py", board] + scope)
    if "FULLY CONNECTED" not in conn:
        _fail(f"connectivity failure on {board}:\n{conn[-1500:]}")


def test_synth_demo(tmp, spacing=None, expect_pitch=None):
    """Route the synthetic board (diff chain then SE chain) and grade it."""
    tag = f"sp{spacing}" if spacing else "default"
    src = os.path.join(tmp, f"synth_{tag}.kicad_pcb")
    mid = os.path.join(tmp, f"synth_{tag}_dp.kicad_pcb")
    out = os.path.join(tmp, f"synth_{tag}_out.kicad_pcb")
    write_synth_board(src)
    sp = ["--meander-spacing", str(spacing)] if spacing else []

    dp_out = _run(["route_diff.py", src, mid,
                   "DP_A_P", "DP_A_N", "DP_B_P", "DP_B_N", "DP_C_P", "DP_C_N",
                   "--track-width", "0.2", "--clearance", "0.15", "--diff-pair-gap", "0.15",
                   "--length-match-group", "DP_A_*", "DP_B_*", "DP_C_*"] + sp)
    s = _json_summary(dp_out)
    if s.get("successful") != 3 or s.get("failed"):
        _fail(f"[{tag}] expected 3/3 pairs routed, got {s.get('successful')}/{s.get('failed')}")

    se_out = _run(["route.py", mid, out, "--nets", "SE1", "SE2", "SE3",
                   "--track-width", "0.2", "--clearance", "0.15",
                   "--length-match-group", "SE*"] + sp)
    s = _json_summary(se_out)
    if s.get("successful") != 3:
        _fail(f"[{tag}] expected 3/3 SE nets routed, got {s.get('successful')}")

    _grade(out)

    # The #501 guarantee, measured on the written copper: SE3 (the shortest net,
    # the most meandered) must keep its arms >= the demanded pitch apart.
    width = 0.2
    want = expect_pitch if expect_pitch else 2.0 * width
    pitch = min_riser_pitch(out, "SE3", run_dir=(1, 0))  # SE3 runs horizontally
    if pitch is None:
        _fail(f"[{tag}] SE3 has no meander risers at all")
    if pitch < want - 1e-6:
        _fail(f"[{tag}] SE3 arm pitch {pitch:.3f} < required {want:.3f}")
    # DP_B's legs are regenerated from the meandered centerline; its arms must
    # also never sit tighter than the SE guarantee. The grid router lays its
    # slanted connection as horizontal runs + 45-degree jogs, so the meandered
    # straight run is horizontal and the risers are vertical.
    dpp = min_riser_pitch(out, "DP_B_P", run_dir=(1, 0))
    if dpp is None:
        _fail(f"[{tag}] DP_B_P has no meander risers (centerline meanders missing)")
    if dpp < 2.0 * width - 1e-6:
        _fail(f"[{tag}] DP_B_P arm pitch {dpp:.3f} < {2.0 * width:.3f}")
    # DP_C is MULTIPOINT (two 20mm legs, span 20mm vs the 40mm target): the
    # group pass must meander its longest MST leg (#520).
    dpc = min_riser_pitch(out, "DP_C_P", run_dir=(1, 0))
    if dpc is None:
        _fail(f"[{tag}] DP_C_P has no meander risers (multipoint span not meandered)")
    if dpc < 2.0 * width - 1e-6:
        _fail(f"[{tag}] DP_C_P arm pitch {dpc:.3f} < {2.0 * width:.3f}")
    print(f"PASS  synth [{tag}]: 3/3 pairs + 3/3 SE, DRC-clean, connected; "
          f"SE3 pitch {pitch:.3f} DP_B_P pitch {dpp:.3f} DP_C_P pitch {dpc:.3f} (>= {want:.2f})")


def test_lvds_intra_pair(tmp):
    """Intra-pair P/N matching on the bundled lvds_converter board (SE-style
    meanders on one leg of a coupled pair, at the board's own 0.2 netclass)."""
    src = os.path.join(REPO, "kicad_files", "lvds_converter_dualclk.kicad_pcb")
    out = os.path.join(tmp, "lvds_intra.kicad_pcb")
    log = _run(["route_diff.py", src, out,
                "--diff-pair-gap", "0.25", "--track-width", "0.2", "--clearance", "0.2",
                "--diff-pair-intra-match"])
    m = re.search(r"intra-pair: (\d+) bumps added, new P=[\d.]+mm, new delta=([\d.]+)mm", log)
    if not m:
        _fail("intra-pair pass added no bumps on lvds_converter (expected /CLK leg meander)")
    bumps, delta = int(m.group(1)), float(m.group(2))
    if bumps < 1 or delta > 0.1:
        _fail(f"intra-pair match ineffective: {bumps} bumps, residual delta {delta}")
    print(f"PASS  lvds intra-pair: {bumps} bump(s), residual P/N delta {delta:.3f}mm")


def main():
    tmp = tempfile.mkdtemp(prefix="meander_demo_")
    test_synth_demo(tmp)
    # Non-default spacing must reach the geometry: 3W on a 0.2 track = 0.6mm.
    test_synth_demo(tmp, spacing=3.0, expect_pitch=0.6)
    test_lvds_intra_pair(tmp)
    print("PASS  all meander demo chains")


if __name__ == "__main__":
    main()
