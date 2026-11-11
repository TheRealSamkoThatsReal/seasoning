"""PyInstaller entry point for the Windows build.

The exe is built without a console, so there is no stdout: send output to
SEASONING_LOG if set (CI uses this for the selftest report), else discard it.
"""
import os
import sys

log = os.environ.get("SEASONING_LOG")
if log:
    sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
elif sys.stdout is None:
    sys.stdout = sys.stderr = open(os.devnull, "w")

from seasoning.__main__ import main  # noqa: E402

sys.exit(main())
