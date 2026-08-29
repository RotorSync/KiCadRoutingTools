#!/usr/bin/env python3
"""Grade the post-merge ON-arm outputs: check_connected + check_drc + score.
Usage: si_corpus_postmerge_grade.py <board_key>
"""
import json, os, re, subprocess, sys

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
OUT = os.path.join(ROOT, "carrier_lab", "postmerge_validation")

CLR = {
    "tigard": 0.15, "watchy": 0.09, "sonde_u": 0.1, "interf_u": 0.1,
    "kitdev": 0.09, "glasgow": 0.09, "ulx3s": 0.09, "haasoscope": 0.09,
}


def run(args):
    return subprocess.run([PY] + args, capture_output=True, text=True)


def grade(key):
    clr = CLR[key]
    odir = os.path.join(OUT, key)
    b = os.path.join(odir, "on_n.kicad_pcb")
    r = run(["py_router/check_connected.py", b])
    conn = r.stdout + r.stderr
    issues = unrouted = 0
    for ln in conn.splitlines():
        m = re.search(r"Connectivity issues \((\d+)\)", ln)
        if m: issues = int(m.group(1))
        m = re.search(r"Unrouted nets \((\d+)\)", ln)
        if m: unrouted = int(m.group(1))
    r = run(["py_router/check_drc.py", b, "--clearance", str(clr), "--clearance-margin", "0.1"])
    drc_out = r.stdout + r.stderr
    drc = 0
    for ln in drc_out.splitlines():
        if "FOUND" in ln and "DRC VIOLATIONS" in ln:
            try: drc = int(ln.split()[1])
            except Exception: pass
    sj = os.path.join(odir, "on_score.json")
    r = run(["quality/score.py", b, "--json", sj])
    score = {}
    try:
        score = json.load(open(sj))
    except Exception:
        pass
    si = score.get("sub_scores", {}).get("si_coupling", {}).get("sub_score")
    final = score.get("final_score")
    print("{} on: conn={} unrouted={} drc={} si_coup={} final={}".format(
        key, issues, unrouted, drc, si, final), flush=True)
    return {"conn": issues, "unrouted": unrouted, "drc": drc,
            "si_coup": si, "final": final}


if __name__ == "__main__":
    grade(sys.argv[1])
