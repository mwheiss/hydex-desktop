#!/usr/bin/env python3
"""Explicit cache entry point; the original native Node bridge remains upstream."""
import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_api_cache.entrypoint import main
main()
