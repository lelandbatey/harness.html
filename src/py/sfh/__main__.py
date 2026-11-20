"""Dev entry: PYTHONPATH=src/py:vendor python3 -m sfh dist/harness.html [cmd ...]"""
import sys

from sfh.main import main

main(sys.argv[1:])
