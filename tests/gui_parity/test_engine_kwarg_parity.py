#!/usr/bin/env python3
"""Gate: every engine kwarg the CLI main passes must ALSO reach the GUI engine call.

This generalises the `coplanar_gap` gap (fixed c99ffb4/this sweep) into a guard.
It catches TWO distinct failure modes, which is the point -- fixing only the
first one is how that gap survived its own fix for an hour:

  CLASS 1 -- NOT PASSED.  The CLI main passes `foo=args.foo` to the engine and
      the GUI call site simply omits `foo`, so the engine default silently wins
      on one front. (differential_gui never passed coplanar_gap.)

  CLASS 2 -- PASSED BUT INERT.  The GUI call site passes
      `foo=config.get('foo', <default>)` but NOTHING EVER PUTS 'foo' IN THAT
      CONFIG, so it evaluates to the default forever and the control is dead.
      This is invisible to Class-1 checking -- the kwarg IS present at the call
      site -- and it is a REPEAT offender here: planes_gui read
      config['add_teardrops'] with no supplier (#489 s9), and the same was true
      of the diff tab for BOTH add_teardrops and coplanar_gap.

Class 1 is checked STATICALLY (ast, no wx) so it runs anywhere. Class 2 needs to
know whether a CONTROL exists for the key -- a key with no control legitimately
falls back to the same default the CLI's argparse uses (11 plane knobs are
deliberately unexposed) -- so it is checked LIVE against a real headless dialog,
and skips cleanly when wx/pcbnew are unavailable.

    python3 tests/gui_parity/test_engine_kwarg_parity.py

Exit 0 = parity. Exit 1 = a real divergence (printed with the file:line of both
sides). See CLAUDE.md "Keep CLI and GUI routing in sync".
"""

# ---------------------------------------------------------------------------
# macOS: if this HANGS at ~0% CPU, it is NOT wx, machine load, or a deadlock.
#
# After any wx process here is killed (a pkill, a timeout, a crash), macOS
# decides the app "quit unexpectedly", and the NEXT headless launch stops inside
# NSApplication bootstrap showing the restore-windows alert you cannot see:
#     -[NSPersistentUIRestorer promptToIgnorePersistentState]
#         -> -[NSAlert runModal]
# Headless, nobody can click it, so it waits forever: process state SN accruing
# ~0.3s of CPU over many minutes, which reads exactly like a hang. This cost a
# full session of ".gui-parity-checked" markers recording "wx blocked, gate NOT
# RUN" -- the gates were fine the whole time.
#
#   diagnose:  sample <pid> 3 -mayDie | grep -E "NSAlert|PersistentUI"
#   fix:       defaults write -g ApplePersistenceIgnoreState -bool YES
#
# A sandboxed HOME does NOT help -- cfprefsd serves that pref per-user
# regardless of HOME. With the default set, test_gui_engine_parity.py runs ~90s.
# ---------------------------------------------------------------------------
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
PLUG = os.path.join(REPO, "kicad_routing_plugin")

# (CLI module, GUI module, engine function)
PAIRS = [
    ("route.py",            "kicad_routing_plugin/swig_gui.py",         "batch_route"),
    ("route_diff.py",       "kicad_routing_plugin/differential_gui.py", "batch_route_diff_pairs"),
    ("route_planes.py",     "kicad_routing_plugin/planes_gui.py",       "create_plane"),
]

# Kwargs the CLI passes that the GUI legitimately does not, with the reason.
# Keep this list SHORT and justified -- every entry is a place the two fronts
# genuinely differ, not a place we gave up.
CLI_ONLY_OK = {
    # The CLI streams a route trace to build the stress movie (#482). The GUI has
    # its own recorder (movie_recorder.py) driven from the dialog, so it does not
    # hand the engine a callback.
    "vis_callback": "GUI uses movie_recorder.py instead of an engine callback (#482/#506)",
}

# Kwargs only the GUI passes: the in-memory/live-board plumbing that has no CLI
# equivalent (the CLI works file->file).
GUI_ONLY_OK = {
    "input_file", "output_file", "net_names", "pcb_data", "return_results",
    "progress_callback", "cancel_check", "net_clearances",
}

