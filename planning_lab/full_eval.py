"""Full-width eval + failure-mode split for a param set on all boards."""
import sys, time, math, json
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi

WIDTHS = [0.25, 0.5, 1.0, 2.0, 3.0, 4.0]
SAMPLE_STEP = 0.2
BOARDS = [
    "kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb",
    "kicad_files/routed_output.kicad_pcb",
    "carrier_lab/routed.kicad_pcb",
    "/home/austin/eda/kstudio-workspace/helisync-carrier/helisync-carrier.kicad_pcb",
    "carrier_lab/d1_routed.kicad_pcb",
]

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

def q(vals,p):
    if not vals: return float("nan")
    s=sorted(vals); k=(len(s)-1)*p; f=math.floor(k); c=math.ceil(k)
    if f==c: return s[int(k)]
    return s[f]*(c-k)+s[c]*(k-f)

def main():
    fw=float(sys.argv[1]); fp=float(sys.argv[2]); al=float(sys.argv[3])
    label=sys.argv[4] if len(sys.argv)>4 else f"fw{fw}_fp{fp}_al{al}"
    out={}
    for b in BOARDS:
        t0=time.time()
        pcb=parse_kicad_pcb(b)
        graphs=build_graphs(pcb,0.1,0.1)
        res=plan_board_multi(pcb,graphs,0.1,0.1,via_size=0.3,
                             alpha=al,fidelity_weight=fw,fidelity_power=fp)
        act_by_net={}
        act_len={}
        for s in pcb.segments:
            if s.net_id<=0: continue
            d=math.hypot(s.end_x-s.start_x,s.end_y-s.start_y)
            act_by_net.setdefault(s.net_id,[]).append((s.start_x,s.start_y,s.end_x,s.end_y))
            act_len[s.net_id]=act_len.get(s.net_id,0)+d
        plan_by_net={}
        for np_ in res.nets:
            pts=[(x,y) for (l,x,y) in np_.path_pts]
            plan_by_net[np_.net_id]={"pts":pts,"len":polyline_len(pts)}
        nets=[]
        for nid in act_len:
            if act_len[nid]<=0 or nid not in plan_by_net: continue
            pts=plan_by_net[nid]["pts"]
            if len(pts)<2: continue
            segs=act_by_net.get(nid,[])
            c={W:sample_containment(segs,pts,W) for W in WIDTHS}
            nets.append({"c":c,"act":act_len[nid],"plan":plan_by_net[nid]["len"]})
        name=b.split("/")[-1]
        bo={}
        for W in WIDTHS:
            vals=[n["c"][W] for n in nets if n["c"][W]==n["c"][W]]
            bo[f"med{W}"]=q(vals,.5)
            bo[f"f80{W}"]=sum(1 for v in vals if v>=0.8)/len(vals)
            bo[f"f90{W}"]=sum(1 for v in vals if v>=0.9)/len(vals)
        ratios=[n["plan"]/n["act"] for n in nets if n["act"]>0]
        bo["lenratio_med"]=q(ratios,.5)
        bo["lenratio_mean"]=sum(ratios)/len(ratios)
        bo["n"]=len(nets)
        # mode split at W=2: wander vs reality-detour vs ok
        wander=[n for n in nets if n["plan"]>1.5*n["act"]]
        short=[n for n in nets if n["plan"]<0.75*n["act"]]
        fail_w2=[n for n in nets if n["c"][2]<0.8]
        bo["wander_n"]=len(wander); bo["short_n"]=len(short); bo["fail_w2_n"]=len(fail_w2)
        out[name]=bo
        print(f"== {name} == n={bo['n']} lenratio med={bo['lenratio_med']:.2f} mean={bo['lenratio_mean']:.2f} "
              f"wander={bo['wander_n']} short={bo['short_n']} fail@W2={bo['fail_w2_n']}", flush=True)
        print(f"   med: " + " ".join(f"W{W}={bo[f'med{W}']*100:.0f}%" for W in WIDTHS))
        print(f"   f80: " + " ".join(f"W{W}={bo[f'f80{W}']*100:.0f}%" for W in WIDTHS))
        print(f"   f90: " + " ".join(f"W{W}={bo[f'f90{W}']*100:.0f}%" for W in WIDTHS))
        print(f"   ({time.time()-t0:.0f}s)", flush=True)
    with open(f"/home/austin/krt_work/planning_lab/full_eval_{label}.json","w") as f:
        json.dump({"params":{"fw":fw,"fp":fp,"al":al},"boards":out},f)

if __name__=="__main__":
    main()
