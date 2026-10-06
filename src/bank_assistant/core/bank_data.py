"""A deterministic, in-memory stand-in for a core banking system.

Every method takes the customer id from a verified context token and enforces ownership itself.
A record that exists but belongs to someone else is reported exactly like one that doesn't exist,
so the API never confirms another customer's account or transaction ids.

All data is synthetic. Real deployments replace this class with adapters to the core banking platform.
"""

from __future__ import annotations

import itertools
import random
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class NotFoundError(Exception):
    """Unknown record, or a record the customer doesn't own (deliberately indistinguishable)."""


class BusinessRuleError(Exception):
    """The request is understood but not allowed (e.g. disputing a credit)."""


class AccountType(StrEnum):
    CURRENT = "current"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class DisputeReason(StrEnum):
    NOT_RECOGNISED = "not_recognised"
    DUPLICATE = "duplicate"
    GOODS_NOT_RECEIVED = "goods_not_received"
    INCORRECT_AMOUNT = "incorrect_amount"
    CANCELLED_SUBSCRIPTION = "cancelled_subscription"


class CardReissueReason(StrEnum):
    LOST = "lost"
    STOLEN = "stolen"
    DAMAGED = "damaged"
    NOT_RECEIVED = "not_received"


class KycField(StrEnum):
    ADDRESS = "address"
    EMAIL = "email"
    PHONE = "phone"


class Customer(BaseModel):
    customer_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    address: str
    vulnerable_flag: bool = False


class Account(BaseModel):
    account_ref: str
    customer_id: str
    type: AccountType
    name: str
    sort_code: str
    account_number: str
    balance: Decimal
    available: Decimal
    overdraft_limit: Decimal = Decimal("0")

    @property
    def masked(self) -> str:
        return f"****{self.account_number[-4:]}"


class Transaction(BaseModel):
    transaction_id: str
    account_ref: str
    booked_at: date
    description: str
    merchant: str
    category: str
    amount: Decimal  # always positive; direction says which way
    direction: Direction


class Card(BaseModel):
    card_ref: str
    customer_id: str
    account_ref: str
    last4: str
    status: str = "active"
    expiry: str


class ServiceRequest(BaseModel):
    reference: str
    customer_id: str
    kind: str
    status: str
    created_at: datetime
    details: dict[str, str] = Field(default_factory=dict)


_MERCHANTS: list[tuple[str, str, Decimal, Decimal]] = [
    ("Tesco", "groceries", Decimal("8"), Decimal("95")),
    ("Sainsbury's", "groceries", Decimal("6"), Decimal("80")),
    ("TfL Travel", "transport", Decimal("2.80"), Decimal("12")),
    ("Pret A Manger", "eating_out", Decimal("3.50"), Decimal("14")),
    ("Amazon UK", "shopping", Decimal("9.99"), Decimal("120")),
    ("Netflix", "subscriptions", Decimal("10.99"), Decimal("10.99")),
    ("Spotify", "subscriptions", Decimal("11.99"), Decimal("11.99")),
    ("Thames Water", "bills", Decimal("38"), Decimal("38")),
    ("British Gas", "bills", Decimal("74"), Decimal("74")),
    ("Boots", "health", Decimal("4"), Decimal("40")),
]


