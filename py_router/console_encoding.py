"""UTF-8 console safety (issue #152, extended).

Windows consoles default to cp1252, which cannot encode the non-ASCII glyphs some
log lines use -- arrows in bus order, the Ohm sign in impedance, the warning sign
in the fab-floor escalation message, and more. A bare ``print()`` of such a glyph
raises ``UnicodeEncodeError`` and crashes the whole run.

Reconfiguring stdout/stderr to UTF-8 with ``errors="replace"`` makes a ``print``
never crash: the glyph is emitted as UTF-8 where the console supports it, and
replaced (never raised) where it does not.

Call :func:`enable_utf8_console` once at the top of every CLI entry point (each
``if __name__ == "__main__"`` block). It is idempotent and defensive -- a no-op on
older Pythons without ``reconfigure`` or on a stream that is not a real text
stream (e.g. an already-wrapped capture buffer under a test harness).
"""
import sys


def enable_utf8_console():
    """Reconfigure stdout/stderr to UTF-8 (errors='replace') so a non-ASCII
    ``print`` can never crash on a cp1252 console. Idempotent; swallows any
    failure so it is always safe to call unconditionally at startup."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# Readable ASCII stand-ins, used only when the stream genuinely cannot encode
# the glyph. 'replace' alone would render "3.500 m?" -- the unit silently lost,
# which is worse than a plain-ASCII spelling.
_ASCII_FALLBACKS = {
    "Ω": "ohm",   # GREEK CAPITAL OMEGA (impedance / resistance)
    "Ω": "ohm",   # OHM SIGN
    "µ": "u",     # MICRO SIGN
    "→": "->",
    "±": "+/-",
    "⚠": "!",
    "²": "^2",
    "×": "x",
}


def ascii_safe(text, stream=None):
    """``text`` with any glyph the target stream cannot encode spelled out.

    For CLI entry points :func:`enable_utf8_console` is the better fix -- it
    keeps the real glyph. This is for the OTHER caller shape: shared engine
    functions invoked IN-PROCESS (a test, a script, an embedder), where no
    ``__main__`` block ran and stdout is still the interpreter's default. On a
    cp1252 Windows console that made a library ``print`` of "mΩ" abort the whole
    run -- the CLI was protected, the same engine function called directly was
    not (the CLAUDE.md CLI/GUI parity shape: a fix in ``main()`` is not a fix in
    the engine).

    Returns ``text`` unchanged whenever the stream can encode it, so UTF-8
    consoles -- the normal case -- keep the real symbols.
    """
    if not text:
        return text
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None)
    if not enc:
        return text
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        pass
    except (LookupError, TypeError):
        return text
    out = "".join(_ASCII_FALLBACKS.get(ch, ch) for ch in text)
    try:
        out.encode(enc)
        return out
    except UnicodeEncodeError:
        return out.encode(enc, errors="replace").decode(enc, errors="replace")
