"""Extra diagnostics: plan-vs-actual length ratio (corridor wander), and
how much of each net's actual copper lies on layers the plan never touches.
"""
import json, math

def q(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)

def main():
    with open("/home/austin/krt_work/planning_lab/corridor_data.json") as f:
        data = json.load(f)
    for board in data:
        name = board["board"].split("/")[-1]
        nets = [n for n in board["nets"] if n.get("planned")]
        ratios = [n["plan_len_2d"] / n["act_len"] for n in nets if n["act_len"] > 0]
        wander = [n for n in nets if n["plan_len_2d"] > 2 * n["act_len"]]
        shrink = [n for n in nets if n["plan_len_2d"] < 0.5 * n["act_len"]]
        print(f"== {name} ==")
        print(f"plan/actual length ratio: p25={q(ratios,0.25):.2f} median={q(ratios,0.5):.2f} "
              f"p75={q(ratios,0.75):.2f} mean={sum(ratios)/len(ratios):.2f}")
        print(f"nets with plan_len > 2x actual (corridor wanders): {len(wander)}/{len(nets)} "
              f"({len(wander)/len(nets)*100:.0f}%)")
        print(f"nets with plan_len < 0.5x actual (corridor too short): {len(shrink)}/{len(nets)} "
              f"({len(shrink)/len(nets)*100:.0f}%)")

if __name__ == "__main__":
    main()
