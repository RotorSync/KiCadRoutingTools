"""Focused search around small-alpha region on TRAIN boards.
Checks whether alpha->small-positive keeps obstacle avoidance ON while
approaching pure-fidelity behavior -- vs degenerate alpha==0 which disables
the capacity<=0 obstacle penalty entirely."""
import sys, time, math, json
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi

WIDTHS = [0.5, 1.0, 2.0]
SAMPLE_STEP = 0.2
TRAIN = [
    "kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb",
    "kicad_files/routed_output.kicad_pcb",
    "carrier_lab/routed.kicad_pcb",
]
_cache = {}

def keepout_polys_for_layer(bi, layer):
    polys = []
    for k in bi.keepouts:
        if layer in k.get("layers", set()):
            if k.get("tracks_allowed", True):
                continue
            polys.append(k["polygon"])
    return polys

def build_graphs(pcb, tw, cl):
    bi = pcb.board_info
    graphs = {}
    for layer in bi.copper_layers:
        pads_on_layer = [p for plist in pcb.pads_by_net.values() for p in plist if layer in p.layers]
        graphs[layer] = build_capacity_graph(layer, pads_on_layer,
                                             keepout_polys_for_layer(bi, layer),
                                             bi.board_bounds, tw, cl)
    return graphs

def load_board(path):
    if path in _cache: return _cache[path]
    pcb = parse_kicad_pcb(path)
    graphs = build_graphs(pcb, 0.1, 0.1)
    act_segs = {}; act_len = {}
    for s in pcb.segments:
        if s.net_id <= 0: continue
        d = math.hypot(s.end_x-s.start_x,s.end_y-s.start_y)
        act_segs.setdefault(s.net_id,[]).append((s.start_x,s.start_y,s.end_x,s.end_y))
        act_len[s.net_id] = act_len.get(s.net_id,0)+d
    e = {"pcb":pcb,"graphs":graphs,"act_segs":act_segs,"act_len":act_len}
    _cache[path]=e
    return e

def point_seg_dist(px,py,ax,ay,bx,by):
    vx,vy=bx-ax,by-ay; wx,wy=px-ax,py-ay
    c1=vx*wx+vy*wy
    if c1<=0: return math.hypot(px-ax,py-ay)
    c2=vx*vx+vy*vy
    if c2<=c1: return math.hypot(px-bx,py-by)
    t=c1/c2
    return math.hypot(px-(ax+t*vx),py-(ay+t*vy))

def poly_dist(px,py,pts):
    return min(point_seg_dist(px,py,pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1]) for i in range(len(pts)-1))

def polyline_len(pts):
    return sum(math.hypot(pts[i+1][0]-pts[i][0],pts[i+1][1]-pts[i][1]) for i in range(len(pts)-1))

def sample_containment(segs, pts, W):
    if len(pts)<2: return 0.0 if segs else float("nan")
    inside=total=0
    for (x0,y0,x1,y1) in segs:
        L=math.hypot(x1-x0,y1-y0)
        if L<=0: continue
        n=max(1,int(L/SAMPLE_STEP))
        w=L/n
        for k in range(n):
            t=(k+0.5)/n
            px=x0+t*(x1-x0); py=y0+t*(y1-y0)
            total+=w
            if poly_dist(px,py,pts)<=W: inside+=w
    return inside/total if total>0 else float("nan")

def eval_board(path, params):
    e = load_board(path)
    res = plan_board_multi(e["pcb"], e["graphs"], 0.1, 0.1, via_size=0.3,
                           alpha=params["alpha"],
                           fidelity_weight=params["fidelity_weight"],
                           fidelity_power=params["fidelity_power"])
    plan_by_net = {}
    for np_ in res.nets:
        pts=[(x,y) for (l,x,y) in np_.path_pts]
        plan_by_net[np_.net_id]={"pts":pts,"len":polyline_len(pts)}
    nets=[]
    for nid in e["act_len"]:
        if e["act_len"][nid]<=0 or nid not in plan_by_net: continue
        pts=plan_by_net[nid]["pts"]
        if len(pts)<2: continue
        c={W:sample_containment(e["act_segs"].get(nid,[]),pts,W) for W in WIDTHS}
        nets.append({"c":c,"act":e["act_len"][nid],"plan":plan_by_net[nid]["len"]})
    return nets

def fitness(params):
    total_w={0.5:0.4,1.0:0.35,2.0:0.25}
    score=0
    per={}
    for b in TRAIN:
        nets=eval_board(b,params)
        s=sum(total_w[W]*sum(1 for n in nets if n["c"][W]==n["c"][W] and n["c"][W]>=0.8)/max(1,len(nets)) for W in total_w)
        per[b.split("/")[-1]]={"fit":s,"n":len(nets)}
        score+=s
    return score/len(TRAIN), per

combos=[]
for al in [0.125, 0.25]:
    for fw in [3.0]:
        combos.append({"alpha":al,"fidelity_weight":fw,"fidelity_power":1})
for al in [0.125]:
    for fw in [4.0]:
        combos.append({"alpha":al,"fidelity_weight":fw,"fidelity_power":1})
results=[]
for params in combos:
    t0=time.time()
    fit,per=fitness(params)
    results.append({"params":params,"fit":fit,"per":per})
    print(f"[{time.time()-t0:.0f}s] {params} fit={fit:.4f} per={ {k:f"{v['fit']:.3f}" for k,v in per.items()} }", flush=True)
best=max(results,key=lambda r:r["fit"])
print("BEST:",best["params"],best["fit"])
with open("/home/austin/krt_work/planning_lab/mini_search3.json","w") as f:
    json.dump(results,f)
