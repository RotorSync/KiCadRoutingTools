"""Removed CLI flags, and the argv filter that keeps old manifests replaying.

Single source of truth for flags the pours-first architecture (#562) deleted.
Two consumers must agree on this list or recorded chains break:

  * the CLI ``main()`` of the script that dropped the flag, which strips it
    from ``sys.argv`` (with a printed note) instead of dying in argparse; and
  * the manifest migration, which rewrites the ~457 recorded
    ``redo_commands.sh`` chains to the pours-first step order.

The strip is a MIGRATION SHIM, not a supported interface: the flags are gone
from ``--help`` and from the engine signatures, so passing one changes
nothing. Delete this module once the manifests have been rewritten.

Why the shim exists at all: the flags fed the plane step's tap/rip machinery,
which no longer runs (``create_plane`` defers every via-needed pad to the
route step's pour-launch and in-run finalize). Every recorded chain from
before the switch still passes them, so a hard argparse failure would strand
the whole replay corpus between this commit and the migration.
"""
from typing import Dict, List, Sequence, Set

# flag -> whether it consumed a following value. Store-true flags are False.
_REMOVED: Dict[str, Dict[str, bool]] = {
    # route_planes.py: the pour step does no tapping and no ripping (#562),
    # so via-search/reuse radii and the blocker rip-up knobs feed nothing.
    'route_planes.py': {
        '--max-search-radius': True,
        '--max-via-reuse-radius': True,
        '--close-via-radius': True,
        '--rip-blocker-nets': False,
        '--max-rip-nets': True,
        '--reroute-ripped-nets': False,
    },
}


def removed_flags(script: str) -> Dict[str, bool]:
    """Flags removed from ``script``, mapped to "takes a value"."""
    return dict(_REMOVED.get(script, {}))


def all_removed() -> Set[str]:
    """Every removed flag across all scripts (for the migration script)."""
    out: Set[str] = set()
    for spec in _REMOVED.values():
        out.update(spec)
    return out


def strip_removed(argv: Sequence[str], script: str,
                  announce: bool = True) -> List[str]:
    """Drop ``script``'s removed flags from ``argv``, returning a new list.

    Handles both ``--flag value`` and ``--flag=value`` spellings. Unknown
    flags are left alone so argparse still reports genuine typos.
    """
    spec = _REMOVED.get(script)
    if not spec:
        return list(argv)
    out: List[str] = []
    dropped: List[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        name = tok.split('=', 1)[0]
        if name in spec:
            dropped.append(name)
            # `--flag=value` carries its value inline; `--flag value` does not.
            if spec[name] and '=' not in tok and i + 1 < len(argv):
                i += 1
            i += 1
            continue
        out.append(tok)
        i += 1
    if dropped and announce:
        print(f"Note: ignoring removed flag(s) {', '.join(sorted(set(dropped)))}"
              f" -- the plane step does no tapping or ripping since #562; "
              f"the route step welds plane pads. Drop them from your command.")
    return out
