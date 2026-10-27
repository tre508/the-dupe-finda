import sys
sys.path.insert(0, ".")
from vdf import fingerprint, config

print(config.DURATION_TOLERANCE_ABS)
print(config.DURATION_TOLERANCE_PCT)
print(fingerprint.durations_compatible(1238, 428))