# ---------------------------------------------------------------------------
# KNOWN pre-existing Class-2 divergence, RECORDED so this gate catches NEW ones
# (the harness convention: gates lock in a fixed divergence, they don't hide it).
#
# EMPTY since #511: the Differential tab used to hand-maintain its own routing-
# config builder (~30 keys) while single-ended routing used
# swig_gui._build_routing_config (~83 keys), leaving 25 same-named Basic-tab
# controls dead for diff pairs -- plus a dozen MORE behind differently-named
# controls (keepout_check, time_matching_check, length_match_groups_ctrl,
# direction_choice, schematic_dir_ctrl, ...) that this gate could not even see.
# get_routing_config now DELEGATES to _build_routing_config, so the diff tab
# inherits every knob and cannot drift again. Any key landing here means a NEW
# dead control -- fix the supplier, don't grow this baseline.
KNOWN_DEAD_DIFF_KEYS = set()

# Config key -> dialog control attr, for keys whose control is not named after
# the key and not derivable by the _check/_ctrl/_choice suffix probe. Used only
# to decide "a control EXISTS for this key" in Class 2.
CTRL_NAME_ALIASES = {
    "keepout_enabled": "keepout_check",
    "can_swap_to_top_layer": "can_swap_to_top",
    "swappable_nets": "swappable_net_panel",
}


def _has_control(objs, key):
    """True if any dialog/tab object exposes a control for config key ``key``.

    Probes the key itself, the common wx naming suffixes (_check, _ctrl,
    _choice), and the explicit alias map -- the bare-hasattr version of this
    missed 12 real dead controls in the #511 sweep (keepout, time_matching,
    length_match_groups, ...), which is why they never made the baseline.
    """
    names = [key, key + "_check", key + "_ctrl", key + "_choice"]
    alias = CTRL_NAME_ALIASES.get(key)
    if alias:
        names.append(alias)
    return any(hasattr(o, n) for o in objs if o is not None for n in names)


def _calls(path, fname):
    """[(lineno, {kwarg names}, {kwarg -> config key read})] for each call of fname."""
    tree = ast.parse(open(path).read())
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        nm = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
        if nm != fname:
            continue
        names, keys = set(), {}
        for kw in n.keywords:
            if not kw.arg:
                continue
            names.add(kw.arg)
            v = kw.value
            # See through `not config.get('foo', ...)` (e.g. crossing_layer_check):
            # the negation hid the read from this gate, so 'no_crossing_layer_check'
            # never even made the #511 dead-key baseline.
            if isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not):
                v = v.operand
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get" and v.args
                    and isinstance(v.args[0], ast.Constant) and isinstance(v.args[0].value, str)):
                keys[kw.arg] = v.args[0].value
            elif isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant) \
                    and isinstance(v.slice.value, str):
                keys[kw.arg] = v.slice.value
        out.append((n.lineno, names, keys))
    return out


def _widest(calls):
    return max(calls, key=lambda c: len(c[1])) if calls else (0, set(), {})


def check_class1():
    """Every kwarg the CLI main passes is also passed at the GUI call site."""
    failures = []
    print("CLASS 1 -- kwargs the CLI passes but the GUI does not")
    for cli, gui, fn in PAIRS:
        cln, cset, _ = _widest(_calls(os.path.join(REPO, cli), fn))
        gln, gset, _ = _widest(_calls(os.path.join(REPO, gui), fn))
        if not cset or not gset:
            print(f"  {fn:24} SKIP (call site not found: cli={bool(cset)} gui={bool(gset)})")
            continue
        missing = sorted(k for k in (cset - gset) if k not in CLI_ONLY_OK)
        exempt = sorted(k for k in (cset - gset) if k in CLI_ONLY_OK)
        unexpected_gui = sorted(k for k in (gset - cset) if k not in GUI_ONLY_OK)
        status = "OK" if not missing else "FAIL"
        print(f"  {fn:24} {status}  cli={cli}:{cln} ({len(cset)}) gui={gui}:{gln} ({len(gset)})")
        for k in exempt:
            print(f"      exempt: {k} -- {CLI_ONLY_OK[k]}")
        for k in missing:
            failures.append(f"{fn}: CLI passes '{k}' ({cli}:{cln}) but GUI does not ({gui}:{gln})")
        for k in unexpected_gui:
            print(f"      note: GUI-only kwarg '{k}' (not in the structural allowlist)")
    return failures


