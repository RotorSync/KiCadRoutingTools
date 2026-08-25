import sys, os
# Simulate a clean-worktree import: only quality/ on the path, no py_router.
sys.path.insert(0, '/home/austin/krt_work/quality')
# Block si_classes import to test the fallback
import builtins
real_import = builtins.__import__
def fake_import(name, *a, **k):
    if name == 'si_classes':
        raise ImportError('si_classes blocked for test')
    return real_import(name, *a, **k)
builtins.__import__ = fake_import

import score
# Build a minimal PCBData and call metric_si_coupling
from kicad_parser import PCBData, BoardInfo
info = BoardInfo(layers={0:'F.Cu',1:'B.Cu'}, copper_layers=['F.Cu','B.Cu'])
pcb = PCBData(board_info=info, nets={}, footprints={}, vias=[], segments=[], pads_by_net={})
r = score.metric_si_coupling(pcb)
print('fallback result:', r)
