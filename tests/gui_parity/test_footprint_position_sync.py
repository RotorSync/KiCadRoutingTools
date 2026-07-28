#!/usr/bin/env python3
"""Regression gate for the stale-cap-position bug (#362), IPC edition.

The GUI parses pcb_data ONCE when the dialog opens and reuses it across plan
steps. optimize_caps (fanout tab) relocates decoupling caps, but a cached
pcb_data keeps their load-time positions -- so a LATER route step routes around
where a cap USED to be, and the moved cap's pad shorts the fresh copper.

The SWIG plugin patched the cached objects in place
(gui_utils.sync_footprint_positions_from_board, which had its own trap: pads had
to be matched by ITERATION ORDER, since one footprint routinely carries many
pads sharing a number -- U6 has 11 pads numbered "61"). The IPC plugin has no
such helper and does not need one: _sync_pcb_data_from_board REBUILDS PCBData
from the board wholesale, so there is no stale cached object to correct and no
pad-matching to get wrong.

What still has to hold is the end-to-end guarantee, and that is what this gate
now asserts: after apply_footprint_moves, the position a FOLLOWING step sees
(i.e. a fresh build_pcb_data_from_board) is the NEW one, for the footprint and
for the pads_by_net view the router actually consults.

That is a real risk on IPC rather than a formality -- the move only reaches the
next step by way of the board, so anything that drops or coarsens it on the way
(e.g. the placement writer's %g coordinate formatting, which quantised any
coordinate past 100mm to 0.1mm) shows up here.

Needs wx + kipy (KiCad's python plus `pip install --user kicad-python`); skips
if absent. Run: python3 tests/gui_parity/test_footprint_position_sync.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\\Program Files\\KiCad\\bin\\python.exe"),
]


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand != sys.executable and os.path.exists(cand):
            if subprocess.run([cand, '-c', 'import wx, kipy'],
                              capture_output=True).returncode == 0:
                os.execv(cand, [cand, os.path.abspath(__file__)] + sys.argv[1:])
    print("SKIP: no python with wx + kipy found "
          "(install kipy: <kicad_python> -m pip install --user kicad-python)")
    sys.exit(0)


def main():
    try:
        import kipy  # noqa: F401
    except ImportError:
        _reexec_into_kicad()

    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kicad_ipc_adapter
    from fake_ipc_board import install as _install_fake_board, build_pcb_data_like_ipc

    src = os.path.join(REPO, 'kicad_files', 'rp2350_fpga_eensy_prePlane.kicad_pcb')
    if not os.path.exists(src):
        print(f"SKIP: {src} not found")
        sys.exit(0)

    # Work on a copy: this moves a part.
    workdir = tempfile.mkdtemp(prefix='fp_sync_')
    board_path = os.path.join(workdir, os.path.basename(src))
    shutil.copy(src, board_path)
    pro = os.path.splitext(src)[0] + '.kicad_pro'
    if os.path.exists(pro):   # the .kicad_pro carries the DRC floor (#441)
        shutil.copy(pro, os.path.splitext(board_path)[0] + '.kicad_pro')

    board = _install_fake_board(board_path)
    pcb = build_pcb_data_like_ipc(board_path)

    # Pick any footprint that has a pad, to relocate.
    ref = None
    for r, fp in pcb.footprints.items():
        if fp.pads:
            ref = r
            break
    assert ref is not None, "no footprint with pads on the board"
    fp = pcb.footprints[ref]
    before_fp = (fp.x, fp.y)
    before_pad = (fp.pads[0].global_x, fp.pads[0].global_y)

    # Move it, exactly as optimize_caps does (#130).
    dx_mm, dy_mm = 0.2, -0.35
    n = kicad_ipc_adapter.apply_footprint_moves(board, [{
        'reference': ref,
        'new_x': before_fp[0] + dx_mm,
        'new_y': before_fp[1] + dy_mm,
        'new_rotation': fp.rotation or 0.0,
    }])
    assert n == 1, f"apply_footprint_moves reported {n} moves, expected 1"

    # What the NEXT plan step sees.
    fresh = build_pcb_data_like_ipc(board_path)
    ffp = fresh.footprints[ref]
    assert abs((ffp.x - before_fp[0]) - dx_mm) < 1e-6 and \
           abs((ffp.y - before_fp[1]) - dy_mm) < 1e-6, (
        f"{ref} expected ({dx_mm:+}, {dy_mm:+})mm, got "
        f"({ffp.x - before_fp[0]:+.6f}, {ffp.y - before_fp[1]:+.6f}) -- the move "
        f"was lost or coarsened on the way to the board")

    # The router consults pads_by_net, so the PAD must have moved with it.
    fpad = ffp.pads[0]
    assert abs((fpad.global_x - before_pad[0]) - dx_mm) < 1e-6 and \
           abs((fpad.global_y - before_pad[1]) - dy_mm) < 1e-6, (
        f"{ref} pad0 did not follow the footprint: got "
        f"({fpad.global_x - before_pad[0]:+.6f}, "
        f"{fpad.global_y - before_pad[1]:+.6f})")
    same = None
    for _nid, pads in fresh.pads_by_net.items():
        for p in pads:
            if p is fpad:
                same = p
                break
        if same:
            break
    assert same is not None, f"{ref} pad0 not present in pads_by_net"

    shutil.rmtree(workdir, ignore_errors=True)
    print(f"PASS: {ref} moved ({dx_mm:+}, {dy_mm:+})mm and a freshly built "
          f"PCBData -- footprint, pad, and pads_by_net -- reports the new "
          f"position exactly.")


if __name__ == '__main__':
    main()
