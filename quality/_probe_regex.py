import re
REGULATOR_HINT = re.compile(r'(lmd+|tpsd+|apd+|mpd+|buck|boost|switcher|regulator|ldo)', re.I)
for s in ['AP63203WU', 'AP63203WU Package_TO_SOT_SMD:TSOT-23-6', 'LM5175RHF', 'TPS54560BDDA']:
    print(repr(s), bool(REGULATOR_HINT.search(s)))
