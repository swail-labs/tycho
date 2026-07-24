"""Run Tycho as a module: `python -m tycho`.

Mirrors the `tycho` console script (`tycho.cli:main`). Also the freeze entry point for
standalone binaries (TYCHO-69) — PyInstaller/zipapp target the package, no wrapper script.
"""

import sys

from tycho.cli import main

if __name__ == "__main__":
    sys.exit(main())
