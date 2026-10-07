"""Two replicas of the MCP servers must agree on runtime state (the bug: each replica kept its own memory)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest
from mcp import Client

from bank_assistant.core.bank_data import BusinessRuleError, CardReissueReason, CoreBankingStore, DisputeReason
from bank_assistant.core.bank_state import RedisBankState
from bank_assistant.core.context_token import issue_context_token
from bank_assistant.mcp_servers.service_server import build_service_server
from bank_assistant.mcp_servers.transactions_server import build_transactions_server
from tests.conftest import TODAY

SECRET = "s" * 40


def replicas(n: int = 2) -> list[CoreBankingStore]:
    server = fakeredis.FakeServer()
    return [CoreBankingStore(today=TODAY, state=RedisBankState(fakeredis.FakeRedis(server=server))) for _ in range(n)]


def debit(store: CoreBankingStore) -> str:
    return next(t.transaction_id for t in store.transactions.values()
                if t.account_ref == "acc_c001_cur" and t.direction.value == "debit")


def test_dispute_on_one_replica_is_seen_by_the_other() -> None:
    a, b = replicas()
    tx = debit(a)
    a.raise_dispute("C001", tx, DisputeReason.DUPLICATE, "", execute=True)
    assert b.is_disputed(tx) and b.disputed == {tx}
    with pytest.raises(BusinessRuleError, match="already has an open dispute"):
        b.raise_dispute("C001", tx, DisputeReason.DUPLICATE, "", execute=True)
    assert [r.kind for r in b.service_requests("C001")] == ["dispute"]


def test_simultaneous_confirmations_only_one_wins() -> None:
    stores = replicas(8)
    tx = debit(stores[0])

    def confirm(store: CoreBankingStore) -> str:
        try:
            return store.raise_dispute("C001", tx, DisputeReason.DUPLICATE, "", execute=True).reference
        except BusinessRuleError:
            return "refused"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(confirm, stores))
    assert sum(r != "refused" for r in results) == 1


def test_card_replacement_and_cheque_book_are_shared() -> None:
    a, b = replicas()
    a.reissue_card("C001", "card_c001_1", CardReissueReason.LOST, execute=True)
    assert b.card_status(b.cards["card_c001_1"]) == "replaced"
    with pytest.raises(BusinessRuleError, match="already been replaced"):
        b.reissue_card("C001", "card_c001_1", CardReissueReason.LOST, execute=True)
    a.request_cheque_book("C001", None, execute=True)
    with pytest.raises(BusinessRuleError, match="already on its way"):
        b.request_cheque_book("C001", None, execute=False)


def test_reference_numbers_are_unique_across_replicas() -> None:
    a, b = replicas()
    refs = [s.request_statement("C001", None, TODAY.replace(day=1), TODAY, "post", execute=True).reference
            for s in (a, b, a, b)]
    assert len(set(refs)) == 4


async def test_mcp_servers_on_different_replicas_agree() -> None:
    a, b = replicas()
    token = issue_context_token("C001", "sess", SECRET)
    tx = debit(a)
    async with Client(build_transactions_server(a, SECRET)) as ca, Client(build_transactions_server(b, SECRET)) as cb:
        ok = await ca.call_tool("raise_dispute", {"context_token": token, "transaction_id": tx,
                                                  "reason": "duplicate", "dry_run": False})
        assert not ok.is_error
        seen = await cb.call_tool("get_transaction", {"context_token": token, "transaction_id": tx})
        assert seen.structured_content["disputed"] is True
        status = await cb.call_tool("get_dispute_status", {"context_token": token})
        assert len(status.structured_content["disputes"]) == 1
    async with Client(build_service_server(a, SECRET)) as sa, Client(build_service_server(b, SECRET)) as sb:
        await sa.call_tool("reissue_card", {"context_token": token, "card_ref": "card_c001_1", "reason": "stolen",
                                            "dry_run": False})
        cards = await sb.call_tool("list_cards", {"context_token": token})
        assert cards.structured_content["cards"][0]["status"] == "replaced"
