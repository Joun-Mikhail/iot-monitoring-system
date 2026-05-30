"""
Shared pytest configuration.

Marks:
  integration  — requires LocalStack or real AWS; skipped by default
  aws_live     — requires real AWS credentials + deployed infrastructure; never runs in CI
  slow         — takes >5s; can be excluded with -m "not slow"
"""

import sys
import os

import pytest

# make project root importable without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires LocalStack or live AWS")
    config.addinivalue_line("markers", "aws_live: requires deployed AWS infrastructure and real credentials")
    config.addinivalue_line("markers", "slow: test takes more than a few seconds")
    config.addinivalue_line("markers", "flaky: known intermittent failures, see comment for reason")
