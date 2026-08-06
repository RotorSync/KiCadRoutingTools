#!/usr/bin/env python3
"""The loop BETWEEN placement and routing, one stage at a time.

The two halves each have a driver, and each keeps its own rules in front of the
executor one stage at a time. Their accept rules genuinely conflict -- placement
accepts a lap when the named finding it aimed at is gone, routing accepts an
iteration when `blocking` strictly decreased -- so an executor holding both
skills at once has two contradictory definitions of "better" live in the same
context. That is the confusion this driver removes: it never emits both.

What it adds beyond sequencing is the part nothing else owns:

  * the hand-off REFUSES without a placement close-out, so routing cannot start
    on a board whose placement was never proved;
  * a retry REFUSES without a stated classification, because the three failure
    shapes re-enter at three different points and cost is asymmetric -- a wrong
    parameter guess wastes iterations, a wrong placement guess wastes the whole
    routed board;
  * a placement-shaped re-entry MARKS every routed board stale, by name, so the
    next pass cannot quietly reuse one.

State crosses the boundary on DISK (the converge ledger), never in a head.
Delegation is a choice about context volume, not about correctness -- see
--delegate.

    python3 -X utf8 <this> --stage L1 --board b.kicad_pcb
    python3 -X utf8 <this> --list
    python3 -X utf8 <this> --dump-all
    python3 -X utf8 <this> --self-test

Tags: <stage_instructions> act on these; <subagent_prompt> copy verbatim into a
teammate; <error> you skipped evidence.

Exit: 0 emitted, 2 usage, 4 a guard refused.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

SHAPES = ('parameter', 'placement', 'floorplan')


def err(text):
    return f'<error>\n{text}\n</error>'


def _load(path, what):
    if not path:
        return None, f'{what} not provided.'
    if not os.path.isfile(path):
        return None, f'{what}: no such file: {path}'
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh), None
    except Exception as exc:                                # noqa: BLE001
        return None, f'{what}: unreadable ({type(exc).__name__}: {exc})'


def _ledger_rows(path):
    if not path or not os.path.isfile(path):
        return []
    out = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def l1(a):
    """Place. Delegated or inline -- correctness is the same either way."""
    if a.delegate:
        return f'''<stage_instructions stage="L1" name="place" of="5">
Delegate the placement half to a TEAMMATE, not a plain subagent: the placement
skill spawns its own verification subagents at its close-out, and a subagent
cannot spawn one. Give it the prompt below verbatim.

<subagent_prompt agent="claude" description="place {os.path.basename(a.board)}">
Drive the placement half of this board to its close-out, and do not route.

  board:  {a.board}
  ledger: {a.ledger}

Use /plan-pcb-placement. Ask its driver for one stage at a time:
  python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/placement_driver.py \\
      --stage P0 --board {a.board}
and follow the stage it prints, including its refusals -- an <error> means a
gate is holding, so produce what it asks for rather than working around it.

Record every accepted lap with converge.py into the ledger above.

Return, and return ONLY:
  1. the path of the placed board;
  2. the four close-out measurements with their numbers;
  3. what remains unfixed, each with the measurement that says it is unfixable
     at this stage;
  4. the refs you locked and why.
Do not summarise the process. The next stage needs the board and the numbers.
</subagent_prompt>

When it returns, continue here with --stage L2 and the board it produced.

Next: python3 -X utf8 {sys.argv[0]} --stage L2 --board <placed board> \\
          --ledger {a.ledger} --placement-report <its close-out json>
</stage_instructions>'''
    return f'''<stage_instructions stage="L1" name="place" of="5">
Place this board yourself, driven. Do not read the placement skill end to end:
ask its driver for one stage at a time, so only one loop's rules are ever in
front of you.

  python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/placement_driver.py \\
      --stage P0 --board {a.board}

Follow it to P-close, including its refusals. Record every accepted lap into
{a.ledger} with converge.py.

Delegate this stage instead (--delegate) when the placement run's own output
would crowd out the routing half -- a few hundred parts, long repair sweeps,
many renders. That is a context decision, not a correctness one: the guards
below are identical either way.

Next: python3 -X utf8 {sys.argv[0]} --stage L2 --board <placed board> \\
          --ledger {a.ledger} --placement-report <its close-out json>
</stage_instructions>'''


def l2(a):
    """Freeze what placement decided, then route."""
    rep, e = _load(a.placement_report, 'The placement close-out (--placement-report)')
    if e:
        return err(
            e + '\n\nRouting may not start on a board whose placement was '
                'never proved. L1 ends with a close-out carrying the four '
                'measurements; produce it, then come back.\n\nIf the board '
                'arrived already placed by someone else, run the placement '
                'gate alone and use ITS output:\n'
                '  python3 -X utf8 check_drc.py <board> --clearance <floor> '
                '--json wk/drc0.json\n'
                '  python3 -X utf8 check_assembly.py <board> --json '
                'wk/assembly0.json')
    blocking = rep.get('blocking')
    if isinstance(blocking, int) and blocking > 0 and not a.accept_residue:
        return err(
            f'The placement close-out reports blocking = {blocking}. A board '
            f'that reaches routing with a blocking assembly pair will fail '
            f'routing for a reason routing cannot fix, and the retries spent '
            f'there are wasted.\n\nGo back to the placement half, or re-run '
            f'this stage with --accept-residue if that residue is measured '
            f'unfixable and NAMED in the close-out -- which is a decision you '
            f'are recording, not a flag that makes it go away.')
    return f'''<stage_instructions stage="L2" name="freeze, then route" of="5">
FREEZE first. Lock the refs whose poses are decisions -- mechanically fixed
parts, anything a spec pins, anything the placement half moved deliberately. A
later step that moves them silently undoes the placement work, and nothing
downstream will report it.

Then route, driven, so the routing loop's rules are the only ones in front of
you:

  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/routing_driver.py \\
      --plan --board {a.board}
  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/routing_driver.py \\
      --stage A1 --board {a.board}

Its Step 0 gate will pass: you just did that work, and the close-out is the
evidence.

Two rules that are only true HERE, where the halves meet:
  - copper is not evidence about placement. A route that completed does not
    ratify the placement it ran on, and one that failed does not condemn it.
  - every routed board produced from THIS placement is valid only while this
    placement stands.

Next, on success: --stage L5. On a failure: --stage L3 --score <score json>
</stage_instructions>'''


def l3(a):
    """Classify. The whole loop turns on this."""
    score, e = _load(a.score, 'The routing score (--score)')
    if e:
        return err(e + '\n\nA retry without a classification is a guess, and '
                       'the three shapes re-enter at three different points. '
                       'Produce the score first:\n  python3 -X utf8 '
                       '.claude/skills/plan-pcb-routing/scripts/board_score.py '
                       '<board> --json wk/score.json')
    blocking = score.get('blocking')
    if blocking == 0:
        return f'''<stage_instructions stage="L3" name="classify" note="nothing to classify">
blocking == 0. There is no failure to classify.

Confirm nothing is merely UNGRADED (a component nothing examined is reported
unexamined, never clean), then close out.

Next: --stage L5 --board {a.board} --score {a.score}
</stage_instructions>'''
    return f'''<stage_instructions stage="L3" name="classify the failure" of="5">
blocking = {blocking}. Name the SHAPE before choosing anything.

All three shapes look identical from inside the router -- nets that did not
route. What separates them is a measurement, and the cost of guessing is
asymmetric.

  parameter-shaped   grid, rip-up depth, layer costs, width. The router had a
                     path and did not take it.
                     Evidence: the failing nets have lane supply at their
                     escape faces, and the diagnosis names a knob.

  placement-shaped   a starved escape face, a part its net cannot reach. No
                     router setting adds a lane.
                     Evidence: check_channels shows the face carrying demand
                     with zero supply; check_reachability says CAGED.

  floorplan-shaped   a clause no arrangement at this placement satisfies.
                     Evidence: the constraint survives every parameter and
                     every local move.

Measure it, do not infer it from how the failure feels:

  python3 -X utf8 check_channels.py {a.board} --baseline <the pre-route board>
  python3 -X utf8 check_reachability.py {a.board} --nets <the failing nets>

Then re-run this stage with the shape you measured:
  --stage L4 --shape <parameter|placement|floorplan> --board {a.board} \\
      --score {a.score} --ledger {a.ledger}
</stage_instructions>'''


def l4(a):
    """Re-enter, at the point the classification names."""
    if a.shape not in SHAPES:
        return err(
            'A re-entry needs a measured shape: --shape parameter | placement '
            '| floorplan.\n\nThis is the guard that matters most in this '
            'driver. Retrying a placement-shaped failure with router '
            'parameters spends iterations on a board no parameter can fix; '
            'sending a parameter-shaped failure back to placement throws away '
            'a routed board for nothing. L3 tells you how to measure it.')
    if a.shape == 'parameter':
        return f'''<stage_instructions stage="L4" name="re-enter: parameter" of="5">
Re-enter the FAILING ROUTING STEP with the parameter changed. Nothing before it
is invalidated, and the routed board stands.

  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/routing_driver.py \\
      --stage <the step that failed> --board {a.board}

Change ONE parameter. An iteration that changes three cannot tell you which one
worked, and the ledger entry it writes is unusable as evidence for the next.

Record it, then go back to L3 with the new score. If two parameter iterations
in a row do not move `blocking`, the shape was probably not parameter --
re-measure rather than trying a third.

Next: --stage L3 --board {a.board} --score <new score>
</stage_instructions>'''
    if a.shape == 'floorplan':
        return f'''<stage_instructions stage="L4" name="re-enter: floorplan" of="5">
No arrangement at this placement satisfies the clause, so neither a router
parameter nor a local repair will reach it. Go for a different ARRANGEMENT:

  python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/placement_driver.py \\
      --stage P5 --board <the pre-route placed board>

State the clause as intent first, so the slate can be graded against it rather
than eyeballed.

Every routed board from the old placement is stale. Say so in the ledger before
you start, and start the chain again at L1.

Next: --stage L1 --board <the adopted arrangement> --ledger {a.ledger}
</stage_instructions>'''
    # placement-shaped: the expensive one
    rows = _ledger_rows(a.ledger)
    routed = [r for r in rows if (r.get('kind') or '') in ('completion', 'routing')]
    stale = ('\n'.join(f'    - iteration {r.get("iteration")}: '
                       f'{(r.get("result_sha") or "")[:12]}' for r in routed[-8:])
             or '    (none recorded in this ledger)')
    return f'''<stage_instructions stage="L4" name="re-enter: placement" of="5">
This is the expensive one, and the cost is the point: no router setting adds a
lane, so every routed board produced from this placement is now stale.

Routed boards this ledger recorded, which must not be reused:
{stale}

1. Record the classification in the ledger BEFORE moving, with the measurement
   that produced it -- otherwise the next pass cannot tell why the placement
   changed and will re-derive it wrongly.
2. Re-enter the placement half at the rung the damage calls for:
       P2  a part whose position is a mechanical fact is wrong
       P3  the structure is wrong (a swap, a drag, a bad import)
       P4  local violations only
   python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/placement_driver.py \\
       --stage <P2|P3|P4> --board <the placed board> --before <its input>
3. Then restart the chain at L1. Do not resume routing from a stale board, and
   do not carry a routed net list across: the nets are the same, the corridors
   are not.

Next: --stage L1 --board <the re-placed board> --ledger {a.ledger}
</stage_instructions>'''


def l5(a):
    return f'''<stage_instructions stage="L5" name="close out" of="5">
The board is done when BOTH halves say so, measured, not when the router stops
producing failures.

  python3 -X utf8 check_drc.py {a.board} --clearance <floor> --clearance-margin 0
  python3 -X utf8 check_connected.py {a.board}
  python3 -X utf8 check_assembly.py {a.board}
  python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/board_score.py \\
      {a.board} --json wk/score_final.json

Connectivity is orthogonal to DRC: a DRC-clean board can be entirely
disconnected, because isolated copper has no clearance conflicts.

Then report, per half, with the number and the instrument beside it, and say
how many times the loop turned and why each turn happened. A chain that
re-entered placement twice is not a failure -- an unexplained one is.

<subagent_prompt agent="claude" description="verify the finished board">
Verify this board end to end, independently.

  board:  {a.board}
  ledger: {a.ledger}

Read .claude/skills/plan-pcb-routing/references/verifier-prompts.md and apply
its routed-board lenses, then check the PLACEMENT half too: the copper-free
gate cannot be re-run on a routed board, so verify it from the ledger's
recorded placement close-out and confirm the poses still match the board.

Re-derive every number yourself. Do not trust the report.
Answer with a line beginning VERDICT= and nothing above it.
</subagent_prompt>
</stage_instructions>'''


STAGES = {'L1': l1, 'L2': l2, 'L3': l3, 'L4': l4, 'L5': l5}
TITLES = {'L1': 'place (inline or delegated)',
          'L2': 'freeze what placement decided, then route',
          'L3': 'classify the failure -- the whole loop turns on this',
          'L4': 're-enter at the point the classification names',
          'L5': 'close out, both halves, measured'}


def _args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stage', choices=sorted(STAGES))
    ap.add_argument('--board', default='board.kicad_pcb')
    ap.add_argument('--ledger', default='wk/ledger.jsonl')
    ap.add_argument('--placement-report', default=None)
    ap.add_argument('--score', default=None)
    ap.add_argument('--shape', default=None, choices=SHAPES)
    ap.add_argument('--delegate', action='store_true',
                    help='hand a half to a TEAMMATE (not a plain subagent -- '
                         'each half spawns its own verifiers). A context '
                         'decision, not a correctness one')
    ap.add_argument('--accept-residue', action='store_true',
                    help='proceed to routing with a NAMED, measured-unfixable '
                         'placement residue')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--dump-all', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    a = _args(argv)
    if a.list:
        for k in ('L1', 'L2', 'L3', 'L4', 'L5'):
            print(f'  {k}  {TITLES[k]}')
        return 0
    if a.self_test:
        return _self_test()
    if a.dump_all:
        loose = _args(['--board', 'b.kicad_pcb', '--score', 's.json',
                       '--placement-report', 'p.json', '--shape', 'placement'])
        for k in sorted(STAGES):
            print(f'===== {k} =====')
            print(STAGES[k](loose))
        print('===== L1 (delegated) =====')
        loose.delegate = True
        print(STAGES['L1'](loose))
        return 0
    if not a.stage:
        print('loop_driver: --stage is required (see --list)', file=sys.stderr)
        return 2
    out = STAGES[a.stage](a)
    print(out)
    return 4 if out.startswith('<error>') else 0


def _self_test():
    import tempfile
    bad = []

    def want(cond, label):
        print(f'  {"PASS" if cond else "FAIL"}  {label}')
        if not cond:
            bad.append(label)

    base = ['--board', 'b.kicad_pcb']
    for key in sorted(STAGES):
        out = STAGES[key](_args(base + ['--score', 'x.json',
                                        '--placement-report', 'p.json',
                                        '--shape', 'placement']))
        want(out.startswith(('<stage_instructions', '<error>')),
             f'{key} emits a tagged block')
        want(len(out.splitlines()) <= 70, f'{key} stays under 70 lines')

    want(STAGES['L2'](_args(base)).startswith('<error>'),
         'routing refuses to start without a placement close-out')
    want(STAGES['L3'](_args(base)).startswith('<error>'),
         'classification refuses without a score')
    want(STAGES['L4'](_args(base)).startswith('<error>'),
         'a re-entry refuses without a measured shape')

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'p.json')
        json.dump({'blocking': 3}, open(p, 'w', encoding='utf-8'))
        out = STAGES['L2'](_args(base + ['--placement-report', p]))
        want(out.startswith('<error>') and 'blocking = 3' in out,
             'routing refuses a placement that still has blocking pairs')
        out = STAGES['L2'](_args(base + ['--placement-report', p,
                                         '--accept-residue']))
        want(out.startswith('<stage_instructions'),
             '...and proceeds when the residue is explicitly accepted')

        s = os.path.join(tmp, 's.json')
        json.dump({'blocking': 0}, open(s, 'w', encoding='utf-8'))
        out = STAGES['L3'](_args(base + ['--score', s]))
        want('nothing to classify' in out,
             'a clean score has no failure to classify')

    for shape, needle in (('parameter', 'the routed board stands'),
                          ('floorplan', 'different ARRANGEMENT'),
                          ('placement', 'stale')):
        out = STAGES['L4'](_args(base + ['--shape', shape]))
        want(needle in out, f'{shape}-shaped re-entry says what it costs')

    inline = STAGES['L1'](_args(base))
    deleg = STAGES['L1'](_args(base + ['--delegate']))
    want('<subagent_prompt' in deleg and '<subagent_prompt' not in inline,
         'delegation is a choice, and only the delegated form dispatches')
    want('TEAMMATE' in deleg and 'cannot spawn' in deleg,
         'delegation says a teammate is required, and why')

    # Assemble the emitted text from stages whose guards are SATISFIED --
    # otherwise this scans refusals and reports the instructions are missing
    # content the refusals were never going to carry.
    with tempfile.TemporaryDirectory() as tmp:
        ok_rep = os.path.join(tmp, 'p.json')
        json.dump({'blocking': 0}, open(ok_rep, 'w', encoding='utf-8'))
        ok_score = os.path.join(tmp, 's.json')
        json.dump({'blocking': 4}, open(ok_score, 'w', encoding='utf-8'))
        full = base + ['--score', ok_score, '--placement-report', ok_rep,
                       '--shape', 'placement']
        everything = '\n'.join(STAGES[k](_args(full)) for k in sorted(STAGES))
        for k in sorted(STAGES):
            want(not STAGES[k](_args(full)).startswith('<error>'),
                 f'{k} emits instructions once its evidence exists')
    for phrase in ('you may want to', 'if you are not sure', 'usually skip'):
        want(phrase not in everything.lower(), f'no hedging: {phrase!r}')
    want('copper is not evidence about placement' in everything,
         'the rule that only exists here is stated')

    print('OK' if not bad else f'FAIL: {len(bad)}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
