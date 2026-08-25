import re
REGULATOR_HINT = re.compile(r'(lmd+|tpsd+|apd+|mpd+|buck|boost|switcher|regulator|ldo)', re.I)
m = REGULATOR_HINT.search('AP63203WU')
print('match:', m)
print('groups:', m.groups() if m else None)
print('span:', m.span() if m else None)
# try each alternative
for alt in ['lm', 'tps', 'ap', 'mp', 'buck', 'boost', 'switcher', 'regulator', 'ldo']:
    r = re.compile(alt + r'd+' if alt in ('lm','tps','ap','mp') else alt, re.I)
    print(alt, bool(r.search('AP63203WU')))
