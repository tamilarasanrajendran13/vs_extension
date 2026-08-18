"""Shared test fixtures and path setup for the datacompare test suite."""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

SAMPLE = os.path.join(REPO_ROOT, "sample_data")


@pytest.fixture
def sample_dir():
    return SAMPLE


@pytest.fixture
def repo_root():
    return REPO_ROOT
