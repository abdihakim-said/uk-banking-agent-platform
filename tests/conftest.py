from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date

import pytest
import structlog

from bank_assistant.core.bank_data import CoreBankingStore
from bank_assistant.testing import Stack, build_stack

TODAY = date(2026, 10, 6)

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
logging.getLogger("httpx").setLevel(logging.WARNING)


@pytest.fixture
async def stack() -> AsyncIterator[Stack]:
    async with build_stack(today=TODAY) as s:
        yield s


@pytest.fixture
def bank() -> CoreBankingStore:
    return CoreBankingStore(today=TODAY)
