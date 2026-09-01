#!/usr/bin/env python3
"""Mac bench variant of hw_tc_sweep.py (see that file for the design notes).

Differences from the desktop version:
  - paths: ~/krt_bench/krt_work, venv at ~/krt_bench/venv
  - memory gate uses macOS vm_stat (free+inactive+purgeable pages), floor 2.5G
  - no ionice on macOS; nice -n 19 only
  - arms are given on the command line: mac_bench_sweep.py <arm> [arm...]
    where arm = name:hw:tc, e.g. base:2.3:1000 combo:1.5:3000
Resumable: existing step outputs are skipped.
"""
import json, os, re, shlex, subprocess, sys, time

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, "krt_bench", "krt_work")
PY = os.path.join(HOME, "krt_bench", "venv", "bin", "python")
SRC = os.path.join(ROOT, "carrier_lab", "si_corpus_ab")
OUT = os.path.join(ROOT, "carrier_lab", "hw_sweep")

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
    rest = []
    i = 3
    while i < len(toks):
        if toks[i] == '--output':
            i += 2
        else:
            rest.append(toks[i]); i += 1
    return rest


def free_bytes():
    page = 16384
    out = subprocess.check_output(["vm_stat"]).decode()
    m = re.search(r"page size of (\d+)", out)
    if m:
        page = int(m.group(1))
    free = 0
    for key in ("Pages free", "Pages inactive", "Pages purgeable"):
        m = re.search(re.escape(key) + r":\s+(\d+)", out)
        if m:
            free += int(m.group(1))
    return free * page


def foreign_route_running():
    out = subprocess.run(["pgrep", "-af", "route.py"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "route.py" in l]


def wait_polite(tag):
    floor = 2.5 * (1 << 30)
    while free_bytes() < floor:
        print("[{}] free mem {:.1f}G < 2.5G, waiting 60s...".format(
            tag, free_bytes() / (1 << 30)), flush=True)
        time.sleep(60)
    while foreign_route_running():
        print("[{}] foreign route.py running, waiting 60s...".format(tag), flush=True)
        time.sleep(60)


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
            r = subprocess.run(["nice", "-n", "19"] + cmd,
                               stdout=lf, stderr=subprocess.STDOUT)
        dt = time.time() - t0
        print("[{}] exit={} {:.1f}s".format(tag, r.returncode, dt), flush=True)
        timings.setdefault(arm, {}).setdefault(key, {})[s] = round(dt, 1)
        with open(os.path.join(OUT, "timings.json"), "w") as tf:
            json.dump(timings, tf, indent=1, sort_keys=True)
        if r.returncode != 0:
            print("[{}] FAILED rc={} -- skipping rest of this board".format(tag, r.returncode), flush=True)
            open(os.path.join(odir, "FAILED_step_{}".format(s)), "w").close()
            return
        cur_inp = out_path


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: mac_bench_sweep.py name:hw:tc [name:hw:tc ...]")
    arms = []
    for a in sys.argv[1:]:
        name, hw, tc = a.split(":")
        arms.append((name, float(hw), int(tc)))
    os.makedirs(OUT, exist_ok=True)
    tpath = os.path.join(OUT, "timings.json")
    timings = {}
    if os.path.exists(tpath):
        try:
            timings = json.load(open(tpath))
        except Exception:
            pass
    for arm, hw, tc in arms:
        print("=== ARM {} (hw={} tc={}) ===".format(arm, hw, tc), flush=True)
        for key, chain_dir, staged in BOARDS:
            run_arm_board(arm, hw, tc, key, chain_dir, staged, timings)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()
