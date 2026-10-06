"""service-mcp-server: cards, cheque books and KYC (contact detail) update requests."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from bank_assistant.core.bank_data import CardReissueReason, CoreBankingStore, KycField
from bank_assistant.mcp_servers.common import (
    customer_from,
    domain_errors,
    request_result,
    secret,
    serve,
    shared_store,
)


def build_service_server(store: CoreBankingStore, token_secret: str) -> MCPServer:
    server = MCPServer(
        "service-mcp-server",
        instructions="Operational requests. Every state-changing tool needs customer confirmation.",
    )

    @server.tool(description="List the customer's cards (last 4 digits, status, expiry).")
    @domain_errors
    def list_cards(context_token: str) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return {"cards": [{"card_ref": c.card_ref, "last4": c.last4, "status": c.status, "expiry": c.expiry}
                          for c in store.cards.values() if c.customer_id == cid]}

    @server.tool(description=(
        "Order a replacement card. reason: lost, stolen, damaged or not_received. Lost or stolen cards are "
        "blocked immediately. Needs the customer's confirmation."
    ))
    @domain_errors
    def reissue_card(context_token: str, card_ref: str, reason: CardReissueReason,
                     dry_run: bool = True) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return request_result(store.reissue_card(cid, card_ref, reason, execute=not dry_run))

    @server.tool(description=(
        "Order a cheque book for a current account (omit account_ref for the main current account). "
        "Needs the customer's confirmation."
    ))
    @domain_errors
    def request_cheque_book(context_token: str, account_ref: str | None = None,
                            dry_run: bool = True) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return request_result(store.request_cheque_book(cid, account_ref, execute=not dry_run))

    @server.tool(description=(
        "Start a change to contact details held for KYC: field is address, email or phone. The change only "
        "takes effect after verification (code or link to the new detail). Needs the customer's confirmation."
    ))
    @domain_errors
    def submit_kyc_update(context_token: str, field: KycField, new_value: str,
                          dry_run: bool = True) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return request_result(store.submit_kyc_update(cid, field, new_value, execute=not dry_run))

    @server.tool(description="Status of the customer's card, cheque book and KYC service requests.")
    @domain_errors
    def get_service_request_status(context_token: str) -> dict[str, Any]:
        cid = customer_from(context_token, token_secret)
        return {"requests": [{"reference": r.reference, "kind": r.kind, "status": r.status,
                              "created_at": r.created_at.isoformat()}
                             for r in store.service_requests(cid) if r.kind != "dispute"]}

    return server


def main() -> None:
    serve(build_service_server(shared_store(), secret()))


if __name__ == "__main__":
    main()
