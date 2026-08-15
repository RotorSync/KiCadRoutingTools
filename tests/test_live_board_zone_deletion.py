"""A zone deleted on the LIVE board must not be resurrected from the file.

create_plane's existing-zone check used to UNION the input file's zones with the
caller-supplied pcb_data's. That catches a zone added live but not yet saved --
but a union can only ADD, so the opposite edit was invisible: delete a zone by
hand in KiCad, press Create Plane, and the unsaved-to-disk file still supplied
the deleted zone. skip_existing_zones then kept "it already exists" and NO plane
was ever poured, with the dialog reporting a bland zero.

When the caller supplies pcb_data it IS the board, and it alone is authoritative.
No pcbnew needed: a PCBData parsed from a file with its .zones emptied is exactly
what build_pcb_data_from_board returns for a live board whose zone was deleted.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "py_router"))
sys.path.insert(0, os.path.join(ROOT, "py_tools"))
sys.path.insert(0, os.path.join(ROOT, "rust_router"))

from kicad_parser import parse_kicad_pcb          # noqa: E402
from route_planes import create_plane             # noqa: E402

BOARD = os.path.join(ROOT, "kicad_files", "splitflap_driver.kicad_pcb")


def _pour(tmp):
    """Produce a board that HAS a GND zone on B.Cu, the way the CLI would."""
    src = os.path.join(tmp, "in.kicad_pcb")
    out = os.path.join(tmp, "poured.kicad_pcb")
    shutil.copy2(BOARD, src)
    pro = BOARD[:-len(".kicad_pcb")] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copy2(pro, src[:-len(".kicad_pcb")] + ".kicad_pro")
    create_plane(input_file=src, output_file=out, net_names=["GND"],
                 plane_layers=["B.Cu"])
    assert parse_kicad_pcb(out).zones, "setup failed: no zone poured"
    return out


def _new_zone_count(input_file, pcb_data):
    res = create_plane(input_file=input_file, output_file="", net_names=["GND"],
                       plane_layers=["B.Cu"], dry_run=True, return_results=True,
                       pcb_data=pcb_data, skip_existing_zones=True)
    return len(res[5])          # new_zones


def test_zone_deleted_live_is_poured_again():
    """THE BUG: file still has the zone, live board does not -> must pour."""
    with tempfile.TemporaryDirectory() as tmp:
        poured = _pour(tmp)
        live = parse_kicad_pcb(poured)
        live.zones = []                                  # user deleted it by hand
        n = _new_zone_count(poured, live)
    assert n == 1, (f"expected 1 new zone after the live board's zone was deleted, "
                    f"got {n} -- the file's stale zone is being resurrected")


def test_zone_present_live_is_still_skipped():
    """The converse must not regress: still there live -> keep it, pour nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        poured = _pour(tmp)
        live = parse_kicad_pcb(poured)
        assert live.zones, "setup failed: expected a live zone"
        n = _new_zone_count(poured, live)
    assert n == 0, f"expected 0 new zones when the zone is still present, got {n}"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
