import sys
sys.path.insert(0, ".")
from vdf import fingerprint

d1 = 1238
d2 = 428
print(f"compatible({d1}, {d2}) =", fingerprint.durations_compatible(d1, d2))

