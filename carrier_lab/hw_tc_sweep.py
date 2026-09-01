#!/usr/bin/env python3
"""Dose-response sweep: heuristic_weight x turn_cost on the 8-board faithful corpus.

Replays the exact recorded chain commands (CMD lines from the si_corpus_ab logs,
same source as si_corpus_postmerge.py) with two knobs appended per step:
    --heuristic-weight <hw> --turn-cost <tc>

Arms (hw, tc):
    base    2.3 1000   (fresh baseline at HEAD -- defaults)
    hw19    1.9 1000
    hw15    1.5 1000
    hw12    1.2 1000
    tc3000  2.3 3000
    tc5000  2.3 5000

Politeness contract (other agents are working on this machine):
  - strictly serial: one route.py alive at a time, ever
  - every step waits for MemAvailable >= 8G AND no foreign route.py
  - nice -n 19 + ionice -c 3 on every route step
Resumable: existing step outputs are skipped, so kill/restart is safe.

Usage: hw_tc_sweep.py            # run everything
       hw_tc_sweep.py <arm>      # run one arm
"""
import json, os, re, shlex, subprocess, sys, time

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
SRC = os.path.join(ROOT, "carrier_lab", "si_corpus_ab")
OUT = os.path.join(ROOT, "carrier_lab", "hw_sweep")

ARMS = [
    ("base",   2.3, 1000),
    ("hw19",   1.9, 1000),
    ("hw15",   1.5, 1000),
    ("hw12",   1.2, 1000),
    ("tc3000", 2.3, 3000),
    ("tc5000", 2.3, 5000),
]

# key -> (chain_dir, staged_input_dir); fast boards first for early signal
BOARDS = [
    ("sonde_u",    "sonde_u_chain2", "sonde_u"),
    ("tigard",     "tigard_chain2", "tigard"),
    ("watchy",     "watchy_chain2", "watchy"),
    ("glasgow",    "glasgow_chain2", "glasgow_revC"),
    ("kitdev",     "kitdev_chain2", "kit-dev-coldfire-xilinx_5213"),
    ("ulx3s",      "ulx3s_chain3", "ulx3s"),
    ("interf_u",   "interf_u_chain2", "interf_u_unrouted"),
    ("haasoscope", "haasoscope_chain3", "haasoscope_pro_max_test"),
]

STEPS = ["a", "v", "n"]


def parse_cmd(logpath):
    with open(logpath) as f:
        line = f.readline()
    m = re.match(r'CMD:\s+(.*)$', line)
    if not m:
        raise RuntimeError("cannot parse CMD from {}: {!r}".format(logpath, line))
    toks = shlex.split(m.group(1))
    out = None
    rest = []
    i = 3
    while i < len(toks):
        if toks[i] == '--output':
            out = toks[i + 1]; i += 2
        else:
            rest.append(toks[i]); i += 1
    if out is None:
        raise RuntimeError("no --output in {}".format(logpath))
    return rest


def free_gb():
    out = subprocess.check_output(["free", "-g"]).decode()
    for ln in out.splitlines():
        if ln.startswith("Mem:"):
            return int(ln.split()[6])  # 'available' column
    return 0


def foreign_route_running():
    out = subprocess.run(["pgrep", "-af", "route.py"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "route.py" in l]


def wait_polite(tag):
    waited = False
    while free_gb() < 8:
        print("[{}] MemAvailable {}G < 8G, waiting 60s...".format(tag, free_gb()), flush=True)
        waited = True
        time.sleep(60)
    rp = foreign_route_running()
    while rp:
        print("[{}] foreign route.py running ({} procs), waiting 60s...".format(tag, len(rp)), flush=True)
        waited = True
        time.sleep(60)
        rp = foreign_route_running()
    if waited:
        print("[{}] clear, proceeding".format(tag), flush=True)


def run_arm_board(arm, hw, tc, key, chain_dir, staged, timings):
    bdir = os.path.join(SRC, chain_dir)
    odir = os.path.join(OUT, arm, key)
    os.makedirs(odir, exist_ok=True)

    cur_inp = os.path.join(SRC, staged, "input.kicad_pcb")
    for s in STEPS:
        rest = parse_cmd(os.path.join(bdir, "off_{}.log".format(s)))
        out_path = os.path.join(odir, "{}.kicad_pcb".format(s))
        tag = "{}/{}/{}".format(arm, key, s)
        if os.path.exists(out_path):
            print("[{}] exists, skip".format(tag), flush=True)
            cur_inp = out_path
            continue
        wait_polite(tag)
        cmd = [PY, os.path.join(ROOT, "py_router", "route.py"), cur_inp,
               "--output", out_path] + rest + [
               "--heuristic-weight", str(hw), "--turn-cost", str(tc)]
        logp = os.path.join(odir, "{}.log".format(s))
        print("[{}] start {}".format(tag, time.strftime('%H:%M:%S')), flush=True)
        t0 = time.time()
        with open(logp, "w") as lf:
            lf.write("CMD: " + " ".join(shlex.quote(c) for c in cmd) + "\n")
            lf.flush()
            r = subprocess.run(["nice", "-n", "19", "ionice", "-c", "3"] + cmd,
                               stdout=lf, stderr=subprocess.STDOUT)
        dt = time.time() - t0
        print("[{}] exit={} {:.1f}s".format(tag, r.returncode, dt), flush=True)
        timings.setdefault(arm, {}).setdefault(key, {})[s] = round(dt, 1)
        with open(os.path.join(OUT, "timings.json"), "w") as tf:
            json.dump(timings, tf, indent=1, sort_keys=True)
        if r.returncode != 0:
            # a failed step fails THIS board's remaining chain, not the sweep:
            # record and move on so one bad arm/board can't stall the night.
            print("[{}] FAILED rc={} -- skipping rest of this board".format(tag, r.returncode), flush=True)
            open(os.path.join(odir, "FAILED_step_{}".format(s)), "w").close()
            return
        cur_inp = out_path


def main():
    os.makedirs(OUT, exist_ok=True)
    tpath = os.path.join(OUT, "timings.json")
    timings = {}
    if os.path.exists(tpath):
        try:
            timings = json.load(open(tpath))
        except Exception:
            pass
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for arm, hw, tc in ARMS:
        if only and arm != only:
            continue
        print("=== ARM {} (hw={} tc={}) ===".format(arm, hw, tc), flush=True)
        for key, chain_dir, staged in BOARDS:
            run_arm_board(arm, hw, tc, key, chain_dir, staged, timings)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()
