"""#589: classify every FAILED attempt block in a route log.
Categories per attempt: WALLED (majority of frontier static/unrippable),
COMPETITION (majority attributed to rippable routed nets), VIA-DECLINE
(via-in-pad / last-resort via declines present), EXHAUSTED (hit big
iteration counts without a wall)."""
import re, sys
from collections import defaultdict

log = open(sys.argv[1], errors="replace").read()
blocks = re.split(r"(?=^\[\d+/\d+[^\]]*\] Routing )", log, flags=re.M)
final = {}   # net -> (category, detail) of its LAST failed attempt
attempts = defaultdict(int)
for b in blocks:
    m = re.match(r"\[\d+/\d+[^\]]*\] Routing (.+) \(id=(\d+)\)", b)
    if not m:
        continue
    net = m.group(1)
    if "FAILED:" not in b:
        final.pop(net, None) if "SUCCESS:" in b and net in final else None
        if "SUCCESS:" in b:
            final.pop(net, None)
        continue
    attempts[net] += 1
    cov = re.search(r"Coverage: (\d+)/(\d+) frontier cells attributed to "
                    r"routed nets; (\d+) static/unrippable", b)
    it = re.search(r"No route found after (\d+) iterations", b)
    iters = int(it.group(1)) if it else 0
    decl = ("via-in-pad decline" if "Via-in-pad decline" in b else
            "last-resort-via" if "last-resort via" in b else "")
    blockers = re.findall(r"\d+\. (\S+): \d+ \(([\d.]+)%\)", b)
    if cov:
        attr, tot, static = map(int, cov.groups())
        if static > attr:
            cat = "WALLED-STATIC"
        elif attr > 0:
            cat = "COMPETITION"
        else:
            cat = "WALLED-STATIC"
        det = (f"iters={iters} frontier {attr}/{tot} attributed, "
               f"{static} static; top={blockers[:2]} {decl}")
    else:
        cat = "EXHAUSTED" if iters > 100000 else "OTHER"
        det = f"iters={iters} {decl}"
    final[net] = (cat, det)

cats = defaultdict(list)
for net, (cat, det) in final.items():
    cats[cat].append((net, det))
for cat in sorted(cats):
    print(f"\n== {cat} ({len(cats[cat])} nets never recovered in main loop)")
    for net, det in sorted(cats[cat]):
        print(f"  {net:<22} x{attempts[net]:<3} {det}")