def check_class2():
    """Every config key a GUI call site READS must be SUPPLIED, when a control exists."""
    print("\nCLASS 2 -- GUI reads a config key nothing supplies (dead control)")
    try:
        import wx  # noqa: F401
        from kicad_parser import parse_kicad_pcb
        from kicad_routing_plugin import swig_gui
    except Exception as e:
        print(f"  SKIP (needs KiCad python: {type(e).__name__}: {e})")
        print("  Run under KiCad's python to cover this class:")
        print("    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
              "Versions/Current/bin/python3 tests/gui_parity/test_engine_kwarg_parity.py")
        return []

    os.environ.setdefault("WXSUPPRESS_SIZER_FLAGS_CHECK", "1")
    board = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")
    app = wx.App(False)  # noqa: F841 -- must outlive the dialog
    dlg = swig_gui.RoutingDialog(None, parse_kicad_pcb(board), board)
    try:
        # (engine fn, GUI module, live config the call site actually sees)
        d = dlg.differential_tab
        p = dlg.planes_tab
        supplies = [
            ("batch_route_diff_pairs", "kicad_routing_plugin/differential_gui.py",
             {**d.get_routing_config(), **d.get_config()}),
            ("create_plane", "kicad_routing_plugin/planes_gui.py",
             {**p.create_options.get_config(), **p.get_shared_params()}),
        ]
        failures = []
        for fn, gui, cfg in supplies:
            _, _, keys = _widest(_calls(os.path.join(REPO, gui), fn))
            dead = []
            for kwarg, key in sorted(keys.items()):
                if key in cfg:
                    continue
                # No control for that key => the default fallback IS the design
                # (the CLI's argparse default matches); only a key with a real
                # control behind it is a dead control.
                has_ctrl = _has_control((dlg, d, p,
                                         getattr(p, "create_options", None),
                                         getattr(p, "repair_options", None)), key)
                if has_ctrl:
                    dead.append((kwarg, key))
            known = [x for x in dead if x[1] in KNOWN_DEAD_DIFF_KEYS]
            new = [x for x in dead if x[1] not in KNOWN_DEAD_DIFF_KEYS]
            print(f"  {fn:24} {'FAIL' if new else 'OK'}  ({len(keys)} config-read kwargs, "
                  f"{len(cfg)} keys supplied, {len(known)} known-dead recorded)")
            for kwarg, key in new:
                failures.append(f"{fn}: reads config['{key}'] for kwarg '{kwarg}' but no "
                                f"builder supplies it, while a control named '{key}' EXISTS "
                                f"-- NEW dead control ({gui})")
            # A recorded key that came back to life should be removed from the
            # baseline, or the gate silently under-reports forever.
            revived = sorted(KNOWN_DEAD_DIFF_KEYS - {k for _, k in dead}) if fn == \
                "batch_route_diff_pairs" else []
            if revived:
                print(f"      note: {len(revived)} recorded key(s) now supplied -- "
                      f"drop from KNOWN_DEAD_DIFF_KEYS: {revived}")
        failures += check_coplanar_case(dlg)
        failures += check_delegation_case(dlg)
        return failures
    finally:
        dlg.Destroy()


