#!/usr/bin/env python3
"""A non-ASCII glyph in a SHARED ENGINE print must not abort the run.

Windows consoles default to cp1252, which cannot encode the Ohm sign. The CLI
entry points already guard with console_encoding.enable_utf8_console(), but that
runs in `if __name__ == "__main__"` -- so it does nothing for the OTHER caller
shape, which is the whole point of this file:

    create_plane(...)          # shared engine, called IN-PROCESS

A test, a script, or an embedder that imports the engine directly never executes
route_planes.py's __main__, so stdout stays the interpreter's default. On a
cp1252 console that made plane_resistance's one "mOhm" line raise
UnicodeEncodeError and take the entire pour down with it -- the CLI protected,
the same engine function called directly not. That is the CLAUDE.md CLI/GUI
parity shape exactly: a fix in main() is not a fix in the engine.

The GUI escaped it only because gui_utils.StdoutRedirector has its own
try/except (issue #152). This pins the engine-side fix.

    python3 tests/test_console_encoding_ascii_safe.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))

from console_encoding import ascii_safe               # noqa: E402
from plane_resistance import print_single_net_resistance   # noqa: E402


def _cp1252_stream():
    """A stream that behaves like a Windows console: strict, cp1252."""
    return io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict')


def test_utf8_stream_keeps_the_real_glyph():
    """The normal case must not be degraded into ASCII."""
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
    assert ascii_safe("1.103 mΩ", utf8) == "1.103 mΩ"
    print("  PASS: UTF-8 stream keeps the real symbol")


def test_cp1252_stream_gets_a_readable_spelling():
    """Not '?' -- a bare replace loses the unit, which is worse than ASCII."""
    got = ascii_safe("1.103 mΩ", _cp1252_stream())
    assert got == "1.103 mohm", got
    assert '?' not in got, "fell back to lossy replacement instead of a spelling"
    print(f"  PASS: cp1252 stream -> {got!r}")


def test_both_omega_codepoints_are_covered():
    """U+03A9 (GREEK CAPITAL OMEGA) and U+2126 (OHM SIGN) both appear in the
    tree; a map keyed on only one of them would still crash on the other."""
    for cp in ('Ω', 'Ω'):
        got = ascii_safe(f"1.0 m{cp}", _cp1252_stream())
        assert got == "1.0 mohm", (cp, got)
    print("  PASS: both omega codepoints spell out")


def test_engine_print_survives_a_cp1252_console():
    """The actual regression: this print used to abort the pour."""
    result = {'path_length': 180.74, 'avg_width': 78.6, 'resistance': 0.001103,
              'max_current': 56.62, 'max_current_ipc2152': 56.62,
              'in_chart_range': True, 'is_internal': False,
              'copper_oz': 1.0, 'temp_rise': 10.0}
    buf = _cp1252_stream()
    old = sys.stdout
    sys.stdout = buf
    try:
        print_single_net_resistance(result, 'GND')
    except UnicodeEncodeError as e:      # pragma: no cover - the bug
        sys.stdout = old
        raise AssertionError(
            f"print_single_net_resistance still crashes on a cp1252 console: {e}")
    finally:
        sys.stdout = old
    buf.flush()
    text = buf.buffer.getvalue().decode('cp1252')
    line = [ln for ln in text.splitlines() if 'Resistance:' in ln]
    assert line, f"no resistance line emitted:\n{text}"
    assert 'mohm' in line[0], line[0]
    print(f"  PASS: engine print survived ->{line[0]}")


def test_ascii_safe_is_defensive_about_odd_streams():
    """It is called from library code; it must never be the thing that raises."""
    class NoEncoding:
        encoding = None
    class Weird:
        encoding = 'not-a-real-codec'
    assert ascii_safe("mΩ", NoEncoding()) == "mΩ"
    assert ascii_safe("mΩ", Weird()) == "mΩ"
    assert ascii_safe("", _cp1252_stream()) == ""
    assert ascii_safe(None, _cp1252_stream()) is None
    print("  PASS: degrades quietly on streams it cannot interrogate")


TESTS = [
    test_utf8_stream_keeps_the_real_glyph,
    test_cp1252_stream_gets_a_readable_spelling,
    test_both_omega_codepoints_are_covered,
    test_engine_print_survives_a_cp1252_console,
    test_ascii_safe_is_defensive_about_odd_streams,
]


def main():
    fails = []
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            fails.append(f"{t.__name__}: {e}")
    print("=" * 60)
    if fails:
        print("FAIL:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"{len(TESTS)}/{len(TESTS)} checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
