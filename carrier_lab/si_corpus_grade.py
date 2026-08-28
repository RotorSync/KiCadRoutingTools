#!/usr/bin/env python3
"""Grade both arms of a re-scored board: check_connected + check_drc + score.
Usage: si_corpus_grade.py <board_key>
"""
import json, os, subprocess, sys

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
OUT = os.path.join(ROOT, "carrier_lab", "si_corpus_rescore")

# key -> clearance floor (from the original chain CMDs)
CLR = {
    "tigard": 0.15, "watchy": 0.09, "sonde_u": 0.1, "interf_u": 0.1,
    "kitdev": 0.09, "glasgow": 0.09, "ulx3s": 0.09, "haasoscope": 0.09,
}


def run(args):
    return subprocess.run([PY] + args, capture_output=True, text=True)


def grade(key):
    clr = CLR[key]
    odir = os.path.join(OUT, key)
    res = {}
    for arm in ["off", "on"]:
        b = os.path.join(odir, f"{arm}_n.kicad_pcb")
        r = run(["py_router/check_connected.py", b])
        conn = r.stdout + r.stderr
        issues = 0
        unrouted = 0
        import re as _re
        for ln in conn.splitlines():
            m = _re.search(r"Connectivity issues \((\d+)\)", ln)
            if m: issues = int(m.group(1))
            m = _re.search(r"Unrouted nets \((\d+)\)", ln)
            if m: unrouted = int(m.group(1))
        r = run(["py_router/check_drc.py", b, "--clearance", str(clr), "--clearance-margin", "0.1"])
        drc_out = r.stdout + r.stderr
        drc = 0
        for ln in drc_out.splitlines():
            if "FOUND" in ln and "DRC VIOLATIONS" in ln:
                try: drc = int(ln.split()[1])
                except Exception: pass
        r = run(["quality/score.py", b, "--json", os.path.join(odir, f"{arm}_score.json")])
        score = {}
        try:
            score = json.load(open(os.path.join(odir, f"{arm}_score.json")))
        except Exception:
            pass
        si = score.get("sub_scores", {}).get("si_coupling", {}).get("sub_score")
        final = score.get("final_score")
        res[arm] = {"conn": issues, "unrouted": unrouted, "drc": drc,
                    "si_coup": si, "final": final}
        print(f"{key} {arm}: conn={issues} unrouted={unrouted} drc={drc} si_coup={si} final={final}", flush=True)
    return res


if __name__ == "__main__":
    grade(sys.argv[1])
