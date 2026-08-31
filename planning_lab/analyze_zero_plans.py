"""Count zero-length plans and copper on unplanned layers."""
import json, math

def main():
    with open("/home/austin/krt_work/planning_lab/corridor_data.json") as f:
        data = json.load(f)
    for board in data:
        name = board["board"].split("/")[-1]
        nets = [n for n in board["nets"] if n.get("planned")]
        zero = [n for n in nets if n["plan_len_2d"] < 0.01]
        print(f"== {name} == zero-length corridors: {len(zero)}/{len(nets)}")
        for n in zero[:6]:
            print(f"   {n['name'][:30]:<30} act={n['act_len']:.1f} plan={n['plan_len_2d']:.2f}")

if __name__ == "__main__":
    main()