def check_coplanar_case(dlg):
    """The #486 coplanar diff-pair case, end to end through the REAL dialog.

    Sets the shared Coplanar Gap + Add-teardrops controls the way a user would
    for a coplanar differential run and asserts BOTH reach the diff engine call:
    the value must land in the merged config the diff tab hands the engine (the
    Class-2 half), and the call site must forward it (the Class-1 half). Guards
    the exact regression fixed this sweep -- the shared control was honoured for
    single-ended routing and silently dropped for coupled pairs, shipping them
    microstrip-wide against a CPW impedance target.
    """
    print("\nCOPLANAR DIFF-PAIR CASE (#486, the c99ffb4 regression)")
    out = []
    d = dlg.differential_tab
    dlg.coplanar_gap.SetValue(0.25)
    dlg.add_teardrops_check.SetValue(True)
    cfg = {**d.get_routing_config(), **d.get_config()}
    for key, want in (("coplanar_gap", 0.25), ("add_teardrops", True)):
        got = cfg.get(key, "<ABSENT>")
        ok = got == want
        print(f"  control -> diff config: {key:16} = {got!r} (want {want!r})  {'OK' if ok else 'FAIL'}")
        if not ok:
            out.append(f"coplanar case: diff config {key}={got!r}, expected {want!r} "
                       f"-- the control does not reach batch_route_diff_pairs")
    _, names, _ = _widest(_calls(os.path.join(REPO, "kicad_routing_plugin/differential_gui.py"),
                                 "batch_route_diff_pairs"))
    for kw in ("coplanar_gap",):
        ok = kw in names
        print(f"  call site forwards:     {kw:16} {'OK' if ok else 'FAIL (not passed to the engine)'}")
        if not ok:
            out.append(f"coplanar case: differential_gui does not pass '{kw}' to the engine")
    return out


def check_delegation_case(dlg):
    """The #511 delegation, end to end through the REAL dialog.

    Sets NON-DEFAULT values on a sample of the controls that used to be dead
    for diff pairs -- same-named ones from the old 25-key baseline AND the
    differently-named ones the old bare-hasattr probe could not even see --
    and asserts each value lands in the merged config the diff tab hands
    batch_route_diff_pairs, and that the call site reads that key. A wiring
    fix can be INERT (the c99ffb4 lesson): observing the non-default VALUE
    arrive is the test, not the wiring's existence.
    """
    print("\n#511 DELEGATION CASE (formerly-dead controls reach the diff engine)")
    out = []
    d = dlg.differential_tab

    def spin(ctrl, val):
        ctrl.SetValue(val)
        return ctrl.GetValue()  # post-clamp actual, so a range clamp can't lie

    cases = []
    # Same-named controls (were in the 25-key baseline):
    cases.append(("crossing_penalty", spin(dlg.crossing_penalty, 12.5)))
    cases.append(("length_match_tolerance", spin(dlg.length_match_tolerance, 3.25)))
    dlg.mps_layer_swap.SetValue(True); cases.append(("mps_layer_swap", True))
    dlg.bus_enabled.SetValue(True); cases.append(("bus_enabled", True))
    # Differently-named controls (invisible to the old control probe):
    dlg.keepout_check.SetValue(True); cases.append(("keepout_enabled", True))
    dlg.time_matching_check.SetValue(True); cases.append(("time_matching", True))
    dlg.length_match_groups_ctrl.SetValue("LMG_A* LMG_B*")
    cases.append(("length_match_groups", [["LMG_A*", "LMG_B*"]]))
    dlg.direction_choice.SetSelection(1); cases.append(("direction", "forward"))

    cfg = {**d.get_routing_config(), **d.get_config()}
    for key, want in cases:
        got = cfg.get(key, "<ABSENT>")
        ok = got == want
        print(f"  control -> diff config: {key:24} = {got!r} (want {want!r})  {'OK' if ok else 'FAIL'}")
        if not ok:
            out.append(f"#511 case: diff config {key}={got!r}, expected {want!r} "
                       f"-- the control does not reach batch_route_diff_pairs")
    _, _, keys = _widest(_calls(os.path.join(REPO, "kicad_routing_plugin/differential_gui.py"),
                                "batch_route_diff_pairs"))
    read = set(keys.values())
    for key, _ in cases:
        ok = key in read
        print(f"  call site reads:        {key:24} {'OK' if ok else 'FAIL (no kwarg reads this key)'}")
        if not ok:
            out.append(f"#511 case: differential_gui call site no longer reads config['{key}']")
    return out


def main():
    failures = check_class1() + check_class2()
    print("\n" + "=" * 72)
    if failures:
        print(f"ENGINE KWARG PARITY: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ENGINE KWARG PARITY: OK (no CLI-only kwargs, no dead controls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
