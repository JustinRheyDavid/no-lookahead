"""Makes ``pytest`` work from a clean checkout, and defines the network policy.

The tests import ``src.edgar``, which needs the repository root on
``sys.path``. ``python -m pytest`` puts it there and bare ``pytest`` does not,
so without this file the suite passes one way and fails the other — the worst
outcome for someone running it for the first time. Its mere presence at the
root fixes that: pytest adds the directory containing ``conftest.py`` to
``sys.path``.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: makes a real request to sec.gov. Excluded from CI, which "
        "must pass on a fresh clone with no configuration and no traffic sent "
        'to a public agency on every push. Run locally with "pytest -m network" '
        "after setting EDGAR_USER_AGENT.",
    )
    config.addinivalue_line(
        "markers",
        "slow: measures real elapsed time or processes the full corpus.",
    )
