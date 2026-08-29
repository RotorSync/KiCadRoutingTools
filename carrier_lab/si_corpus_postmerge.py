#!/usr/bin/env python3
"""Post-merge corpus validation: replay the SAME 8-board faithful chains on the
MERGED tree, ON arm only (current defaults, no knob overrides), fresh output
paths under carrier_lab/postmerge_validation/.

Replays the exact chain commands parsed verbatim from the original
si_corpus_ab logs (CMD lines), only output paths fresh. Usage:
    si_corpus_postmerge.py <board_key>
"""
import os, re, shlex, subprocess, sys, time

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
SRC = os.path.join(ROOT, "carrier_lab", "si_corpus_ab")
OUT = os.path.join(ROOT, "carrier_lab", "postmerge_validation")

# key -> (chain_dir, staged_input_dir)
BOARDS = {
    "tigard":      ("tigard_chain2", "tigard"),
    "watchy":      ("watchy_chain2", "watchy"),
    "sonde_u":     ("sonde_u_chain2", "sonde_u"),
    "interf_u":    ("interf_u_chain2", "interf_u_unrouted"),
    "kitdev":      ("kitdev_chain2", "kit-dev-coldfire-xilinx_5213"),
    "glasgow":     ("glasgow_chain2", "glasgow_revC"),
    "ulx3s":       ("ulx3s_chain3", "ulx3s"),
    "haasoscope":  ("haasoscope_chain3", "haasoscope_pro_max_test"),
}

STEPS = ["a", "v", "n"]


def parse_cmd(logpath):
    with open(logpath) as f:
        line = f.readline()
    m = re.match(r'CMD:\s+(.*)$', line)
    if not m:
        raise RuntimeError("cannot parse CMD from {}: {!r}".format(logpath, line))
    toks = shlex.split(m.group(1))
    inp = toks[2]
    out = None
    rest = []
    i = 3
    while i < len(toks):
        if toks[i] == '--output':
            out = toks[i+1]; i += 2
        else:
            rest.append(toks[i]); i += 1
    if out is None:
        raise RuntimeError("no --output in {}".format(logpath))
    return inp, out, rest


def free_gb():
    out = subprocess.check_output(["free", "-g"]).decode()
    for ln in out.splitlines():
        if ln.startswith("Mem:"):
            return int(ln.split()[6])
    return 0


def route_running():
    out = subprocess.run(["pgrep", "-af", "route.py"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "route.py" in l]


def run_board(key):
    chain_dir, staged = BOARDS[key]
    bdir = os.path.join(SRC, chain_dir)
    odir = os.path.join(OUT, key)
    os.makedirs(odir, exist_ok=True)

    steps = {}
    for s in STEPS:
        inp, outp, rest = parse_cmd(os.path.join(bdir, "off_{}.log".format(s)))
        steps[s] = (inp, outp, rest)

    cur_inp = os.path.join(SRC, staged, "input.kicad_pcb")
    for s in STEPS:
        _, _, rest = steps[s]
        out_path = os.path.join(odir, "on_{}.kicad_pcb".format(s))
        if os.path.exists(out_path):
            print("[{} {}] exists, skip".format(key, s), flush=True)
            cur_inp = out_path
            continue
        while free_gb() < 8:
            print("[{} {}] FREE MEM {}G < 8G, waiting...".format(key, s, free_gb()), flush=True)
            time.sleep(30)
        rp = route_running()
        if rp:
            print("[{} {}] route.py running: {}, waiting...".format(key, s, rp), flush=True)
            while route_running():
                time.sleep(30)
        cmd = [PY, os.path.join(ROOT, "py_router", "route.py"), cur_inp,
               "--output", out_path] + rest
        logp = os.path.join(odir, "on_{}.log".format(s))
        print("[{} {}] start {}".format(key, s, time.strftime('%H:%M:%S')), flush=True)
        t0 = time.time()
        with open(logp, "w") as lf:
            r = subprocess.run(["nice", "-n", "19"] + cmd,
                               stdout=lf, stderr=subprocess.STDOUT)
        dt = time.time() - t0
        print("[{} {}] exit={} {:.1f}s".format(key, s, r.returncode, dt), flush=True)
        if r.returncode != 0:
            print("[{} {}] FAILED rc={}".format(key, s, r.returncode), flush=True)
            sys.exit(1)
        cur_inp = out_path
    print("[{}] DONE".format(key), flush=True)


if __name__ == "__main__":
    run_board(sys.argv[1])
