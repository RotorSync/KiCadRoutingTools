#!/usr/bin/env python3
"""Grade every completed hw_tc_sweep arm/board: completion (check_connected,
check_drc at the board's routed floor) + quality (score.py final + the visual
sub-scores the sweep is about). Writes hw_sweep/grades.json and prints tables.

Grades only boards whose final step output (n.kicad_pcb) exists and whose dir
carries no FAILED marker; missing/failed cells print as '-' (no silent skips).
Usage: hw_tc_sweep_grade.py
"""
import json, os, re, subprocess, sys

ROOT = "/home/austin/krt_work"
PY = "/home/austin/eda/.venv/bin/python"
OUT = os.path.join(ROOT, "carrier_lab", "hw_sweep")

# same routed floors as si_corpus_postmerge_grade.py
CLR = {
    "sonde_u": 0.1, "tigard": 0.15, "watchy": 0.09, "glasgow": 0.09,
    "kitdev": 0.09, "ulx3s": 0.09, "interf_u": 0.1, "haasoscope": 0.09,
}
ARMS = ["base", "hw19", "hw15", "hw12", "tc3000", "tc5000"]
SUBS = ["jog_chains", "bends", "parallel", "fragmentation", "stubs",
        "pad_entry", "vias", "layer_direction", "si_coupling"]


def run(args, cwd=ROOT):
    return subprocess.run([PY] + args, capture_output=True, text=True, cwd=cwd)


def grade_board(arm, key):
    odir = os.path.join(OUT, arm, key)
    b = os.path.join(odir, "n.kicad_pcb")
    if not os.path.exists(b):
        return None
    if any(f.startswith("FAILED") for f in os.listdir(odir)):
        return {"failed": True}
    r = run(["py_router/check_connected.py", b])
    conn_out = r.stdout + r.stderr
    issues = unrouted = 0
    for ln in conn_out.splitlines():
        m = re.search(r"Connectivity issues \((\d+)\)", ln)
        if m: issues = int(m.group(1))
        m = re.search(r"Unrouted nets \((\d+)\)", ln)
        if m: unrouted = int(m.group(1))
    r = run(["py_router/check_drc.py", b, "--clearance", str(CLR[key]),
             "--clearance-margin", "0.1"])
    drc_out = r.stdout + r.stderr
    drc = 0
    for ln in drc_out.splitlines():
        if "FOUND" in ln and "DRC VIOLATIONS" in ln:
            try: drc = int(ln.split()[1])
            except Exception: pass
    sj = os.path.join(odir, "score.json")
    run(["quality/score.py", b, "--json", sj])
    score = {}
    try:
        score = json.load(open(sj))
    except Exception:
        pass
    subs = {k: (score.get("sub_scores", {}).get(k, {}) or {}).get("sub_score")
            for k in SUBS}
    return {"conn": issues, "unrouted": unrouted, "drc": drc,
            "final": score.get("final_score"), "subs": subs}


def fmt(v, w=6):
    if v is None:
        return "-".rjust(w)
    if isinstance(v, float):
        return "{:.1f}".format(v).rjust(w)
    return str(v).rjust(w)


def main():
    grades = {}
    for arm in ARMS:
        for key in CLR:
            g = grade_board(arm, key)
            if g is not None:
                grades.setdefault(arm, {})[key] = g
                print("graded {}/{}".format(arm, key), flush=True)
    with open(os.path.join(OUT, "grades.json"), "w") as f:
        json.dump(grades, f, indent=1, sort_keys=True)

    boards = list(CLR)
    print("\n== completion (conn issues / unrouted / DRC) ==")
    print("board".ljust(11) + "".join(a.rjust(14) for a in ARMS))
    for key in boards:
        row = key.ljust(11)
        for arm in ARMS:
            g = grades.get(arm, {}).get(key)
            if not g or g.get("failed"):
                row += ("FAIL" if g else "-").rjust(14)
            else:
                row += "{}/{}/{}".format(g["conn"], g["unrouted"], g["drc"]).rjust(14)
        print(row)
    print("\n== final score ==")
    print("board".ljust(11) + "".join(a.rjust(8) for a in ARMS))
    for key in boards:
        row = key.ljust(11)
        for arm in ARMS:
            g = grades.get(arm, {}).get(key)
            row += fmt(None if not g or g.get("failed") else g["final"], 8)
        print(row)
    for sub in SUBS:
        print("\n== {} ==".format(sub))
        print("board".ljust(11) + "".join(a.rjust(8) for a in ARMS))
        for key in boards:
            row = key.ljust(11)
            for arm in ARMS:
                g = grades.get(arm, {}).get(key)
                v = None if not g or g.get("failed") else g["subs"].get(sub)
                row += fmt(v, 8)
            print(row)


if __name__ == "__main__":
    main()
