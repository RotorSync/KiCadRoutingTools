import re, sys
path = sys.argv[1]
events = []
with open(path, encoding='utf-8', errors='replace') as f:
    for line in f:
        if 'GRID_TRACE' not in line:
            continue
        m = re.search(r'GRID_TRACE it=(\d+) best_h=(\d+) initial_h=(\d+) tranches=(\d+) reason=(\S+)', line)
        if m:
            events.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)))

searches = []
cur = []
for e in events:
    if cur and e[3] < cur[-1][3]:
        searches.append(cur)
        cur = []
    cur.append(e)
if cur:
    searches.append(cur)

print(f"total events {len(events)}, searches {len(searches)}")
print("=== grant sequences (multi-tranche searches) ===")
for si, s in enumerate(searches):
    grants = [e for e in s if e[4] == 'grant']
    if len(grants) < 2:
        continue
    ih = grants[0][2]
    seq = " -> ".join(f"{e[1]}({e[1]/ih*100:.1f}%)" for e in grants)
    print(f"search {si}: init={ih} [{seq}] end={s[-1][4]}@{s[-1][3]}")
