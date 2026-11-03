"""Non-root container sentinel. Generated code runs under a different non-root UID.

Keep tmpfs available for collection, and bound orphan lifetime if the host dies.
The generated program cannot signal this different-UID sentinel or forge its exit.
"""

import os
import time

# Host enforces the program deadline; 15s permits output import after it finishes.
time.sleep(float(os.environ.get("ANALYSIS_TIMEOUT", "30")) + 15)
os._exit(124)
