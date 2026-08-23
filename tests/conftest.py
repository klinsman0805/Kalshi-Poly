"""Shared setup for the test suite.

Every test here is pure logic: no network, no exchange, no credentials, no
writes outside tmp_path. That is deliberate — these run in the deploy workflow
against a box carrying live positions, so a test must never be able to place an
order, mutate a ledger, or block on an API.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
