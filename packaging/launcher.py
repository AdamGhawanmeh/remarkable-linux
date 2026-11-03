"""Entry point of the standalone app (dist/remarkable-linux/remarkable-linux): runs the
bundled copy of client/run.py.

The bundle keeps client/ (run.py, src/ with the assets, and the demo library) as plain
files under _internal/client/, laid out as in the repository, because the client finds
its assets next to its own source files (see remarkable-linux.spec).
"""

import os
import runpy
import sys

CLIENT = os.path.join(sys._MEIPASS, "client")
sys.argv[0] = os.path.join(CLIENT, "run.py")
runpy.run_path(sys.argv[0], run_name="__main__")
