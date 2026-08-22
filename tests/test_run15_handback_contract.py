#!/usr/bin/env python3
"""What a delegated half is ASKED for must satisfy the next stage's gate.

This is the coverage that did not exist, and its absence is why the gap
survived: `loop_driver`'s L1 prompt asked the placement teammate to return "the
four close-out measurements with their numbers", while the very next stage, L2,
refused to open without a `check_assembly.py --json` document that the prompt
never mentioned. Delegation was tested only as text emission -- "does the word
TEAMMATE appear" -- so nothing connected the two halves of that sentence.

Both directions are asserted here, for every artifact in the contract:

  * the prompt NAMES the path (so the teammate knows where to put it), and
  * the consuming stage ACCEPTS a document at that path, and REFUSES without
    it (so the path is load-bearing rather than decorative).

The paths are named by the PARENT, not chosen by the teammate, which is what
makes this testable at all: the test can predict them.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
DRIVER = os.path.join(ROOT, '.claude', 'skills',
                      'plan-pcb-placement-and-routing', 'scripts',
                      'loop_driver.py')


def run(args):
    r = subprocess.run([sys.executable, '-X', 'utf8', DRIVER] + args,
                       capture_output=True, text=True, timeout=600, cwd=ROOT)
    return r.returncode, r.stdout + r.stderr


def sha_of(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class HandbackContractTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='handback_')
        self.work = os.path.join(self.tmp, 'wk')
        os.makedirs(self.work)
        self.ledger = os.path.join(self.work, 'ledger.jsonl')
        self.board = os.path.join(self.work, 'in.kicad_pcb')
        open(self.board, 'w', encoding='utf-8').close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_with(self, *boards):
        with open(self.ledger, 'w', encoding='utf-8') as f:
            for i, b in enumerate(boards):
                f.write(json.dumps({'iteration': i, 'kind': 'placement',
                                    'accepted': True,
                                    'result_sha': sha_of(b)}) + '\n')

    # ------------------------------------------------------------------
    # L1 -> L2
    # ------------------------------------------------------------------
    def test_l1_names_every_path_l2_consumes(self):
        code, out = run(['--stage', 'L1', '--board', self.board,
                         '--ledger', self.ledger])
        self.assertEqual(code, 0, out[:400])
        w = self.work.replace('\\', '/')
        for want in (w + '/placed.kicad_pcb',
                     w + '/assembly_close.json',
                     w + '/place_close_render.json'):
            self.assertIn(want, out,
                          'the placement prompt must NAME this path: ' + want)
        self.assertIn('check_assembly.py', out)
        self.assertIn('--kind placement', out,
                      'the teammate must be told which kind to record')

    def test_what_l1_asks_for_opens_l2(self):
        """The whole point: follow the prompt, and the next gate opens."""
        placed = os.path.join(self.work, 'placed.kicad_pcb')
        shutil.copyfile(self.board, placed)
        rep = os.path.join(self.work, 'assembly_close.json')
        json.dump({'board': placed, 'blocking': 0, 'oob_pad_count': 0,
                   'locked_contacts': 0, 'buildable': True,
                   'verdict': 'buildable (blocking 0)'},
                  open(rep, 'w', encoding='utf-8'))
        self._ledger_with(placed)
        code, out = run(['--stage', 'L2', '--board', placed,
                         '--ledger', self.ledger, '--placement-report', rep])
        self.assertEqual(code, 0, 'L2 must open on exactly what L1 asked for:\n'
                         + out[:600])
        self.assertIn('DELEGATING:', out)

    def test_l2_refuses_without_the_document_l1_was_told_to_make(self):
        placed = os.path.join(self.work, 'placed.kicad_pcb')
        shutil.copyfile(self.board, placed)
        self._ledger_with(placed)
        code, out = run(['--stage', 'L2', '--board', placed,
                         '--ledger', self.ledger])
        self.assertEqual(code, 4)
        self.assertIn('check_assembly', out,
                      'and the refusal must name the command that makes it')

    def test_l2_refuses_a_board_no_lap_recorded(self):
        placed = os.path.join(self.work, 'placed.kicad_pcb')
        shutil.copyfile(self.board, placed)
        rep = os.path.join(self.work, 'assembly_close.json')
        json.dump({'board': placed, 'blocking': 0, 'oob_pad_count': 0,
                   'locked_contacts': 0, 'buildable': True,
                   'verdict': 'buildable (blocking 0)'},
                  open(rep, 'w', encoding='utf-8'))
        with open(self.ledger, 'w', encoding='utf-8') as f:      # wrong sha
            f.write(json.dumps({'iteration': 0, 'kind': 'placement',
                                'accepted': True, 'result_sha': '0' * 64}) + '\n')
        code, out = run(['--stage', 'L2', '--board', placed,
                         '--ledger', self.ledger, '--placement-report', rep])
        self.assertEqual(code, 4)
        self.assertIn('no lap records a board with this content hash', out)

    # ------------------------------------------------------------------
    # L2 -> L3 / L5
    # ------------------------------------------------------------------
    def _l2_prompt(self):
        placed = os.path.join(self.work, 'placed.kicad_pcb')
        shutil.copyfile(self.board, placed)
        rep = os.path.join(self.work, 'assembly_close.json')
        json.dump({'board': placed, 'blocking': 0, 'oob_pad_count': 0,
                   'locked_contacts': 0, 'buildable': True,
                   'verdict': 'buildable (blocking 0)'},
                  open(rep, 'w', encoding='utf-8'))
        self._ledger_with(placed)
        code, out = run(['--stage', 'L2', '--board', placed,
                         '--ledger', self.ledger, '--placement-report', rep])
        self.assertEqual(code, 0, out[:400])
        return placed, out

    def test_l2_names_every_path_l3_and_l5_consume(self):
        placed, out = self._l2_prompt()
        w = self.work.replace('\\', '/')
        for want in (w + '/routed.kicad_pcb', w + '/score.json',
                     w + '/route.log', w + '/routing_close.json'):
            self.assertIn(want, out,
                          'the routing prompt must NAME this path: ' + want)

    def test_l2_hands_down_authored_from_because_only_it_knows(self):
        placed, out = self._l2_prompt()
        w = self.work.replace('\\', '/')
        # --authored-from is "the board you were HANDED", which is the FROZEN
        # board, not the placed one. L2 no longer stamps locks back into
        # placed.kicad_pcb: doing so changed its content hash while the
        # placement half was still running, which read that as corruption and
        # reverted the file. The fab floors ride across intact because the
        # freeze is a copy_board.py + (locked yes) stamp, so this still answers
        # the question the check exists for -- what did the writeback loosen
        # against -- while naming a path the teammate could not have known.
        self.assertIn('--authored-from ' + w + '/frozen.kicad_pcb', out,
                      'the teammate cannot know the board it was handed; '
                      'without it check_complete cannot run its fab-floor '
                      'check and UNSOUND becomes unreachable')
        self.assertNotIn('--authored-from ' + placed.replace('\\', '/'), out,
                         'the PLACED board is the placement half\'s artifact '
                         'and its ledger binding; the routing half is handed '
                         'the frozen copy and must grade against that')

    def test_l2_freezes_into_a_new_file_never_in_place(self):
        placed, out = self._l2_prompt()
        w = self.work.replace('\\', '/')
        self.assertIn(w + '/frozen.kicad_pcb', out,
                      'L2 must name the file it freezes INTO')
        self.assertIn('DO NOT FREEZE IN PLACE', out,
                      'freezing in place races a still-running placement half')
        self.assertIn(w + '/freeze_refs.json', out,
                      'the freeze list is DECLARED by the placement half, not '
                      'inferred from a pose diff')
        for want in (w + '/frozen.kicad_pcb --kind placement',):
            self.assertIn(want, out,
                          'the freeze is a lap and must be recorded: ' + want)

    def test_l2_states_who_owns_each_classification(self):
        _placed, out = self._l2_prompt()
        self.assertIn('THE CLASSIFICATION DECIDES WHO FIXES IT', out)
        for shape in ('parameter', 'placement', 'floorplan'):
            self.assertIn('`%s`' % shape, out)
        self.assertIn('ends your turn', out,
                      'a placement/floorplan verdict must stop the teammate, '
                      'not become a router retry')

    def test_l2_asks_for_the_lens_verdicts_the_ledger_refuses_without(self):
        _placed, out = self._l2_prompt()
        self.assertIn('VERDICT=', out)
        self.assertIn('connectivity', out)
        self.assertIn('drc', out)
        self.assertIn('spec', out)

    # ------------------------------------------------------------------
    # the fence, on both prompts
    # ------------------------------------------------------------------
    def test_both_prompts_name_the_carriers_a_teammate_must_not_open(self):
        _c, l1 = run(['--stage', 'L1', '--board', self.board,
                      '--ledger', self.ledger])
        _placed, l2 = self._l2_prompt()
        for name, out in (('L1', l1), ('L2', l2)):
            self.assertIn('_truth/', out, name + ' must name the truth dir')
            self.assertIn('perturb.json', out,
                          name + ' must name the pose record')
            self.assertIn('ONLY board you may open', out)

    def test_both_prompts_carry_the_hand_script_disclosure_duty(self):
        """Run 19: the teammate built arrange.py v1-v5 undisclosed until
        challenged. The fence named the carriers a teammate must not OPEN,
        but said nothing about scripts a teammate might WRITE -- so the
        disclosure duty now travels in the same clause, on both prompts."""
        _c, l1 = run(['--stage', 'L1', '--board', self.board,
                      '--ledger', self.ledger])
        _placed, l2 = self._l2_prompt()
        for name, out in (('L1', l1), ('L2', l2)):
            self.assertIn('disclosed hand-assist', out,
                          name + ' must carry the hand-script disclosure duty')

    # ---- the budget doctrine, and the run-output keys it is read with ----

    #: Every passage this change adds to the routing skill, as (anchor,
    #: why-it-is-here). A test that pins only the sentences it happens to
    #: quote lets a reviewer delete whole sections and stay green -- that
    #: happened, so the cover is now section-level and enumerated.
    ADDED_SECTIONS = (
        ('Read the `JSON_SUMMARY_MIN:` line',
         'the one-line verdict #686 added, and which nothing documented'),
        ('It says NOTHING about whether the DRC floors held',
         'the writeback prints AFTER this line; without the caveat "read one '
         'line" is how the fab-floor ratchet hides'),
        ('`main_loop_time_s` counts the single-ended loop',
         'read as a run duration it is wrong by more than an order'),
        ('`status` appears only on a run that legitimately did nothing',
         'otherwise it reads as a verdict about the board'),
        ('`complete` means "a sub-run did not finish"',
         'not a clock; it is the sticky-incompleteness disclosure'),
        ('A note on vocabulary, because this section imports it',
         'blocking/quality/unrouted/broken are board_score keys, not '
         'route.py keys, and the table below reads both documents'),
        ('`failed_single` is HALF the answer',
         'a bucket-grep turns a reported failure into a silent one'),
        ('The routable denominator is ON-BOARD pads',
         'two nets no router could route sit in unrouted forever'),
        ('YOUR OWN CHECKS ARE INSTRUMENTS TOO',
         'the five-row table of checks that reported a result nobody had'),
        ('Learn its signature, because several instruments share it',
         'the three internal timeouts that degrade with no failure signal'),
        ('NO MAIN TAKES A WALL-CLOCK BUDGET',
         'the doctrine this whole change exists for'),
        ('Check the row count after every `converge.py record`',
         'the 126 is the shell\'s and the lap is silently gone'),
        ('Necking is floored at the FAB minimum',
         'a spec width is protected only down to the fab floor'),
        ('Bound `repair_planes.py` by SCOPE',
         'measured at 40 and 25 minutes with no board written'),
        ('A scoped `--nets` retry on a net that is ALREADY CONNECTED is a '
         'no-op',
         'run 20 spent a lap on a retry that could not change anything'),
        ('The hint\'s suggested values are an EXAMPLE, not a derivation',
         'on a board at the floor the hint recommends the values in force'),
        ('The box-in row needs one qualification',
         'the at_floor rework: 9.3d says stay on parameters, which is '
         'right only while the geometry has somewhere to go'),
        ('Check `protected_nets` is still there before relying on it',
         'a project-file helper that replaces rather than merges deletes '
         'the record, and the log tell only prints on a '
         '--rip-blocker-nets call'),
        ('a blocker that is geometrically unsatisfiable, which is a '
         'finding about the REQUIREMENT',
         'the stop conditions: a run has to say which one fired, and '
         'this is the one that is a finding rather than a failure'),
    )

    def test_every_section_this_change_added_is_still_there(self):
        """Section-level cover for the doctrine, not sentence-level.

        The first version of this test asserted only the eight sentences it
        quoted. A reviewer deleted five whole passages -- the instruments
        table, the stop conditions, the retry section, the on-board-pads
        bullet, the QSPI_SD1 bullet -- and it stayed green. Enumerating the
        sections is what makes "the doctrine is pinned" true rather than a
        claim about eight strings.
        """
        _txt, flat = self._skill()
        missing = [(a, why) for a, why in self.ADDED_SECTIONS if a not in flat]
        self.assertFalse(
            missing,
            'these passages are gone from plan-pcb-routing/SKILL.md:\n'
            + '\n'.join('  %s\n      (%s)' % (a, w) for a, w in missing))
        # An anchor that also matches a CROSS-REFERENCE elsewhere in the
        # file pins nothing: deleting the section leaves the pointer behind
        # and this test stays green. Measured -- that is exactly how the
        # silent-timeout section was deletable while advertised as covered.
        dupes = [(a, flat.count(a)) for a, _ in self.ADDED_SECTIONS
                 if flat.count(a) != 1]
        self.assertFalse(
            dupes,
            'each anchor must occur EXACTLY once, or it is matching a '
            'cross-reference rather than the passage: %r' % (dupes,))

    def test_the_constants_the_doctrine_quotes_re_derive(self):
        """A measured number in a doc is a claim about the code.

        The silent-timeout table names two timeouts by value. Falsifying
        either -- 300 to 900, say -- used to leave every test green, which is
        the same defect as documenting a flag that does not exist: the reader
        acts on a number nobody checked.
        """
        _txt, flat = self._skill()
        for mod, const in (('kicad_exact_fill.py', 'EXACT_FILL_TIMEOUT'),
                           ('kicad_oracle.py', 'ORACLE_DRC_TIMEOUT')):
            with open(os.path.join(ROOT, 'py_router', mod),
                      encoding='utf-8') as fh:
                src = fh.read()
            m = re.search(r'^%s = (\d+)$' % const, src, re.M)
            self.assertIsNotNone(m, const + ' is not defined in ' + mod)
            self.assertIn('`py_router/%s`) | %s s |' % (mod, m.group(1)), flat,
                          '%s is %s s in %s; the skill quotes a different '
                          'number' % (const, m.group(1), mod))
        # No absolute wall-clock pair, in a passage arguing that wall-clock is
        # a property of the machine. The ratio is the claim.
        self.assertNotIn('20.72', flat,
                         'an absolute second-count re-derives differently on '
                         'every machine, which refutes the passage it sits in')

    def test_the_summary_min_key_list_re_derives(self):
        """The skill lists the keys of `JSON_SUMMARY_MIN`. Build the real one.

        This is the rewrite most worth catching: renaming a key in the doc to
        one that does not exist sends the reader grepping a log for something
        no tool prints, and a test that matches strings stays green through it.
        So the list is not pinned as a string -- it is compared against what
        `route_summary.summary_min` actually returns.

        `scope` is deliberately not required in the prose (it is always the
        literal 'merged' and says nothing a reader acts on); every other key
        must be named, and the prose must name no key the function cannot
        produce.
        """
        sys.path.insert(0, os.path.join(ROOT, 'py_router'))
        from route_summary import summary_min                    # noqa: E402
        real = set(summary_min({}))
        real |= set(summary_min({'finalize_excluded_nets': ['X']}))
        _txt, flat = self._skill()
        # Harvest ONLY the key-list sentence, not a 700-char window: an
        # ordinary backticked word in neighbouring prose (`forensics`) used to
        # fail this test as an "invented key". A gate that cries wolf on
        # innocent prose gets deleted, and takes its real findings with it.
        start = flat.index('it carries the MERGED tally')
        end = flat.index('The big `JSON_SUMMARY` lines', start)
        passage = flat[start:end]
        listed = set(re.findall(r'`([a-z_]{4,})`', passage))
        missing = sorted(k for k in real - {'scope'} if k not in listed)
        self.assertFalse(missing,
                         'JSON_SUMMARY_MIN carries these keys and the skill '
                         'does not name them: %s' % missing)
        invented = sorted(k for k in listed if k not in real)
        self.assertFalse(invented,
                         'the skill names these as JSON_SUMMARY_MIN keys and '
                         'summary_min produces no such key: %s' % invented)

    def test_the_claims_that_invert_are_the_right_way_round(self):
        """Rewrites a deletion-shaped test cannot see.

        Each of these is a sentence whose OPPOSITE is grammatical, reads as
        authoritative, and is wrong. Deleting the passage fails the section
        cover; inverting it passed everything, so each is now checked against
        the thing it is a claim about rather than against itself.
        """
        _txt, flat = self._skill()

        # 1. --evict-depth's default, re-derived from place_seed's argparse.
        with open(os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                  encoding='utf-8') as fh:
            seed = fh.read()
        m = re.search(r'"--evict-depth".*?default=(\d+)', seed, re.S)
        self.assertIsNotNone(m, 'place_seed no longer takes --evict-depth')
        self.assertEqual(m.group(1), '0',
                         'the skill says --evict-depth 0 is already the '
                         'default; place_seed now defaults to '
                         + (m.group(1) if m else '?'))
        self.assertIn('--evict-depth 0` (the default)', flat,
                      'inverted, this tells the reader to PASS a flag that '
                      'already holds -- the opposite of the advice')

        # 2. the writeback runs AFTER the MIN line. That ordering IS the
        #    caveat; reversed, a clean MIN line would cover the floors.
        self.assertIn('writeback runs after this line prints', flat.lower(),
                      'if the writeback ran BEFORE, the MIN line would say '
                      'something about the floors and the caveat would be '
                      'pointless')

        # 3. the floor to compare against is the board's `.kicad_dru` and the
        #    fab minimums -- per-layer clearance cannot be expressed in a
        #    netclass at all (#498), so `.kicad_pro` is the wrong file.
        box = flat[flat.index('The box-in row needs one qualification'):]
        self.assertIn('`.kicad_dru` rules and\nthe fab minimums'.replace(
            '\n', ' '), ' '.join(box[:1600].split()),
            'the floor comes from the rules file and the fab, not from the '
            'project netclass')

        # 4. the on-board-pad counts, and the key that reports them.
        self.assertIn('147 against 149', flat,
                      'the measurement is what makes the rule actionable')
        self.assertIn('components.unrouted.placement_blocked', flat)
        with open(os.path.join(
                ROOT, '.claude', 'skills', 'plan-pcb-placement-and-routing',
                'scripts', 'board_score.py'), encoding='utf-8') as fh:
            self.assertIn("'placement_blocked'", fh.read(),
                          'the skill sends the reader to a board_score key, '
                          'so it has to be one board_score emits')

        # 5. the sibling's boxed-in row is QUALIFIED here, not contradicted,
        #    and this passage quotes it. If the row is reworded the quotation
        #    becomes a misattribution.
        with open(os.path.join(
                ROOT, '.claude', 'skills', 'plan-pcb-placement-and-routing',
                'SKILL.md'), encoding='utf-8') as fh:
            sib = ' '.join(fh.read().split())
        # The WHOLE row, to its end: the tail is the half the routing skill
        # quotes and then argues against, so pinning only the head lets the
        # tail be reworded into something the quotation misattributes.
        self.assertIn('`blockers` empty; the log says boxed in by static '
                      'obstacles | **parameters** | stay here — grid, '
                      'ripup budget, width. Placement is not the lever', sib,
                      'the qualification quotes 9.3d verbatim, tail included')

    def test_the_sibling_pointers_point_somewhere(self):
        """Three sections are one-line pointers into the combined skill.

        A pointer to a heading that has been renamed is worse than a
        duplicate: the reader follows it, finds nothing, and re-derives.
        """
        _txt, flat = self._skill()
        sib = os.path.join(ROOT, '.claude', 'skills',
                           'plan-pcb-placement-and-routing', 'SKILL.md')
        with open(sib, encoding='utf-8') as fh:
            sib_txt = fh.read()
        self.assertIn('.claude/skills/plan-pcb-placement-and-routing/SKILL.md',
                      flat, 'the pointers must name the file by path')
        for heading in ('##### 9.3a — RE-ENTER AT THE FAILING STEP',
                        '##### 9.3b — READ THE ROUTER\'S HINT',
                        '##### 9.3d — Classify the blocker, then pick',
                        '#### 9.5 — Stop conditions'):
            self.assertIn(heading, sib_txt,
                          'the routing skill points at ' + heading
                          + ', which no longer exists in the combined skill')

    def _skill(self):
        """The routing skill, and a whitespace-flattened copy of it.

        Flattened, because every pin below is a SENTENCE and a sentence in a
        markdown file wraps wherever the paragraph happens to end. Pinning the
        wrapped form makes the test fail on a reflow, which teaches the next
        reader to delete the test rather than keep the doctrine.
        """
        p = os.path.join(ROOT, '.claude', 'skills', 'plan-pcb-routing',
                         'SKILL.md')
        with open(p, encoding='utf-8') as fh:
            txt = fh.read()
        return txt, ' '.join(txt.split())

    def test_l2_tells_the_teammate_how_to_hand_back_a_long_step(self):
        """A subagent cannot block on a detached process, so it must not try.

        Both routing halves in run 20 returned "still working, I'll wait" --
        because a teammate has no way to sit on a backgrounded step and be
        woken when it exits. The orchestrator armed the wait itself and resumed
        them twice, and each round trip cost a re-read of the whole context.

        The prompt names the shape -- LOG / MARKER / NEXT -- says it is a
        correct hand-back rather than a failure (an agent that thinks it failed
        keeps trying to finish, which is the behaviour being fixed), and gives
        the step a bound. That last part is the one that has to stay
        deterministic: no tool stops itself on a clock any more.
        """
        _placed, out = self._l2_prompt()
        flat = ' '.join(out.split())
        for token in ('LOG=', 'MARKER=', 'NEXT='):
            self.assertIn(token, out,
                          'the hand-back shape must name ' + token)
        self.assertIn('correct hand-back, not a', out,
                      'a half that hands back correctly must not report it as '
                      'a failure, or the parent reads it as one')
        self.assertIn('written by the STEP', out,
                      'a marker the HALF writes says the work finished when '
                      'it has not started')
        self.assertIn('scope it can finish', out,
                      'the hand-back instruction itself must carry the bound. '
                      'A hand-back whose step never terminates turns one '
                      'blocked agent into two')
        self.assertIn('No step has a wall-clock budget', flat,
                      'the teammate reaches for --deadline otherwise, and '
                      'gets an argparse error (#621)')
        self.assertIn('bound them by SCOPE', flat,
                      'SCOPE is the only bound left that keeps the run '
                      'deterministic')

    def test_the_budget_doctrine_is_deterministic(self):
        """#621 removed every `--deadline`, and the doctrine has to say WHY.

        The rule: no result may depend on timing, because the same board with
        the same arguments has to produce the same copper on a slow machine and
        a fast one. What replaces the flag is not "wrap it in a shell timeout"
        -- that only destroys the partial board and hands you the shell's exit
        code -- but the caps the tools actually take, every one of them a COUNT
        or a SET.

        Fails if the doctrine is deleted, and fails if it is rewritten back
        into a wall-clock one.
        """
        txt, flat = self._skill()
        self.assertIn('the same board with the same arguments must produce '
                      'the same copper on a slow machine and a fast one', flat,
                      'the doctrine must state the DETERMINISM reason, not '
                      'just the fact that the flag is gone')
        self.assertIn('A `timeout` SIGTERMs the tool', flat,
                      'the reason a shell timeout is not a substitute has to '
                      'be stated where the reader would reach for one')
        for cap in ('`route.py --nets`', '`--max-ripup`', '--evict-depth 0',
                    'verdict --flat'):
            self.assertIn(cap, flat,
                          cap + ' is one of the deterministic caps that '
                          'replaced the clock; without them the doctrine says '
                          'stop and offers nothing to do instead')
        for gone in ('Pass `--deadline`', 'KRT_DEADLINE_S', 'status: deadline',
                     'DEADLINE:'):
            self.assertNotIn(gone, txt,
                             gone + ' prescribes a mechanism #621 removed; '
                             'passing it is an argparse error')
        self.assertIn('NO MAIN TAKES A WALL-CLOCK BUDGET, AND THAT IS '
                      'DELIBERATE', txt)
        # The hand-back pattern and the budget doctrine are the same problem
        # from the two ends, so the budget passage has to say where the other
        # end lives. It is NOT restated here: plan-pcb-routing has no loop, no
        # ledger and no teammates, and orchestration doctrine in a skill that
        # cannot orchestrate is how a passage gets applied by the wrong agent.
        self.assertIn('loop_driver.py', flat,
                      'the budget passage must point at the driver that owns '
                      'the delegation half')
        for orchestration in ('A DELEGATED HALF CANNOT BLOCK',
                              'the COMPLETION MARKER |'):
            self.assertNotIn(orchestration, txt,
                             orchestration + ' is orchestration doctrine and '
                             'belongs in loop_driver.py, which has teammates')
        with open(os.path.join(ROOT, '.claude', 'skills',
                               'plan-pcb-placement-and-routing', 'scripts',
                               'loop_driver.py'), encoding='utf-8') as fh:
            ld = fh.read()
        self.assertIn('HAND BACK -- do not wait on it', ld,
                      'the delegation half has to exist somewhere, and this '
                      'is where it was moved to')

    def test_the_run_output_keys_are_described_as_they_are(self):
        """`complete` and `status` are not clocks, and route.py agrees.

        `complete` is the sticky-incompleteness key: nothing writes it today,
        because no main can stop early, but `merge_summaries` forces it onto a
        merged summary when any part carries it and `place_route_loop` refuses
        one. `status` on JSON_SUMMARY_MIN names why an EMPTY tally is empty.
        Describing either as "the budget fired" sends a reader looking for a
        clock that does not exist.

        The `finalize_excluded_nets` claim is checked against route.py itself,
        because a doc that names a summary key nobody sets is the same defect
        as one that names a flag nobody registers.
        """
        _txt, flat = self._skill()
        self.assertIn('`complete` means "a sub-run did not finish", never '
                      '"a budget expired"', flat)
        self.assertIn('no_valid_nets', flat,
                      "the two real values of JSON_SUMMARY_MIN's `status` key "
                      'belong beside it, or it keeps being read as a verdict')
        self.assertIn('finalize_excluded_nets', flat)
        # The box-in verdict is read off the key route.py actually emits.
        # `boxed_in[]` carries `geometry`; it carries no floor comparison, and
        # a doctrine that names one sends the reader after a key nobody sets.
        self.assertIn('`boxed_in[].geometry`', flat,
                      'the box-in row must name the key that exists and tell '
                      'the reader to make the floor comparison themselves')
        for gone in ('at_floor', 'boxed_in[].floors'):
            self.assertNotIn(gone, flat,
                             gone + ' is not a key any main emits')
        with open(os.path.join(ROOT, 'py_router', 'route.py'),
                  encoding='utf-8') as fh:
            rt = fh.read()
        self.assertIn("summary['finalize_excluded_nets'] = _excluded9", rt,
                      'the key the skill tells a reader to look for must be '
                      'the key route.py sets')
        self.assertIn('{RED}  Plane finalize: zone net(s)', rt,
                      'the printed JSON_SUMMARY predates the finalize, so the '
                      'log line has to carry its own severity -- which on '
                      'main is the RED escape, not a WARNING: prefix')
        with open(os.path.join(ROOT, 'py_router', 'route_summary.py'),
                  encoding='utf-8') as fh:
            rs = fh.read()
        self.assertIn("merged['complete'] = False", rs,
                      'incompleteness is sticky through the merge, which is '
                      'the whole reason the key still exists')


if __name__ == '__main__':
    unittest.main(verbosity=2)
