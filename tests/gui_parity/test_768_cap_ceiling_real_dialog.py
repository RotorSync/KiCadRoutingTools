#!/usr/bin/env python3
"""#768 on the REAL headless dialog: what value of `netclass_ceiling` does the
cap pass actually receive, on BOTH GUI call paths?

    python3 tests/gui_parity/test_768_cap_ceiling_real_dialog.py

(re-execs into KiCad's bundled python automatically, like its siblings)

THIS FILE EXISTS BECAUSE THE SOURCE-TEXT HALF COULD NOT CATCH THE DEFECT IT WAS
NAMED FOR. `tests/test_768_cap_clearance_ceiling.py` asserted that the gate
expression mentions a config key and that the key appears once per file. Both
were true of a gate that was WRONG (it read `fix_drc_settings`) and, on one of
the two call paths, INERT (that key is not in the config that path builds). Only
capturing the kwargs the engine is handed, from the real dialog, distinguishes
those.

THE RULE. The CLI switches the ceiling on the PRESENCE of `--clearance`. A
dialog has no absent value, so the switch has to be the control that already
means "I am overriding the board's clearance": the Basic tab's Min Clearance
override, exported as `clamp_netclasses` (`swig_gui.py`, `self.clearance_check`)
and consumed as `clamp_nondefault_netclasses` by the signal, planes and diff
steps. `ai_plan.py` states the same equivalence where it clamps.

WHY NOT `fix_drc_settings`, which the first cut of #768 used: measured, that box
does not clamp a net class at all. `update_live_drc_floors` writes
`m_MinClearance` and the DEFAULT class only, carries no
`clamp_nondefault_netclasses`, and this tab never calls
`apply_targets_to_board`. Gated there, the GUI priced every pair at the ceiling
and clamped nothing -- the GIVEN branch's pricing with the OMITTED branch's
writeback, which is #768 pointing the other way.

TWO PATHS, and the first cut was correct on one and inert on the other:
  inline      `_apply_fanout_results` -> `_optimize_decoupling_caps(fanout_config)`
  standalone  `run_cap_optimization` -> builds its OWN cfg from a handful of
              shared keys, then calls the same method. The plan executor's
              `optimize_caps` step uses this one.
Both are driven below. A gate that reads a key the standalone cfg does not carry
looks right inline and does nothing where it matters.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

KICAD_PYTHONS = [
    '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/'
    'Versions/Current/bin/python3',
    '/usr/bin/python3',
    r'C:\Program Files\KiCad\10.0\bin\python.exe',
]


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew, wx'],
                          capture_output=True).returncode == 0:
            argv = [cand, os.path.abspath(__file__)] + sys.argv[1:]
            if os.name == 'nt':
                sys.exit(subprocess.run(argv).returncode)
            os.execv(cand, argv)
    print("SKIP: no python with pcbnew+wx found")
    sys.exit(0)


def main():
    try:
        import wx  # noqa: F401
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()

    os.environ.setdefault('WXSUPPRESS_SIZER_FLAGS_CHECK', '1')
    import wx
    sys.path.insert(0, REPO)
    for sub in ('py_router', 'py_placer', 'py_tools'):
        sys.path.insert(0, os.path.join(REPO, sub))
    sys.path.insert(0, os.path.dirname(REPO))

    app = wx.App(False)  # noqa: F841
    # flat_hierarchy is the repo's only tracked board declaring a NON-Default
    # class (Default 0.2, Wide 0.4). A board with one class cannot tell a
    # working ceiling from a broken one, because there is nothing to cap.
    board = os.path.join(REPO, 'kicad_files', 'flat_hierarchy.kicad_pcb')
    from kicad_parser import parse_kicad_pcb
    from kicad_routing_plugin.swig_gui import RoutingDialog

    dlg = RoutingDialog(None, parse_kicad_pcb(board), board)
    tab = dlg.fanout_tab
    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(name)
        print(("  PASS " if cond else "  FAIL ") + name
              + (f"  {detail}" if detail else ""))

    # -- capture the kwargs, run nothing ------------------------------------
    seen = {}

    def _spy(pcb_data, **kw):
        seen.clear()
        seen.update(kw)
        raise _Stop()

    class _Stop(Exception):
        pass

    # The tab imports the engine INSIDE the method
    # (`from placement.fanout_clearance import repair_fanout_clearance`), so the
    # spy has to sit on the source module, not on `fanout_gui`. Patching the
    # wrong one raises AttributeError rather than silently recording nothing --
    # which is the failure mode this file is guarding against elsewhere.
    from placement import fanout_clearance as _fc
    real = _fc.repair_fanout_clearance

    import pcbnew as _pcbnew
    # `pcbnew.GetBoard()` is None outside the KiCad process, and
    # `_optimize_decoupling_caps` returns early on a None board -- so the engine
    # is never reached and every kwarg reads back as absent. Load the fixture
    # and hand the tab THAT board. The first run of this file "passed" four
    # checks against an empty dict for exactly this reason, which is the
    # missing-input false pass `run_utils.evidence` exists to refuse.
    live = _pcbnew.GetBoard() or _pcbnew.LoadBoard(board)
    if live is None:
        print("SKIP: pcbnew could not load the fixture board")
        return 0

    ABSENT = '<<absent>>'

    def _drive(cfg):
        """Call the tab's cap step with `cfg` and return the engine kwargs.

        Raises if the engine was not reached: a check whose input is missing
        tests nothing, and `.get(k)` returning None for an ABSENT key is
        indistinguishable from the value this file is asserting."""
        seen.clear()
        _fc.repair_fanout_clearance = _spy
        try:
            try:
                tab._optimize_decoupling_caps(live, _pcbnew, cfg)
            except _Stop:
                pass
            except Exception as e:                            # noqa: BLE001
                if not seen:
                    raise AssertionError(
                        "the engine was not reached (%s: %s) -- this check "
                        "would have read every kwarg as absent"
                        % (type(e).__name__, str(e)[:120]))
            if not seen:
                raise AssertionError(
                    "the engine was not reached and nothing raised: the tab "
                    "returned early, so no kwarg below means anything")
            return dict(seen)
        finally:
            _fc.repair_fanout_clearance = real

    # -- 1. the shared params carry the override at all ---------------------
    shared = tab.get_shared_params() if tab.get_shared_params else {}
    check("the fanout tab's shared params carry clamp_netclasses",
          'clamp_netclasses' in shared,
          "keys=%d" % len(shared))

    # -- 2. the INLINE path, both positions of the override -----------------
    for ticked in (False, True):
        cfg = dict(shared)
        cfg['clamp_netclasses'] = ticked
        cfg['clearance'] = 0.2
        kw = _drive(cfg)
        got = kw.get('netclass_ceiling', ABSENT)
        want = 0.2 if ticked else None
        check("inline: override %s -> ceiling %r"
              % ('CHECKED' if ticked else 'unchecked', want),
              got == want, "got %r" % (got,))

    # -- 3. the STANDALONE path, which builds its own cfg -------------------
    # This is the one the plan executor uses, and the one an earlier cut of
    # #768 left inert by gating on a key this cfg never carried.
    for ticked in (False, True):
        dlg.clearance_check.SetValue(ticked)
        shared2 = tab.get_shared_params()
        cfg = dict(tab.bga_options.get_config())
        cfg.update({
            'clearance': shared2.get('clearance'),
            'grid_step': shared2.get('grid_step'),
            'via_size': shared2.get('via_size'),
            'clamp_netclasses': shared2.get('clamp_netclasses', False),
            'fix_drc_settings': shared2.get('fix_drc_settings', True),
        })
        check("standalone: shared params report override %s"
              % ('CHECKED' if ticked else 'unchecked'),
              bool(shared2.get('clamp_netclasses')) == ticked,
              "clamp_netclasses=%r" % (shared2.get('clamp_netclasses'),))
        kw = _drive(cfg)
        got = kw.get('netclass_ceiling', ABSENT)
        check("standalone: override %s -> ceiling is %s"
              % ('CHECKED' if ticked else 'unchecked',
                 'the clearance' if ticked else 'None'),
              (got is not None) == ticked, "got %r" % (got,))

    # -- 4. the gate is NOT fix_drc_settings --------------------------------
    # The change detector for the defect this file was written after: with the
    # override UNCHECKED, no value of the Fix-DRC box may produce a ceiling.
    for fix in (False, True):
        cfg = dict(shared)
        cfg['clamp_netclasses'] = False
        cfg['fix_drc_settings'] = fix
        cfg['clearance'] = 0.2
        kw = _drive(cfg)
        got = kw.get('netclass_ceiling', ABSENT)
        check("fix_drc_settings=%r cannot conjure a ceiling on its own" % fix,
              got is None, "got %r" % (got,))

    # -- 5. an ABSENT key means "honour the board" --------------------------
    cfg = dict(shared)
    cfg.pop('clamp_netclasses', None)
    cfg['clearance'] = 0.2
    kw = _drive(cfg)
    got = kw.get('netclass_ceiling', ABSENT)
    check("an absent clamp_netclasses defaults to NO ceiling",
          got is None, "got %r" % (got,))

    # -- 6. and the flat floor is unaffected by the switch ------------------
    # The operator's number stays the pair floor either way; only whether the
    # net CLASSES are capped by it moves.
    cfg = dict(shared)
    cfg['clamp_netclasses'] = False
    cfg['clearance'] = 0.2
    kw = _drive(cfg)
    check("the flat clearance is handed over regardless of the switch",
          kw.get('clearance') == 0.2, "got %r" % (kw.get('clearance'),))

    # -- 7. the plan executor must not turn OMITTED into GIVEN --------------
    # `optimize_caps` deliberately skips the per-step reset so it inherits the
    # preceding fanout step's controls, which is right for the VALUE. Since
    # #768 the PRESENCE of --clearance is a semantic switch, so a step carrying
    # no `clearance` param must also clear the override, or an omitted flag
    # arrives at the engine as a ceiling. Replays the executor's own rule.
    def _executor_rule(step):
        if step["action"] == "optimize_caps" and not (
                step.get("params") or {}).get("clearance"):
            cc = getattr(dlg, 'clearance_check', None)
            if cc is not None and cc.GetValue():
                cc.SetValue(False)
        return bool(getattr(dlg, 'clearance_check').GetValue())

    dlg.clearance_check.SetValue(True)          # the fanout step ticked it
    check("plan: optimize_caps with NO clearance param clears the override",
          _executor_rule({"action": "optimize_caps"}) is False)
    dlg.clearance_check.SetValue(True)
    check("plan: optimize_caps WITH a clearance param keeps it",
          _executor_rule({"action": "optimize_caps",
                          "params": {"clearance": 0.1}}) is True)
    # and the rule as SHIPPED, read off the executor rather than re-implemented
    _plan_src = open(os.path.join(REPO, 'kicad_routing_plugin', 'ai_plan.py'),
                     encoding='utf-8').read()
    check("the executor actually carries that rule",
          'clearance_check' in _plan_src
          and 'optimize_caps' in _plan_src
          and ").get(\"clearance\")" in _plan_src)

    print()
    if failures:
        print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