class CoreBankingStore:
    """Synthetic data for two customers, generated from a fixed seed relative to ``today``."""

    def __init__(self, today: date | None = None, seed: int = 7) -> None:
        self.today = today or datetime.now(UTC).date()
        self._rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
        self._refs = itertools.count(100001)
        self.customers: dict[str, Customer] = {}
        self.accounts: dict[str, Account] = {}
        self.transactions: dict[str, Transaction] = {}
        self.cards: dict[str, Card] = {}
        self.requests: dict[str, ServiceRequest] = {}
        self.disputed: set[str] = set()
        self._seed()

    # ---- seed -------------------------------------------------------------------------------
    def _seed(self) -> None:
        self.customers = {
            "C001": Customer(
                customer_id="C001", first_name="Amina", last_name="Yusuf", email="amina.yusuf@example.com",
                phone="07700 900123", address="12 Example Road, London, E1 6AN",
            ),
            "C002": Customer(
                customer_id="C002", first_name="Daniel", last_name="Okafor", email="d.okafor@example.com",
                phone="07700 900456", address="4 Sample Street, Leeds, LS1 4AP", vulnerable_flag=True,
            ),
        }
        self._add_account("acc_c001_cur", "C001", AccountType.CURRENT, "Everyday Current Account",
                          "20-00-00", "41234567", Decimal("1842.37"), Decimal("500"))
        self._add_account("acc_c001_sav", "C001", AccountType.SAVINGS, "Easy Access Saver",
                          "20-00-00", "41239876", Decimal("6250.00"))
        self._add_account("acc_c002_cur", "C002", AccountType.CURRENT, "Everyday Current Account",
                          "20-00-01", "52345678", Decimal("312.04"), Decimal("250"))
        self.cards["card_c001_1"] = Card(card_ref="card_c001_1", customer_id="C001", account_ref="acc_c001_cur",
                                         last4="4821", expiry="09/28")
        self.cards["card_c002_1"] = Card(card_ref="card_c002_1", customer_id="C002", account_ref="acc_c002_cur",
                                         last4="7730", expiry="03/27")
        for account_ref in ("acc_c001_cur", "acc_c002_cur"):
            self._seed_transactions(account_ref)
        # One salary credit per current account so not everything is a debit.
        for account_ref, amount in (("acc_c001_cur", Decimal("2650.00")), ("acc_c002_cur", Decimal("1480.00"))):
            self._add_tx(account_ref, self.today - timedelta(days=5), "SALARY ACME LTD", "Acme Ltd",
                         "income", amount, Direction.CREDIT)

    def _add_account(self, ref: str, cid: str, kind: AccountType, name: str, sort_code: str, number: str,
                     balance: Decimal, overdraft: Decimal = Decimal("0")) -> None:
        self.accounts[ref] = Account(account_ref=ref, customer_id=cid, type=kind, name=name, sort_code=sort_code,
                                     account_number=number, balance=balance, available=balance + overdraft,
                                     overdraft_limit=overdraft)

    def _seed_transactions(self, account_ref: str) -> None:
        for day in range(1, 90):
            for _ in range(self._rng.choice([0, 1, 1, 2])):
                merchant, category, low, high = self._rng.choice(_MERCHANTS)
                cents = self._rng.randint(int(low * 100), int(high * 100))
                self._add_tx(account_ref, self.today - timedelta(days=day), f"CARD PAYMENT {merchant.upper()}",
                             merchant, category, Decimal(cents) / 100, Direction.DEBIT)

    def _add_tx(self, account_ref: str, booked: date, desc: str, merchant: str, category: str,
                amount: Decimal, direction: Direction) -> None:
        tx_id = f"tx_{account_ref[4:8]}_{len(self.transactions) + 1:05d}"
        self.transactions[tx_id] = Transaction(transaction_id=tx_id, account_ref=account_ref, booked_at=booked,
                                               description=desc, merchant=merchant, category=category,
                                               amount=amount.quantize(Decimal("0.01")), direction=direction)

    def _new_ref(self, prefix: str) -> str:
        return f"{prefix}-{next(self._refs)}"

    # ---- ownership helpers -----------------------------------------------------------------
    def customer(self, customer_id: str) -> Customer:
        try:
            return self.customers[customer_id]
        except KeyError as exc:
            raise NotFoundError("customer not found") from exc

    def accounts_for(self, customer_id: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.customer_id == customer_id]

    def account(self, customer_id: str, account_ref: str | None) -> Account:
        """Return an owned account. With no ref, return the customer's primary current account."""
        if account_ref is None:
            owned = [a for a in self.accounts_for(customer_id) if a.type is AccountType.CURRENT]
            if not owned:
                raise NotFoundError("account not found")
            return owned[0]
        account = self.accounts.get(account_ref)
        if account is None or account.customer_id != customer_id:
            raise NotFoundError("account not found")
        return account

    def transaction(self, customer_id: str, transaction_id: str) -> Transaction:
        tx = self.transactions.get(transaction_id)
        if tx is None or self.accounts[tx.account_ref].customer_id != customer_id:
            raise NotFoundError("transaction not found")
        return tx

    def card(self, customer_id: str, card_ref: str) -> Card:
        card = self.cards.get(card_ref)
        if card is None or card.customer_id != customer_id:
            raise NotFoundError("card not found")
        return card

    # ---- accounts ---------------------------------------------------------------------------
    def request_statement(self, customer_id: str, account_ref: str | None, start: date, end: date,
                          delivery: str, execute: bool) -> ServiceRequest:
        account = self.account(customer_id, account_ref)
        if end < start:
            raise BusinessRuleError("end date is before start date")
        if end > self.today:
            raise BusinessRuleError("end date is in the future")
        if (self.today - start).days > 7 * 365:
            raise BusinessRuleError("statements are available for the last 7 years")
        return self._service_request(customer_id, "statement", execute, {
            "account": f"{account.name} {account.masked}", "from": start.isoformat(), "to": end.isoformat(),
            "delivery": delivery,
        })

    # ---- transactions -----------------------------------------------------------------------
    def search_transactions(self, customer_id: str, account_ref: str | None = None, merchant: str | None = None,
                            start: date | None = None, end: date | None = None,
                            min_amount: Decimal | None = None, max_amount: Decimal | None = None,
                            direction: Direction | None = None, limit: int = 20) -> list[Transaction]:
        account = self.account(customer_id, account_ref)
        needle = merchant.lower() if merchant else None
        results = [
            t for t in self.transactions.values()
            if t.account_ref == account.account_ref
            and (needle is None or needle in t.merchant.lower() or needle in t.description.lower())
            and (start is None or t.booked_at >= start)
            and (end is None or t.booked_at <= end)
            and (min_amount is None or t.amount >= min_amount)
            and (max_amount is None or t.amount <= max_amount)
            and (direction is None or t.direction is direction)
        ]
        results.sort(key=lambda t: (t.booked_at, t.transaction_id), reverse=True)
        return results[: max(1, min(limit, 50))]

    def spending_summary(self, customer_id: str, account_ref: str | None, month: str) -> dict[str, Decimal]:
        account = self.account(customer_id, account_ref)
        try:
            year, mon = (int(p) for p in month.split("-"))
            first = date(year, mon, 1)
        except ValueError as exc:
            raise BusinessRuleError("month must be YYYY-MM") from exc
        nxt = date(year + (mon == 12), mon % 12 + 1, 1)
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for t in self.transactions.values():
            if t.account_ref == account.account_ref and t.direction is Direction.DEBIT and first <= t.booked_at < nxt:
                totals[t.category] += t.amount
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    def raise_dispute(self, customer_id: str, transaction_id: str, reason: DisputeReason, description: str,
                      execute: bool) -> ServiceRequest:
        tx = self.transaction(customer_id, transaction_id)
        if tx.direction is not Direction.DEBIT:
            raise BusinessRuleError("only debits can be disputed")
        if transaction_id in self.disputed:
            raise BusinessRuleError("this transaction already has an open dispute")
        # Payment Services Regulations 2017: unauthorised transactions must be reported within 13 months.
        if (self.today - tx.booked_at).days > 396:
            raise BusinessRuleError("transactions older than 13 months can't be disputed here")
        request = self._service_request(customer_id, "dispute", execute, {
            "transaction": f"{tx.merchant} £{tx.amount} on {tx.booked_at.isoformat()}", "reason": reason.value,
            "description": description[:300],
        })
        if execute:
            self.disputed.add(transaction_id)
        return request

    # ---- service ----------------------------------------------------------------------------
    def reissue_card(self, customer_id: str, card_ref: str, reason: CardReissueReason, execute: bool) -> ServiceRequest:
        card = self.card(customer_id, card_ref)
        if card.status == "replaced":
            raise BusinessRuleError("this card has already been replaced")
        blocks_now = reason in (CardReissueReason.LOST, CardReissueReason.STOLEN)
        request = self._service_request(customer_id, "card_reissue", execute, {
            "card": f"card ending {card.last4}", "reason": reason.value,
            "old_card": "blocked immediately" if blocks_now else "keeps working until the new card is activated",
            "delivery": "3-5 working days to the address we hold",
        })
        if execute:
            card.status = "replaced"
        return request

    def request_cheque_book(self, customer_id: str, account_ref: str | None, execute: bool) -> ServiceRequest:
        account = self.account(customer_id, account_ref)
        if account.type is not AccountType.CURRENT:
            raise BusinessRuleError("cheque books are only available on current accounts")
        if any(r.customer_id == customer_id and r.kind == "cheque_book" and r.status == "submitted"
               and r.details.get("account") == f"{account.name} {account.masked}" for r in self.requests.values()):
            raise BusinessRuleError("a cheque book is already on its way for this account")
        return self._service_request(customer_id, "cheque_book", execute, {
            "account": f"{account.name} {account.masked}", "delivery": "5-7 working days to the address we hold",
        })

    def submit_kyc_update(self, customer_id: str, field: KycField, new_value: str, execute: bool) -> ServiceRequest:
        self.customer(customer_id)
        if not new_value.strip() or len(new_value) > 200:
            raise BusinessRuleError("the new value is empty or too long")
        next_step = {
            KycField.ADDRESS: "we'll post a verification code to the new address before it changes",
            KycField.EMAIL: "we'll email a verification link to the new address",
            KycField.PHONE: "we'll text a verification code to the new number",
        }[field]
        return self._service_request(customer_id, "kyc_update", execute, {
            "field": field.value, "new_value": new_value, "next_step": next_step,
        })

    def service_requests(self, customer_id: str) -> list[ServiceRequest]:
        return sorted((r for r in self.requests.values() if r.customer_id == customer_id),
                      key=lambda r: r.created_at, reverse=True)

    def _service_request(self, customer_id: str, kind: str, execute: bool, details: dict[str, str]) -> ServiceRequest:
        request = ServiceRequest(
            reference=self._new_ref(kind.upper().replace("_", "")[:6]) if execute else "PREVIEW",
            customer_id=customer_id, kind=kind, status="submitted" if execute else "preview",
            created_at=datetime.now(UTC), details=details,
        )
        if execute:
            self.requests[request.reference] = request
        return request
