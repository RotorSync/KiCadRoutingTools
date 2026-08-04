"""KICAD_DUP_TRAP=1: name the code path that puts the SAME copper object into
pcb_data twice.

The BOARD/FILE ledgers detect duplicate object entries but only AFTER the run,
so they say "net 38 has a duplicate" and nothing about who appended it. This
wraps pcb_data.segments / pcb_data.vias in a list subclass that prints the
offending stack the moment a re-append happens, turning a whole re-run into a
single pinpointed call site.

Why a duplicate object entry is always a bug: the write model holds the object
once, so the ledger reports phantom board-only copper; obstacles get
double-stamped (#208/#309); and gates treating the list as a node set are
defeated (#195).

NO FALSE POSITIVES FROM REMOVALS: code removes copper with slice assignment
(``pcb_data.segments[:] = [...]``) and ``.remove()``, which an id-set cache
would not see -- a later legitimate restore would then look like a duplicate.
So the id set is only a cheap PRE-FILTER; on a hit we confirm by scanning the
real list for an identical object. The O(n) scan runs only on suspicion.

Enable for one run:

    KICAD_DUP_TRAP=1 python3 route.py board.kicad_pcb out.kicad_pcb --nets '*'
"""
import os
import sys
import traceback


class DupTrapList(list):
    """A list that reports (does not block) re-appends of the same object."""

    def __init__(self, iterable=(), label='list', limit=12):
        super().__init__(iterable)
        self._label = label
        self._ids = {id(o) for o in self}
        self._limit = limit
        self._hits = 0

    def _is_really_present(self, obj):
        # Confirm against the LIVE list: the id cache never learns about
        # removals, so it over-reports on its own.
        for existing in self:
            if existing is obj:
                return True
        return False

    def _check(self, obj):
        if id(obj) in self._ids and self._is_really_present(obj):
            self._hits += 1
            if self._hits <= self._limit:
                what = getattr(obj, 'layer', None)
                where = (f"({getattr(obj, 'start_x', getattr(obj, 'x', '?'))},"
                         f"{getattr(obj, 'start_y', getattr(obj, 'y', '?'))})")
                print(f"\n[DUP_TRAP] {self._label}: re-append of the SAME "
                      f"object -- net {getattr(obj, 'net_id', '?')} {where} "
                      f"{what}", file=sys.stderr)
                traceback.print_stack(file=sys.stderr)
                sys.stderr.flush()
            elif self._hits == self._limit + 1:
                print(f"[DUP_TRAP] {self._label}: further hits suppressed",
                      file=sys.stderr)
        self._ids.add(id(obj))

    def append(self, obj):
        self._check(obj)
        super().append(obj)

    def extend(self, items):
        items = list(items)
        for o in items:
            self._check(o)
        super().extend(items)

    def insert(self, i, obj):
        self._check(obj)
        super().insert(i, obj)


def install(pcb_data, label=''):
    """Wrap pcb_data.segments/vias in traps. No-op unless KICAD_DUP_TRAP=1.

    Slice assignment (``segments[:] = [...]``) preserves the object, so the
    trap survives the removal passes that use it.
    """
    if os.environ.get('KICAD_DUP_TRAP', '') != '1':
        return pcb_data
    try:
        if not isinstance(pcb_data.segments, DupTrapList):
            pcb_data.segments = DupTrapList(pcb_data.segments,
                                            f"{label}segments")
        if not isinstance(pcb_data.vias, DupTrapList):
            pcb_data.vias = DupTrapList(pcb_data.vias, f"{label}vias")
        print(f"[DUP_TRAP] armed on {label or 'pcb_data'} "
              f"({len(pcb_data.segments)} segs, {len(pcb_data.vias)} vias)")
    except Exception as e:      # a debug aid must never break a run
        print(f"[DUP_TRAP] could not arm: {e}")
    return pcb_data
