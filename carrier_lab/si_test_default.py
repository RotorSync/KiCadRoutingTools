import sys
sys.path.insert(0, 'py_router')
import env_knobs
env_knobs.refresh()
import si_enforce
print('default SI_ADAPTIVE:', si_enforce._adaptive_enabled())
