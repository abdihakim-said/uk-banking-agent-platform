from __future__ import annotations

from typing import Any

import pytest
from mcp import Client

from bank_assistant.core.bank_data import CoreBankingStore
from bank_assistant.core.context_token import InvalidContextTokenError, issue_context_token, verify_context_token
from bank_assistant.mcp_servers.accounts_server import build_accounts_server
from bank_assistant.mcp_servers.service_server import build_service_server
from bank_assistant.mcp_servers.transactions_server import build_transactions_server

SECRET = "s" * 40


def token(cid: str = "C001", **kw: Any) -> str:
    return issue_context_token(cid, "sess-1", SECRET, **kw)


def test_context_token_round_trip_and_tamper() -> None:
    tok = token()
    assert verify_context_token(tok, SECRET).customer_id == "C001"
    payload, sig = tok.split(".")
    with pytest.raises(InvalidContextTokenError):
        verify_context_token(f"{payload}.{sig[:-2]}AA", SECRET)
    with pytest.raises(InvalidContextTokenError):
        verify_context_token(tok, "another-secret" * 3)
    with pytest.raises(InvalidContextTokenError):
        verify_context_token(token(ttl_seconds=10, now=1000), SECRET, now=2000)


async def test_balance_uses_customer_from_token_only(bank: CoreBankingStore) -> None:
    async with Client(build_accounts_server(bank, SECRET)) as c:
        mine = await c.call_tool("get_balance", {"context_token": token()})
        assert mine.structured_content["balance"] == "1842.37"
        # Another customer's account ref is "not found", exactly like a ref that doesn't exist.
        theirs = await c.call_tool("get_balance", {"context_token": token(), "account_ref": "acc_c002_cur"})
        missing = await c.call_tool("get_balance", {"context_token": token(), "account_ref": "acc_nope"})
        assert theirs.is_error and missing.is_error
        assert theirs.content[0].text == missing.content[0].text


async def test_invalid_token_is_rejected(bank: CoreBankingStore) -> None:
    async with Client(build_accounts_server(bank, SECRET)) as c:
        result = await c.call_tool("list_accounts", {"context_token": "forged.token"})
        assert result.is_error and "not authorised" in result.content[0].text


async def test_write_tools_default_to_dry_run(bank: CoreBankingStore) -> None:
    async with Client(build_service_server(bank, SECRET)) as c:
        preview = await c.call_tool("reissue_card", {"context_token": token(), "card_ref": "card_c001_1",
                                                     "reason": "lost"})
        assert preview.structured_content["status"] == "preview"
        assert bank.cards["card_c001_1"].status == "active"
        done = await c.call_tool("reissue_card", {"context_token": token(), "card_ref": "card_c001_1",
                                                  "reason": "lost", "dry_run": False})
        assert done.structured_content["status"] == "submitted"
        assert bank.cards["card_c001_1"].status == "replaced"
        again = await c.call_tool("reissue_card", {"context_token": token(), "card_ref": "card_c001_1",
                                                   "reason": "lost", "dry_run": False})
        assert again.is_error and "already been replaced" in again.content[0].text


async def test_dispute_rules(bank: CoreBankingStore) -> None:
    credit = next(t for t in bank.transactions.values()
                  if t.account_ref == "acc_c001_cur" and t.direction.value == "credit")
    debit = next(t for t in bank.transactions.values()
                 if t.account_ref == "acc_c001_cur" and t.direction.value == "debit")
    other = next(t for t in bank.transactions.values() if t.account_ref == "acc_c002_cur")
    async with Client(build_transactions_server(bank, SECRET)) as c:
        r = await c.call_tool("raise_dispute", {"context_token": token(), "transaction_id": credit.transaction_id,
                                                "reason": "not_recognised"})
        assert r.is_error and "only debits" in r.content[0].text
        r = await c.call_tool("raise_dispute", {"context_token": token(), "transaction_id": other.transaction_id,
                                                "reason": "not_recognised"})
        assert r.is_error and "not found" in r.content[0].text
        args = {"context_token": token(), "transaction_id": debit.transaction_id, "reason": "duplicate",
                "dry_run": False}
        assert not (await c.call_tool("raise_dispute", args)).is_error
        r = await c.call_tool("raise_dispute", args)
        assert r.is_error and "already has an open dispute" in r.content[0].text


async def test_search_and_spending(bank: CoreBankingStore) -> None:
    async with Client(build_transactions_server(bank, SECRET)) as c:
        r = await c.call_tool("search_transactions", {"context_token": token(), "merchant": "netflix", "limit": 3})
        txs = r.structured_content["transactions"]
        assert 0 < len(txs) <= 3 and all(t["merchant"] == "Netflix" for t in txs)
        assert [t["date"] for t in txs] == sorted((t["date"] for t in txs), reverse=True)
        s = await c.call_tool("spending_summary", {"context_token": token(), "month": "2026-09"})
        assert s.structured_content["total"] == str(sum(
            (t.amount for t in bank.transactions.values() if t.account_ref == "acc_c001_cur"
             and t.direction.value == "debit" and t.booked_at.strftime("%Y-%m") == "2026-09"), start=0))
        bad = await c.call_tool("spending_summary", {"context_token": token(), "month": "September"})
        assert bad.is_error


async def test_cheque_book_only_for_current_accounts(bank: CoreBankingStore) -> None:
    async with Client(build_service_server(bank, SECRET)) as c:
        r = await c.call_tool("request_cheque_book", {"context_token": token(), "account_ref": "acc_c001_sav"})
        assert r.is_error and "current accounts" in r.content[0].text
