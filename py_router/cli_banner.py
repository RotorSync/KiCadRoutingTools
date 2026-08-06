"""Self-describing CMD/EXIT banner for instrument CLIs (run-3 finding B1).

Run 3's conformance watcher filed 8 traceability FAILs for one root cause:
instruments leave no record of HOW they were invoked or WHAT they exited,
so every gate had to be hand-wrapped in ``{ echo "CMD: ..."; tool; echo
"EXIT=$?"; }`` — and the hand wrapping was applied inconsistently (one
render log carried it, the next did not). The evidence chain should not
depend on operator discipline: the instrument prints its own banner.

Usage — two lines at the top of the ``__main__`` block, no re-indentation::

    if __name__ == "__main__":
        import cli_banner; cli_banner.install()
        ... existing body, whether inline or sys.exit(main()) ...

``install()``:

- prints ``CMD: <the exact command line>`` — from ``sys.orig_argv`` when
  available (Python 3.10+), which includes interpreter flags like
  ``-X utf8`` verbatim, so the line is REPLAYABLE truth rather than a
  reconstruction;
- arranges for ``EXIT=<rc>`` to be printed as the process ends, whatever
  the path out: a ``sys.exit(n)`` (argparse errors included), falling off
  the end of the script (rc 0), or an unhandled exception (rc 1, the
  traceback still prints).

Both lines go to stdout so a single ``> log 2>&1`` redirect captures the
whole evidence unit. Set ``KRT_NO_BANNER=1`` to suppress (for consumers
that parse an instrument's stdout strictly).
"""
from __future__ import annotations

import atexit
import os
import sys

_installed = False


def _quote(a: str) -> str:
    if not a or any(c in a for c in ' \t"\'!*?$&|;<>(){}'):
        return '"' + a.replace('"', '\\"') + '"'
    return a


def command_line() -> str:
    """The exact invocation, interpreter flags included when knowable."""
    argv = getattr(sys, 'orig_argv', None)
    if argv:
        head = [os.path.basename(argv[0])] + list(argv[1:])
    else:  # pre-3.10 fallback: reconstruct (loses -X flags)
        head = [os.path.basename(sys.executable)] + list(sys.argv)
    return ' '.join(_quote(a) for a in head)


def install() -> None:
    """Print the CMD banner now; print EXIT=<rc> when the process ends."""
    global _installed
    if _installed or os.environ.get('KRT_NO_BANNER'):
        return
    _installed = True
    print(f"CMD: {command_line()}", flush=True)

    state = {'rc': 0}

    real_exit = sys.exit

    def _exit(code=0):
        state['rc'] = code if isinstance(code, int) else (0 if code is None else 1)
        real_exit(code)

    sys.exit = _exit

    real_hook = sys.excepthook

    def _hook(et, ev, tb):
        if et is SystemExit:  # raised directly, not via sys.exit()
            c = ev.code
            state['rc'] = c if isinstance(c, int) else (0 if c is None else 1)
        else:
            state['rc'] = 1
        real_hook(et, ev, tb)

    sys.excepthook = _hook

    @atexit.register
    def _print_exit():  # pragma: no cover - exercised via subprocess tests
        try:
            print(f"EXIT={state['rc']}", flush=True)
        except Exception:
            pass
