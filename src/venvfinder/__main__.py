"""Allow `python -m venvfinder`."""

import sys

from venvfinder.app import main

if __name__ == "__main__":
    sys.exit(main())
