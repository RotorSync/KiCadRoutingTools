#!/usr/bin/env python3
"""The routing chain, one stage at a time, with only the stages this board needs.

Two things a document cannot do, and this can.

FIRST, it emits one stage. A skill file is read whole, so every reader of the
routing half pays for the BGA escape arithmetic, the impedance ladder and the
plane-repair doctrine even on a two-layer board with none of them.

SECOND, it computes the chain from the BOARD. The stage list is not a fixed
recipe with "skip if not applicable" notes -- the notes are what get misread. A
board with no fine-pitch parts is never told about fanout; a board with no
plane nets is never told about pours or plane repair. What you are shown is
what you run.

    python3 -X utf8 <this> --stage A1 --board b.kicad_pcb
    python3 -X utf8 <this> --plan --board b.kicad_pcb     # the chain, computed
    python3 -X utf8 <this> --list
    python3 -X utf8 <this> --dump-all
    python3 -X utf8 <this> --self-test

Tags: <stage_instructions> act on these; <subagent_prompt> copy verbatim into a
subagent; <error> you skipped evidence.

Exit: 0 emitted, 2 usage, 4 a guard refused.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
REFS = os.path.join(SKILL_DIR, 'references')


def err(text):
    return f'<error>\n{text}\n</error>'


def _load(path, what):
    if not path:
        return None, (f'{what} not provided.')
    if not os.path.isfile(path):
        return None, f'{what}: no such file: {path}'
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh), None
    except Exception as exc:                                # noqa: BLE001
        return None, f'{what}: unreadable ({type(exc).__name__}: {exc})'


# --------------------------------------------------------------------------
# board facts -- what decides which stages exist
# --------------------------------------------------------------------------

def board_facts(board):
    """(facts, error). Read from the board; never guessed, never asked for."""
    # repo root = .../<repo>/.claude/skills/<skill>/scripts -> up four
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(HERE))))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from kicad_parser import parse_kicad_pcb, detect_package_type
    except Exception as exc:                                # noqa: BLE001
        return None, f'cannot import the parser ({exc})'
    if not os.path.isfile(board):
        return None, f'no such board: {board}'
    try:
        pcb = parse_kicad_pcb(board)
    except Exception as exc:                                # noqa: BLE001
        return None, f'cannot parse {board}: {exc}'

    layers = list(getattr(pcb.board_info, 'copper_layers', []) or [])
    fine = []
    for ref, fp in sorted((pcb.footprints or {}).items()):
        try:
            if detect_package_type(fp) in ('QFN', 'QFP', 'BGA', 'PGA', 'LGA'):
                fine.append(ref)
        except Exception:
            pass
    names = [n.name for n in (pcb.nets or {}).values() if n.name]
    powerish = [n for n in names
                if n.upper().lstrip('/+').startswith(
                    ('GND', 'VCC', 'VDD', 'VSS', '3V3', '5V', '1V8', 'AGND'))]
    # A diff pair, conservatively: two net names differing only in a P/N or
    # +/- suffix. Names alone under-count (that is why a separate skill exists
    # to find them by pin function) -- so this only decides whether to SHOW the
    # stage, and the stage itself says to confirm.
    stems = {}
    for n in names:
        for a, b in (('_P', '_N'), ('+', '-'), ('_p', '_n')):
            if n.endswith(a):
                stems.setdefault(n[:-len(a)], set()).add('P')
            elif n.endswith(b):
                stems.setdefault(n[:-len(b)], set()).add('N')
    pairs = sorted(s for s, v in stems.items() if v == {'P', 'N'})
    return {
        'board': board,
        'layers': len(layers) or 2,
        'layer_names': layers,
        'nets': len(names),
        'fine_pitch': fine,
        'plane_candidates': sorted(set(powerish)),
        'diff_pair_stems': pairs,
        'has_copper': bool(pcb.segments or pcb.vias),
    }, None


def chain_for(f):
    """The stages this board actually needs, in order."""
    steps = [('A1', 'read the board and state what it is'),
             ('A2', 'layers and stackup')]
    if f['fine_pitch']:
        steps.append(('A3', 'which parts need a fanout, and would benefit'))
    steps.append(('A4', 'diff pairs, power nets, and the design rules'))
    if f['plane_candidates']:
        steps.append(('A5', 'which nets become planes, on which layers'))
    steps.append(('A6', 'the net-coverage partition (a gate for every R stage)'))
    if f['plane_candidates']:
        steps.append(('R1', 'pour the planes, bare, on the empty board'))
    if f['fine_pitch']:
        steps.append(('R2', 'fanout the fine-pitch parts'))
        steps.append(('R3', 'clear decap/fanout-via collisions'))
    if f['diff_pair_stems']:
        steps.append(('R4', 'differential pairs and impedance nets'))
    steps.append(('R5', 'the bulk signal route'))
    if f['plane_candidates']:
        steps.append(('R6', 'finalize the planes (stitching, return vias)'))
        steps.append(('R7', 'repair disconnected plane regions'))
        steps.append(('R8', 'reconnect what the repair ripped'))
    steps.append(('R9', 'verify: DRC, connectivity, orphan stubs, coverage'))
    steps.append(('V1', 'score it, and audit the score'))
    steps.append(('V2', 'classify the blocker and pick the cheapest lever'))
    steps.append(('V3', 'apply, accept or revert, and record'))
    steps.append(('V5', 'stop conditions and close-out'))
    return steps


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def _facts_or_err(a):
    f, e = board_facts(a.board)
    if e:
        return None, err(f'{e}\n\nEvery stage in this driver is computed from '
                         f'the board, so it cannot run without one. Pass '
                         f'--board <file>.')
    if f['has_copper'] and a.stage in ('R1', 'R2'):
        return None, err(
            'This board already carries copper, and this stage assumes an '
            'empty board. Re-run the chain from the unrouted board, or pick '
            'the stage you actually mean to re-enter at.')
    return f, None


def a1(a, f):
    return f'''<stage_instructions stage="A1" name="read the board">
State what this board IS before planning anything for it. Read, do not assume:

  python3 -X utf8 list_nets.py {a.board} --design-rules
  python3 -X utf8 check_drc.py {a.board} --clearance <the board's own floor>

Read from THIS board, measured just now:
  copper layers      {f['layers']}  ({', '.join(f['layer_names']) or 'unnamed'})
  nets               {f['nets']}
  fine-pitch parts   {len(f['fine_pitch'])}{': ' + ', '.join(f['fine_pitch'][:8]) if f['fine_pitch'] else ' -- no fanout stage in this chain'}
  plane candidates   {len(f['plane_candidates'])}{': ' + ', '.join(f['plane_candidates'][:8]) if f['plane_candidates'] else ' -- no pour or plane stages in this chain'}
  diff-pair stems    {len(f['diff_pair_stems'])}{': ' + ', '.join(f['diff_pair_stems'][:6]) if f['diff_pair_stems'] else ' -- no diff-pair stage in this chain'}
  carries copper     {f['has_copper']}

The clearance floor comes off the board (its .kicad_pro netclass, its
.kicad_dru rules), never from a round number. Grading at a floor the board was
not routed to manufactures violations that are not there.

Next: --stage A2 --board {a.board}   (or --plan for the whole computed chain)
</stage_instructions>'''


def a2(a, f):
    tail = ('' if f['layers'] > 2 else '''
This is a TWO-LAYER board. Everything about inner-layer plane costs and
4+-layer pour strategy is absent from this chain on purpose -- it does not
apply. What does: on a dense two-layer board the layer-cost balance is the
lever, and the default is often too steep. Rebalance toward 1.0/1.5, then
1.0/1.0, and compare via counts.''')
    return f'''<stage_instructions stage="A2" name="layers and stackup">
{f['layers']} copper layer(s): {', '.join(f['layer_names']) or 'unnamed'}.

  python3 -X utf8 -c "
from kicad_parser import parse_kicad_pcb
b = parse_kicad_pcb('{a.board}').board_info
print('stackup layers:', len(b.stackup))
for l in b.stackup[:12]: print(' ', l.name, l.layer_type, l.thickness, l.epsilon_r)"

An empty or default stackup means impedance targets cannot be computed and
must not be claimed. Say so rather than routing to a number nobody derived.{tail}

Next: --stage {'A3' if f['fine_pitch'] else 'A4'} --board {a.board}
</stage_instructions>'''


def a3(a, f):
    return f'''<stage_instructions stage="A3" name="fanout">
Fine-pitch parts on this board: {', '.join(f['fine_pitch'])}

Needing a fanout and BENEFITING from one are different questions. Ask all four
per part, and answer them from the board:
  1. Are there interior pads that no edge lane can reach? (they need a via,
     not a lane -- that is the actual fanout signal)
  2. Does the escape budget close? via + track + 2*clearance + margin must fit
     within the pad pitch.
  3. Do the parts' own escape faces have lanes left?
       python3 -X utf8 check_channels.py {a.board}
  4. Are the plane balls excluded, so they get plane-drop vias instead of
     escape routes?

A part that passes 1 and 2 with room to spare usually routes better WITHOUT a
fanout: an escape stub is committed copper that later steps must route around.

Next: --stage A4 --board {a.board}
</stage_instructions>'''


def a4(a, f):
    dp = (f'''Diff-pair stems found BY NAME: {', '.join(f['diff_pair_stems'][:10])}
Name matching under-counts -- pairs whose names do not follow P/N convention
are invisible to it. Confirm by pin function before routing them:
  /identify-diff-pairs   (a separate skill; it reads datasheets)'''
          if f['diff_pair_stems'] else
          'No P/N-suffixed net names on this board, so no diff-pair stage is in\n'
          'this chain. If the board HAS pairs under other names, find them now:\n'
          '  /identify-diff-pairs')
    return f'''<stage_instructions stage="A4" name="pairs, power, rules">
{dp}

The board's own rules outrank any default you would otherwise pass:
  python3 -X utf8 list_nets.py {a.board} --design-rules

And a requirements document, if one exists, outranks the board's rules. Read it
before choosing a width, a clearance, or an impedance -- a spec clause is the
one input none of these tools can derive.

Power nets: {', '.join(f['plane_candidates'][:10]) or '(none by name)'}
Width is a REQUEST, not a result. Whatever you set, measure the emitted copper
afterwards; the router does not complain when it cannot honour a width.

Next: --stage {'A5' if f['plane_candidates'] else 'A6'} --board {a.board}
</stage_instructions>'''


def a5(a, f):
    return f'''<stage_instructions stage="A5" name="plane mapping">
Plane candidates by name: {', '.join(f['plane_candidates'])}

Decide which become planes and on which layers. The signal-integrity reasoning
(GND adjacency for return paths, GND/VCC pairing for interplane capacitance,
split layers for multiple rails) has its own skill:
  /recommend-plane-mappings

The output you need here is a net -> layer map, because two later gates consume
it: the coverage partition (A6) and the layer costs the bulk route uses.

Next: --stage A6 --board {a.board}
</stage_instructions>'''


def a6(a, f):
    return f'''<stage_instructions stage="A6" name="coverage partition">
Every net is routed by exactly one stage. Write that partition down NOW, as
JSON, because every R stage below refuses to run without it:

  wk/coverage.json
  {{"planes": ["GND", "VCC"], "diff_pairs": ["USB_*"], "impedance": [],
    "signals": ["*"]}}

Then assert it, in code, before any command runs:

  python3 -X utf8 -c "
import json; c = json.load(open('wk/coverage.json'))
planes = set(c['planes']); excl = set(c.get('signals_exclude', planes))
assert planes == excl, f'partition leak: {{planes ^ excl}}'
print('partition OK')"

The failure this prevents: a net that belongs to the pour also appearing in the
bulk route's net list, so the router lays copper for a net the pour already
owns -- or the reverse, a net nobody routes and nobody notices until the
connectivity check at the end.

Next: --stage {'R1' if f['plane_candidates'] else 'R5'} --board {a.board} \\
          --coverage wk/coverage.json
</stage_instructions>'''


def _needs_coverage(a):
    cov, cerr = _load(a.coverage, 'The net-coverage partition (--coverage)')
    if cerr:
        return err(cerr + '\n\nA6 produces it. Every routing stage refuses '
                          'without it, because a net routed by two stages (or '
                          'by none) is the failure that is hardest to see '
                          'afterwards.')
    return None


def r1(a, f):
    return f'''<stage_instructions stage="R1" name="pour the planes FIRST">
Pour on the EMPTY board, before the fanout. Nets and layers only:

  python3 -X utf8 route_planes.py {a.board} board_R1.kicad_pcb \\
      --nets {' '.join(f['plane_candidates'][:4]) or 'GND'} --layers <from A5>

NO --add-gnd-vias, NO --stitch-vias, NO --rip-blocker-nets at this stage: each
adapts to signals that do not exist yet.

The ordering is mechanical, not stylistic. A fanout's escape stubs ARE signal
copper, and the pour step refuses (exit 3) to pour over a partially-routed
board carrying bare pads. Fanout first and this stage cannot run.

Next: --stage {'R2' if f['fine_pitch'] else 'R5'} --board board_R1.kicad_pcb \\
          --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r2(a, f):
    return f'''<stage_instructions stage="R2" name="fanout">
Escape the fine-pitch parts on the poured board: {', '.join(f['fine_pitch'])}

Exclude the plane nets -- the exclusion is exactly what marks their balls for
plane-drop vias, which the intact pour picks up at fill:

  python3 -X utf8 bga_fanout.py board_R1.kicad_pcb board_R2.kicad_pcb \\
      --nets "*" {' '.join('"!' + n + '"' for n in f['plane_candidates'][:3])}

GATE before moving on: failed == 0 and no drc_grazes class. A fanout that left
escapes unrouted has committed copper that every later stage must route around,
and retrying it later is more expensive than fixing it now.

Next: --stage R3 --board board_R2.kicad_pcb --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r3(a, f):
    return f'''<stage_instructions stage="R3" name="decap clearance">
Fanout vias and decoupling caps compete for the same space under a fine-pitch
part. Clear the collisions before signal routing, not after:

  python3 -X utf8 place_fanout_clearance.py board_R2.kicad_pcb board_R3.kicad_pcb

This moves PARTS, so everything it touches is a placement change: re-check the
placement gates if it moves anything load-bearing.

Next: --stage {'R4' if f['diff_pair_stems'] else 'R5'} --board board_R3.kicad_pcb \\
          --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r4(a, f):
    return f'''<stage_instructions stage="R4" name="pairs and impedance">
The most constrained routes claim their channels before anything else can block
them. Stems seen by name: {', '.join(f['diff_pair_stems'][:8])}

  python3 -X utf8 route_diff.py <in> board_R4.kicad_pcb \\
      --pairs <confirmed pairs> [--impedance <ohms>] --clearance <floor>

Both members of a pair route together or the pair is not routed. A "partial"
pair is a single-ended net with a misleading name.

After this stage those nets are EXCLUDED from every later net list -- they are
already routed, and re-routing them silently is how a matched pair becomes
unmatched.

Next: --stage R5 --board board_R4.kicad_pcb --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r5(a, f):
    lc = ('\n--layer-costs is MANDATORY here: a solid pour exists, and without\n'
          'the cost the router will cut through it wherever that is shortest.\n'
          'Derive it from the plane map (~3x on plane layers).'
          if f['plane_candidates'] else
          '\nNo planes on this board, so no layer costs to derive.')
    return f'''<stage_instructions stage="R5" name="the bulk signal route">
Everything the partition assigns to signals, excluding what earlier stages
already routed:

  python3 -X utf8 route.py <in> board_R5.kicad_pcb \\
      --nets "*" <exclusions from the partition> \\
      --clearance <floor> --track-width <from A4> \\
      [--layer-costs ...] 2>&1 | tee wk/route.log
{lc}

Do NOT pass --max-iterations: the router self-budgets. Rip-up depth 3-5;
deeper measured WORSE, because a ripped victim whose corridor is taken cannot
be restored.

Read the JSON summary, not the console tail: min_clearance_used tells you what
the router actually honoured, which is frequently not what you asked for.

Next: --stage {'R6' if f['plane_candidates'] else 'R9'} --board board_R5.kicad_pcb \\
          --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r6(a, f):
    return f'''<stage_instructions stage="R6" name="finalize the planes">
Now the signals exist, so stitching and return vias can adapt to them:

  python3 -X utf8 route_planes.py board_R5.kicad_pcb board_R6.kicad_pcb \\
      --nets {' '.join(f['plane_candidates'][:3])} --add-gnd-vias --stitch-vias

Prefer the no-rip form first. --rip-blocker-nets leaves nets unrouted for a
later stage to reconnect, and a rip whose restore fails ships an open net.

Next: --stage R7 --board board_R6.kicad_pcb --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r7(a, f):
    return f'''<stage_instructions stage="R7" name="repair plane regions">
  python3 -X utf8 route_disconnected_planes.py board_R6.kicad_pcb board_R7.kicad_pcb \\
      --nets {' '.join(f['plane_candidates'][:3])} --clearance <floor>

This step's reconnect runs at THIS step's parameters, so restate every per-net
constraint that matters (widths, layers, power-net widths) -- none of them
persists from an earlier command.

Read ripped_still_open in its JSON summary and its exit code: nets it ripped
and could neither reconnect nor restore ship OPEN, and the run exits nonzero
when they do.

Next: --stage R8 --board board_R7.kicad_pcb --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r8(a, f):
    return f'''<stage_instructions stage="R8" name="reconnect what was ripped">
Reconnect the casualties, mirroring every constrained pass in the SAME order
they ran originally, and sweeping the remainder last. A net reconnected under
default parameters has silently lost whatever width or layer constraint it
carried.

Next: --stage R9 --board board_R8.kicad_pcb --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def r9(a, f):
    return f'''<stage_instructions stage="R9" name="verify">
Four instruments, none of which replaces another. Do NOT pipe a gate -- a
truncated read is how a failing board reads as passing:

  python3 -X utf8 check_drc.py <board> --clearance <floor> --clearance-margin 0
  python3 -X utf8 check_connected.py <board>
  python3 -X utf8 check_orphan_stubs.py <board>
  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/board_score.py <board> --json

Connectivity is orthogonal to DRC: a DRC-clean board can be entirely
disconnected, because isolated copper has no clearance conflicts.

Also read the fab-floor line the DRC writeback prints. If it relaxed a track or
via minimum, this board grades clean against a rule it just rewrote -- confirm
the fab supports the new number before calling it done.

Next: --stage V1 --board <board> --coverage {a.coverage or 'wk/coverage.json'}
</stage_instructions>'''


def v1(a, f):
    return f'''<stage_instructions stage="V1" name="score and audit the score">
  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/board_score.py \\
      <board> --json wk/score.json <every clause-carrying flag this board has>

Then audit it before believing it:
  - a component reported `ungraded` was NOT examined. Report it as unexamined,
    never as clean.
  - `blocking == null` is not `blocking == 0`.
  - quality (vias, copper, segments) is a tie-break ONLY at blocking == 0.
    Comparing it earlier lets a router trade a disconnected net for a via.

Read {os.path.join(REFS, 'evidence-map.md')} before quoting any
number from any instrument: it says which key answers which question.

Next: --stage V2 --board <board> --score wk/score.json
</stage_instructions>'''


def v2(a, f):
    score, serr = _load(a.score, 'The board score (--score)')
    if serr:
        return err(serr + '\n\nV1 produces it. Choosing a lever without the '
                          'score is how a run spends its budget on the wrong '
                          'component.')
    blocking = score.get('blocking') if isinstance(score, dict) else None
    if blocking is None:
        return err('The score carries no `blocking` value, or it is null. That '
                   'is not zero -- it means something was not graded. Fix the '
                   'instrument first: a systemic iteration, not a routing one.')
    if blocking == 0:
        return f'''<stage_instructions stage="V2" name="classify" note="blocking is 0">
blocking == 0. There is no lever to choose: this board is deliverable on the
gates measured so far.

Confirm nothing is merely UNGRADED (V1's audit), then go to close-out.

Next: --stage V5 --board {a.board} --score {a.score}
</stage_instructions>'''
    return f'''<stage_instructions stage="V2" name="classify the blocker">
blocking = {blocking}. Classify BEFORE retrying; the classification decides
which of three very different levers applies.

Choose by the connectivity-first ladder, in this order -- NOT by which
blocking_by entry is largest. "The biggest entry names the lever" is wrong and
it wrecked a run:

  1. unrouted   nets with no copper at all
  2. broken     nets whose copper does not connect their pads
  3. widths     copper narrower than its clause requires
  4. floorplan  a clause no arrangement at this placement can satisfy
  5. drc        clearance violations

Then classify the SHAPE of the failure:

  parameter-shaped   grid, rip-up depth, layer costs, width -> re-enter at the
                     failing ROUTING stage with the parameter changed
  placement-shaped   a starved escape face, a part its net cannot reach ->
                     STOP. No router setting adds a lane. Go to
                     /plan-pcb-placement, and restart the chain from its output
  floorplan-shaped   the arrangement cannot satisfy the clause -> a different
                     arrangement, not another retry

Next: --stage V3 --board {a.board} --score {a.score}
</stage_instructions>'''


def v3(a, f):
    return f'''<stage_instructions stage="V3" name="apply, accept, record">
Apply ONE lever. Re-score with the same flags.

ACCEPT only if blocking strictly decreased, or it is level and quality
improved. Two exceptions, both real:
  - connectivity outranks everything: an iteration that connects a net and
    raises drc is progress;
  - blocking may RISE because more of the board became measurable. Compare
    what each score LOOKED AT before comparing the scores.

Record before the next iteration starts:
  python3 -X utf8 converge.py record --ledger wk/ledger.jsonl --board <board> \\
      --kind completion --lever "<what and why>" --score "$(cat wk/score.json)" \\
      --argv <the real command>

Failing nets go in by NAME. A count forces every later reader to re-derive the
set, and a truncated re-derivation shipped a wrong close-out.

Next: --stage V2 (another lever) or --stage V5 (close out)
</stage_instructions>'''


def v5(a, f):
    return f'''<stage_instructions stage="V5" name="close out">
Stop on one of these, and NAME which:
  1. blocking == 0 and every lens passes;
  2. the budget is spent;
  3. five consecutive iterations with no change;
  4. the remaining work is measured-unfixable at this stage, and said so.

"It looks done" and "the router says routed" are not stop conditions. A
router's own tally can come from a local proxy while pads stay disconnected.

Dispatch the independent lenses -- quote the file, do not paraphrase it:

<subagent_prompt agent="verifier" description="verify the routed board">
Read {os.path.join(REFS, 'verifier-prompts.md')} and apply its
routed-board lenses to <board>. Re-derive every number from the board itself;
do not trust the report. Answer with a line beginning VERDICT= and nothing
above it.
</subagent_prompt>

The report states, per component: the number, the instrument that produced it,
and for anything unresolved WHY it is unfixable here.
</stage_instructions>'''


STAGES = {'A1': a1, 'A2': a2, 'A3': a3, 'A4': a4, 'A5': a5, 'A6': a6,
          'R1': r1, 'R2': r2, 'R3': r3, 'R4': r4, 'R5': r5, 'R6': r6,
          'R7': r7, 'R8': r8, 'R9': r9,
          'V1': v1, 'V2': v2, 'V3': v3, 'V5': v5}
COVERAGE_GATED = {'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9'}


def _args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stage', choices=sorted(STAGES))
    ap.add_argument('--board', default='board.kicad_pcb')
    ap.add_argument('--coverage', default=None)
    ap.add_argument('--score', default=None)
    ap.add_argument('--plan', action='store_true',
                    help='the chain this board needs, computed from the board')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--dump-all', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    a = _args(argv)
    if a.list:
        for k in ('A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'R1', 'R2', 'R3', 'R4',
                  'R5', 'R6', 'R7', 'R8', 'R9', 'V1', 'V2', 'V3', 'V5'):
            print(f'  {k}')
        return 0
    if a.self_test:
        return _self_test()
    if a.dump_all:
        fake = {'board': 'b.kicad_pcb', 'layers': 4,
                'layer_names': ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'],
                'nets': 120, 'fine_pitch': ['U1'],
                'plane_candidates': ['GND', '+3V3'],
                'diff_pair_stems': ['USB'], 'has_copper': False}
        loose = _args(['--board', 'b.kicad_pcb', '--coverage', 'c.json',
                       '--score', 's.json'])
        for k in sorted(STAGES):
            print(f'===== {k} =====')
            loose.stage = k
            print(STAGES[k](loose, fake))
        return 0
    if a.plan:
        f, e = board_facts(a.board)
        if e:
            print(err(e))
            return 4
        print(f'<stage_instructions stage="plan" name="the chain this board '
              f'needs">')
        print(f'Computed from {a.board}: {f["layers"]} layer(s), '
              f'{len(f["fine_pitch"])} fine-pitch part(s), '
              f'{len(f["plane_candidates"])} plane candidate(s), '
              f'{len(f["diff_pair_stems"])} diff-pair stem(s).\n')
        for key, title in chain_for(f):
            print(f'  {key:3s}  {title}')
        skipped = sorted(set(STAGES) - {k for k, _ in chain_for(f)})
        if skipped:
            print(f'\nNot in this chain (this board has no such work): '
                  f'{", ".join(skipped)}')
        print('\nRun them in order, one at a time:\n'
              f'  python3 -X utf8 {sys.argv[0]} --stage A1 --board {a.board}')
        print('</stage_instructions>')
        return 0
    if not a.stage:
        print('routing_driver: --stage is required (see --list, --plan)',
              file=sys.stderr)
        return 2
    f, e = _facts_or_err(a)
    if e:
        print(e)
        return 4
    if a.stage in COVERAGE_GATED:
        refusal = _needs_coverage(a)
        if refusal:
            print(refusal)
            return 4
    out = STAGES[a.stage](a, f)
    print(out)
    return 4 if out.startswith('<error>') else 0


def _self_test():
    bad = []

    def want(cond, label):
        print(f'  {"PASS" if cond else "FAIL"}  {label}')
        if not cond:
            bad.append(label)

    two_layer = {'board': 'b.kicad_pcb', 'layers': 2,
                 'layer_names': ['F.Cu', 'B.Cu'], 'nets': 40,
                 'fine_pitch': [], 'plane_candidates': [],
                 'diff_pair_stems': [], 'has_copper': False}
    dense = {'board': 'b.kicad_pcb', 'layers': 4,
             'layer_names': ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'], 'nets': 300,
             'fine_pitch': ['U1'], 'plane_candidates': ['GND', '+3V3'],
             'diff_pair_stems': ['USB'], 'has_copper': False}

    simple = {k for k, _ in chain_for(two_layer)}
    full = {k for k, _ in chain_for(dense)}
    want('R2' not in simple, 'a board with no fine-pitch part gets no fanout stage')
    want('R1' not in simple, 'a board with no plane nets gets no pour stage')
    want('R4' not in simple, 'a board with no pairs gets no diff-pair stage')
    want('R5' in simple and 'R9' in simple, 'every board still routes and verifies')
    want(full > simple, 'a dense board gets strictly more stages')
    want({'R1', 'R2', 'R4', 'R7'} <= full, 'a dense board gets all of them')

    a = _args(['--board', 'b.kicad_pcb', '--coverage', 'c.json',
               '--score', 's.json'])
    for key, fn in sorted(STAGES.items()):
        a.stage = key
        out = fn(a, dense)
        want(out.startswith(('<stage_instructions', '<error>')),
             f'{key} emits a tagged block')
        want(len(out.splitlines()) <= 60, f'{key} stays under 60 lines')

    a2_ = _args(['--board', 'b.kicad_pcb'])
    want('TWO-LAYER' in STAGES['A2'](a2_, two_layer),
         'a two-layer board is told the two-layer lever')
    want('TWO-LAYER' not in STAGES['A2'](a2_, dense),
         '...and a 4-layer board is not')
    want('MANDATORY' in STAGES['R5'](_args(['--board', 'b', '--coverage', 'c']), dense),
         'layer costs are mandatory when a pour exists')
    want('No planes' in STAGES['R5'](_args(['--board', 'b', '--coverage', 'c']),
                                     two_layer),
         '...and explicitly absent when none does')

    want(_needs_coverage(_args(['--board', 'b'])) is not None,
         'an R stage refuses without the coverage partition')

    everything = '\n'.join(fn(a, dense) for fn in STAGES.values())
    for phrase in ('you may want to', 'if you are not sure', 'perhaps'):
        want(phrase not in everything.lower(), f'no hedging: {phrase!r}')
    # The forbidden flag may be NAMED (to forbid it) but never offered. The
    # distinction is whether it appears inside a command line.
    offered = [ln for ln in everything.splitlines()
               if '--max-iterations' in ln
               and ('python3' in ln or ln.lstrip().startswith(('--', 'route')))]
    want(not offered, 'the forbidden flag is named to forbid it, never offered')
    want(everything.count('<subagent_prompt') >= 1,
         'the close-out dispatches an independent verifier')

    print('OK' if not bad else f'FAIL: {len(bad)}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
