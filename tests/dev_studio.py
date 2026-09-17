#!/usr/bin/env python3
"""Run the Studio locally with the cloud DORMANT, on a spare port.

The owner's machine has real Supabase settings, so a plain
`python3 studio.py` puts the login gate in front of every page — which
makes the interface impossible to look at while working on it. This
turns the gate off IN MEMORY: nothing on disk is touched, and the
owner's aethron_config.json is left exactly as it is.

Port 8901 by default so it never fights the installed app on 8899.

    python3 tests/dev_studio.py [port]

It lives here rather than in a scratchpad because /private/tmp is wiped
on reboot and has already eaten three pieces of this project's work.
"""
import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import aethron_cloud as cloud
cloud.REAL = cloud.DRY = cloud.ENABLED = False
cloud.SUPABASE_URL = cloud.ANON_KEY = ""

port = sys.argv[1] if len(sys.argv) > 1 else "8901"
sys.argv = ["studio.py", port]
runpy.run_path(str(REPO / "studio.py"), run_name="__main__")
