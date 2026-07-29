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
# ROOT CAUSE: the Differential tab hand-maintains its own routing-config builder
# (swig_gui.get_routing_config, ~30 keys, defined inside _create_differential_tab)
# while single-ended routing uses swig_gui._build_routing_config (~83 keys). 53
# keys exist only in the latter; the 25 below are ones differential_gui actually
# READS at its engine call, so those Basic-tab controls apply to single-ended
# routing but are silently ignored for coupled pairs -- while route_diff.py's
# argparse DOES pass every one of them.
#
# coplanar_gap and add_teardrops were two more instances, fixed this sweep by
# adding them to get_routing_config. The structural fix is to make
# get_routing_config delegate to _build_routing_config so the diff tab inherits
# every knob and cannot drift again -- deliberately NOT done here because it
# would make 25 currently-ignored controls take effect, changing GUI diff-routing
# results. That is a product decision, not a parity cleanup.
#
# To re-measure: empty this set and re-run.
KNOWN_DEAD_DIFF_KEYS = {
    "bga_proximity_cost", "bga_proximity_radius", "bus_attraction_bonus",
    "bus_attraction_radius", "bus_detection_radius", "bus_enabled", "bus_min_nets",
    "crossing_penalty", "keep_input_copper", "length_match_tolerance",
    "meander_amplitude", "mps_layer_swap", "mps_reverse_rounds",
    "mps_segment_intersection", "ripped_route_avoidance_cost",
    "ripped_route_avoidance_radius", "routing_clearance_margin",
    "stub_proximity_cost", "stub_proximity_radius", "time_match_tolerance",
    "track_proximity_cost", "track_proximity_distance", "vertical_attraction_cost",
    "vertical_attraction_radius", "via_proximity_cost",
}


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
                # No control of that name => the default fallback IS the design
                # (the CLI's argparse default matches); only a key with a real
                # control behind it is a dead control.
                has_ctrl = any(hasattr(o, key) for o in (dlg, d, p,
                                                         getattr(p, "create_options", None),
                                                         getattr(p, "repair_options", None))
                               if o is not None)
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
