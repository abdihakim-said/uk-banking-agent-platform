"""transactions-mcp-server: transaction search, spending analysis and disputed debits."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from bank_assistant.core.bank_data import CoreBankingStore, Direction, DisputeReason, Transaction
from bank_assistant.mcp_servers.common import (
    customer_from,
    domain_errors,
    http_options,
    request_result,
    secret,
    shared_store,
)


def _tx(t: Transaction) -> dict[str, Any]:
    return {"transaction_id": t.transaction_id, "date": t.booked_at.isoformat(), "merchant": t.merchant,
            "description": t.description, "category": t.category, "amount": str(t.amount),
            "direction": t.direction.value, "currency": "GBP"}


def build_transactions_server(store: CoreBankingStore, token_secret: str) -> MCPServer:
    server = MCPServer(
        "transactions-mcp-server",
        instructions="Transaction history and spending analysis; disputes need customer confirmation.",
    )

    @server.tool(description=(
        "Search the customer's transactions, newest first. All filters are optional: merchant (substring), "
        "start_date/end_date (ISO), min_amount/max_amount (GBP), direction ('debit' or 'credit'), limit (max 50). "
        "Omit account_ref to search the main current account."
    ))
    @domain_errors
    def search_transactions(context_token: str, account_ref: str | None = None, merchant: str | None = None,
                            start_date: date | None = None, end_date: date | None = None,
                            min_amount: Decimal | None = None, max_amount: Decimal | None = None,
                            direction: Literal["debit", "credit"] | None = None,
                            limit: int = 20) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        found = store.search_transactions(cid, account_ref, merchant, start_date, end_date, min_amount, max_amount,
                                          Direction(direction) if direction else None, limit)
        return {"count": len(found), "transactions": [_tx(t) for t in found]}

    @server.tool(description="Total debit spending by category for one month (month as YYYY-MM).")
    @domain_errors
    def spending_summary(context_token: str, month: str, account_ref: str | None = None) -> dict[str, Any]:
        totals = store.spending_summary(customer_from(context_token, token_secret), account_ref, month)
        return {"month": month, "currency": "GBP", "total": str(sum(totals.values(), Decimal("0"))),
                "by_category": {k: str(v) for k, v in totals.items()}}

    @server.tool(description="Full details of one transaction by transaction_id.")
    @domain_errors
    def get_transaction(context_token: str, transaction_id: str) -> dict[str, Any]:
        tx = store.transaction(customer_from(context_token, token_secret), transaction_id)
        return {**_tx(tx), "disputed": transaction_id in store.disputed}

    @server.tool(description=(
        "Raise a dispute on a debit the customer doesn't recognise or disagrees with. reason is one of: "
        "not_recognised, duplicate, goods_not_received, incorrect_amount, cancelled_subscription. "
        "Needs the customer's confirmation."
    ))
    @domain_errors
    def raise_dispute(context_token: str, transaction_id: str, reason: DisputeReason, description: str = "",
                      dry_run: bool = True) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        result = request_result(store.raise_dispute(cid, transaction_id, reason, description, execute=not dry_run))
        if reason is DisputeReason.NOT_RECOGNISED:
            result["advice"] = "If you think your card details were stolen, we'll also need to replace your card."
        return result

    @server.tool(description="Status of the customer's disputes and other service requests.")
    @domain_errors
    def get_dispute_status(context_token: str) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return {"disputes": [{"reference": r.reference, "status": r.status, "details": r.details}
                             for r in store.service_requests(cid) if r.kind == "dispute"]}

    return server


def main() -> None:
    build_transactions_server(shared_store(), secret()).run("streamable-http", **http_options())


if __name__ == "__main__":
    main()
