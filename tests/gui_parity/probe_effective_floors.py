"""Does the REAL dialog resolve plane track_width from the board's Default
netclass (CLI parity), or fall back to defaults.TRACK_WIDTH like the shim?"""
import os, sys
os.environ.setdefault('WXSUPPRESS_SIZER_FLAGS_CHECK', '1')
# Repo root from THIS file's location (tests/gui_parity/ -> repo), never a
# hardcoded home dir -- the sibling gates all derive it this way.
R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R); sys.path.insert(0, os.path.dirname(R))
import wx, pcbnew, json
import routing_defaults as defaults
from kicad_parser import build_pcb_data_from_board
from kicad_routing_plugin import swig_gui

board_path = sys.argv[1]
app = wx.App(False)
board = pcbnew.LoadBoard(board_path)
pcbnew.GetBoard = lambda: board
pd = build_pcb_data_from_board(board)
dlg = swig_gui.RoutingDialog(None, pd, board_path)

pro = os.path.splitext(board_path)[0] + '.kicad_pro'
proj = None
if os.path.exists(pro):
    for c in json.load(open(pro)).get('net_settings', {}).get('classes', []):
        if c.get('name') == 'Default':
            proj = c.get('track_width')
print(f'board .kicad_pro Default track_width : {proj}')
print(f'defaults.TRACK_WIDTH (shim fallback) : {defaults.TRACK_WIDTH}')
print(f'track_width override checkbox        : {dlg.track_width_check.GetValue()}')
print(f'raw control value                    : {dlg.track_width.GetValue()}')
print(f'_effective_track_width()             : {dlg._effective_track_width()}')
print(f'planes tab shared track_width        : {dlg.planes_tab.get_shared_params().get("track_width")}')
