#!/usr/bin/env python3
"""Run the ADAPTIVE arm (KICAD_SI_ADAPTIVE=1, default) of the SI-enforcement
faithful chain for one board.

Replays the exact faithful chain (aggressors -> victims -> rest) from the
original si_corpus_ab logs, fresh output paths under si_corpus_adaptive/.
Only the adaptive arm is run here; OFF/ON arms already exist (verified
deterministic) in si_corpus_rescore/.
Usage: si_corpus_adaptive.py <board_key>
"""
import os, re, shlex, subprocess, sys, time

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
SRC = os.path.join(ROOT, "carrier_lab", "si_corpus_ab")
OUT = os.path.join(ROOT, "carrier_lab", "si_corpus_adaptive")

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
ENV = {"KICAD_SI_ENFORCE": "1", "KICAD_SI_ADAPTIVE": "1"}


def parse_cmd(logpath):
    with open(logpath) as f:
        line = f.readline()
    m = re.match(r'CMD:\s+(.*)$', line)
    if not m:
        raise RuntimeError(f"cannot parse CMD from {logpath}: {line!r}")
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
        raise RuntimeError(f"no --output in {logpath}")
    return inp, out, rest


def free_gb():
    out = subprocess.check_output(["free", "-g"]).decode()
    for ln in out.splitlines():
        if ln.startswith("Mem:"):
            return int(ln.split()[6])
    return 0


def run_board(key):
    chain_dir, staged = BOARDS[key]
    bdir = os.path.join(SRC, chain_dir)
    odir = os.path.join(OUT, key)
    os.makedirs(odir, exist_ok=True)

    steps = {}
    for s in STEPS:
        inp, outp, rest = parse_cmd(os.path.join(bdir, f"off_{s}.log"))
        steps[s] = (inp, outp, rest)

    cur_inp = os.path.join(SRC, staged, "input.kicad_pcb")
    for s in STEPS:
        _, _, rest = steps[s]
        out_path = os.path.join(odir, f"ad_{s}.kicad_pcb")
        if os.path.exists(out_path):
            print(f"[{key} ad {s}] exists, skip", flush=True)
            cur_inp = out_path
            continue
        if free_gb() < 8:
            print(f"[{key} ad {s}] FREE MEM {free_gb()}G < 8G, waiting...", flush=True)
            while free_gb() < 8:
                time.sleep(30)
        cmd = [PY, os.path.join(ROOT, "py_router", "route.py"), cur_inp,
               "--output", out_path] + rest
        full_env = dict(os.environ)
        full_env.update(ENV)
        logp = os.path.join(odir, f"ad_{s}.log")
        print(f"[{key} ad {s}] start {time.strftime('%H:%M:%S')}", flush=True)
        t0 = time.time()
        with open(logp, "w") as lf:
            r = subprocess.run(["nice", "-n", "19"] + cmd,
                               env=full_env, stdout=lf, stderr=subprocess.STDOUT)
        dt = time.time() - t0
        print(f"[{key} ad {s}] exit={r.returncode} {dt:.1f}s", flush=True)
        if r.returncode != 0:
            print(f"[{key} ad {s}] FAILED rc={r.returncode}", flush=True)
            sys.exit(1)
        cur_inp = out_path
    print(f"[{key}] DONE", flush=True)


if __name__ == "__main__":
    run_board(sys.argv[1])
