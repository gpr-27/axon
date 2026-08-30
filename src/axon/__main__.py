"""
Entrypoint for `python3 -m axon`.
"""
import sys
from axon.cli import main

if __name__ == "__main__":
    sys.exit(main())
