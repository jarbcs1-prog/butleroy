"""`python -m assistant` - the command that starts the assistant.

This module exists because it did not, and that absence is why the tray, the
chord and the prompt window were unreachable: each was complete and tested, and
none was imported by anything that ran. See `launcher.py` for the ordering and
the reasoning.
"""

from __future__ import annotations

import sys

from .launcher import main

if __name__ == "__main__":
    sys.exit(main())
