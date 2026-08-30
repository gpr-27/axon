#!/usr/bin/env python3
"""
Launcher script for Axon CLI.
Usage:
    python3 axon_run.py
    python3 axon_run.py -p "Your prompt"
"""
import sys
import os

# Ensure src is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from axon.cli import main

if __name__ == "__main__":
    sys.exit(main())
