"""Shared pytest configuration: make src/ importable for all test modules."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
