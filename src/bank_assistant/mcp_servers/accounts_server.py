"""accounts-mcp-server: balances, account list, profile summary and statement requests."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from bank_assistant.core.bank_data import CoreBankingStore
from bank_assistant.mcp_servers.common import (
    customer_from,
    domain_errors,
    http_options,
    request_result,
    secret,
    shared_store,
)


def build_accounts_server(store: CoreBankingStore, token_secret: str) -> MCPServer:
    server = MCPServer(
        "accounts-mcp-server",
        instructions="Read-only account information, plus statement requests that need customer confirmation.",
    )

    @server.tool(description="List the customer's accounts with masked numbers and current balances.")
    @domain_errors
    def list_accounts(context_token: str) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return {"accounts": [
            {"account_ref": a.account_ref, "name": a.name, "type": a.type.value, "masked_number": a.masked,
             "balance": str(a.balance), "currency": "GBP"}
            for a in store.accounts_for(cid)
        ]}

    @server.tool(description=(
        "Get the balance, available funds and overdraft limit for one account. "
        "Omit account_ref to use the customer's main current account."
    ))
    @domain_errors
    def get_balance(context_token: str, account_ref: str | None = None) -> dict[str, Any]:
        account = store.account(customer_from(context_token, token_secret), account_ref)
        return {"account_ref": account.account_ref, "name": account.name, "masked_number": account.masked,
                "balance": str(account.balance), "available": str(account.available),
                "overdraft_limit": str(account.overdraft_limit), "currency": "GBP",
                "as_of": store.today.isoformat()}

    @server.tool(description="Masked contact details held for the customer (for confirming what's on file).")
    @domain_errors
    def get_profile_summary(context_token: str) -> dict[str, Any]:
        c = store.customer(customer_from(context_token, token_secret))
        return {"first_name": c.first_name, "email_on_file": f"{c.email[0]}***@{c.email.split('@')[1]}",
                "phone_on_file": f"*** *** {c.phone[-3:]}", "postcode_on_file": c.address.rsplit(",", 1)[-1].strip()}

    @server.tool(description=(
        "Request a statement for a date range (ISO dates). delivery is 'post' or 'secure_message'. "
        "This creates a service request, so the customer must confirm it."
    ))
    @domain_errors
    def request_statement(context_token: str, start_date: date, end_date: date,
                          delivery: Literal["post", "secure_message"] = "secure_message",
                          account_ref: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return request_result(store.request_statement(cid, account_ref, start_date, end_date, delivery,
                                                      execute=not dry_run))

    return server


def main() -> None:
    build_accounts_server(shared_store(), secret()).run("streamable-http", **http_options())


if __name__ == "__main__":
    main()
