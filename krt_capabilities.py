#!/usr/bin/env python3
"""What this clone can actually do, as JSON.

A board repository pins a KiCadRoutingTools clone through an environment
variable and then routes with it. Today the strongest check available to that
consumer is "does route.py exist as a file" -- which passes for a clone that is
years old, on the wrong branch, or missing the module the chain depends on. The
run then completes, prints green, and describes an engine the repo does not
pin. That is the one failure a no-fallbacks rule exists to prevent, and nothing
detects it.

So: publish the capability set and let the consumer assert against it.

    python3 krt_capabilities.py                 # everything, as JSON
    python3 krt_capabilities.py --require route.py:--track-width-floor board_score
    python3 route.py --capabilities             # same JSON, from the router

`--require` takes `module` or `module:--flag` tokens and exits non-zero listing
everything missing, so a consumer's check is one line and its failure message
names the gap instead of the symptom.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# The modules a consumer is likely to depend on by name. Presence is reported
# for every one; absence is only an ERROR when --require asks for it.
KNOWN_MODULES = (
    'route.py', 'route_diff.py', 'route_planes.py', 'route_disconnected_planes.py',
    'place_optimize.py', 'place_route_loop.py', 'place_fanout_clearance.py',
    'bga_fanout.py', 'qfn_fanout.py',
    'check_drc.py', 'check_connected.py', 'check_floorplan.py',
    'check_impedance.py', 'check_orphan_stubs.py', 'check_pads.py',
    'kicad_unconnected.py', 'net_forensics.py', 'copy_board.py',
    'make_movie.py', 'render_placement.py', 'list_nets.py', 'route_summary.py',
)

# Scripts whose flag set a consumer may want to pin.
FLAG_SCRIPTS = ('route.py', 'route_diff.py', 'route_planes.py',
                'route_disconnected_planes.py', 'place_route_loop.py',
                'place_optimize.py', 'check_drc.py', 'check_floorplan.py')

_FLAG_RE = re.compile(r'add_argument\(\s*["\'](--[A-Za-z0-9][A-Za-z0-9-]*)["\']')


def script_flags(path):
    """Long flags a script's argparse defines.

    Read from the source rather than by importing and building the parser:
    importing runs module-level code, and a consumer asking "can this clone do
    X" must not be able to trigger a side effect by asking.
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return sorted(set(_FLAG_RE.findall(f.read())))
    except OSError:
        return []


def capabilities(root=ROOT):
    mods = {m: os.path.isfile(os.path.join(root, m)) for m in KNOWN_MODULES}
    flags = {s: script_flags(os.path.join(root, s))
             for s in FLAG_SCRIPTS if mods.get(s)}
    out = {
        'schema': 1,
        'root': root,
        'is_git_clone': os.path.exists(os.path.join(root, '.git')),
        'modules': mods,
        'flags': flags,
    }
    try:                                    # best-effort, never fatal
        import routing_defaults as _d
        out['version'] = getattr(_d, 'VERSION', None)
    except Exception:
        out['version'] = None
    return out


def missing(caps, required):
    """Which `module` / `module:--flag` tokens this clone cannot satisfy."""
    gaps = []
    for token in required:
        mod, _, flag = token.partition(':')
        if not caps['modules'].get(mod, False):
            gaps.append(f"{mod} (module not present)")
            continue
        if flag and flag not in caps['flags'].get(mod, []):
            gaps.append(f"{mod} {flag} (flag not supported)")
    return gaps


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--require', nargs='+', metavar='TOKEN', default=None,
                    help="Assert these are available: `module` or `module:--flag`. "
                         "Exits 3 listing everything missing.")
    ap.add_argument('--quiet', '-q', action='store_true',
                    help='With --require, print nothing on success.')
    a = ap.parse_args(argv)

    caps = capabilities()
    if not a.require:
        json.dump(caps, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
        return 0

    gaps = missing(caps, a.require)
    if gaps:
        print(f"KiCadRoutingTools clone at {caps['root']} cannot satisfy "
              f"{len(gaps)} requirement(s):", file=sys.stderr)
        for g in gaps:
            print(f"  - {g}", file=sys.stderr)
        print("This is not the engine you pinned. Check the branch and the "
              "environment variable rather than the routing result.",
              file=sys.stderr)
        return 3
    if not a.quiet:
        print(f"OK: {len(a.require)} requirement(s) satisfied by {caps['root']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
