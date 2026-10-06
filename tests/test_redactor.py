from __future__ import annotations

import pytest

from bank_assistant.security.redactor import PiiType, PiiVault, Redactor, iban_valid, luhn_valid

R = Redactor()


def kinds(text: str, names: tuple[str, ...] = ()) -> list[PiiType]:
    return [f.type for f in R.find(text, names)]


@pytest.mark.parametrize(("text", "expected"), [
    ("my NI number is AB 12 34 56 C", PiiType.NI_NUMBER),
    ("NI: JG103759A", PiiType.NI_NUMBER),
    ("sort code 20-00-00", PiiType.SORT_CODE),
    ("sort code: 200000", PiiType.SORT_CODE),
    ("account 41234567", PiiType.ACCOUNT_NUMBER),
    ("card 4111 1111 1111 1111", PiiType.CARD_NUMBER),
    ("card 5555-5555-5555-4444", PiiType.CARD_NUMBER),
    ("IBAN GB82 WEST 1234 5698 7654 32", PiiType.IBAN),
    ("email me at someone@example.co.uk", PiiType.EMAIL),
    ("mobile 07700 900123", PiiType.UK_PHONE),
    ("office +44 20 7946 0958", PiiType.UK_PHONE),
    ("postcode SW1A 1AA", PiiType.POSTCODE),
    ("I live at 22 King Street", PiiType.ADDRESS),
    ("my name is John Smith", PiiType.PERSON_NAME),
    ("ask Mrs Patel", PiiType.PERSON_NAME),
])
def test_detects_uk_identifiers(text: str, expected: PiiType) -> None:
    assert expected in kinds(text)


@pytest.mark.parametrize("text", [
    "card 4111 1111 1111 1112",  # fails Luhn
    "NI QQ123456C",  # Q is never a valid first letter (HMRC's example number)
    "NI GB123456A",  # never-issued prefix
    "I spent £41.50 on 2026-09-01 at Tesco",
    "what is my balance",
])
def test_ignores_non_pii(text: str) -> None:
    assert kinds(text) == []


def test_known_names_from_identity_token_are_redacted() -> None:
    assert kinds("Hi, it's amina here", ("Amina", "Yusuf")) == [PiiType.PERSON_NAME]


def test_card_wins_over_overlapping_account_number() -> None:
    assert kinds("4111111111111111") == [PiiType.CARD_NUMBER]


def test_checksums() -> None:
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")
    assert iban_valid("GB82 WEST 1234 5698 7654 32") and not iban_valid("GB82 WEST 1234 5698 7654 33")


def test_vault_is_stable_and_reversible() -> None:
    vault = PiiVault()
    text = "Pay from 41234567 to 52345678, then check 41234567 again"
    redacted = R.redact(text, vault)
    assert redacted == ("Pay from [ACCOUNT_NUMBER_1] to [ACCOUNT_NUMBER_2], then check "
                        "[ACCOUNT_NUMBER_1] again")
    assert vault.reveal(redacted) == text
    reloaded = PiiVault(vault.new_entries)
    assert R.redact("41234567", reloaded) == "[ACCOUNT_NUMBER_1]"
    assert reloaded.new_entries == {}


def test_reveal_json_only_swaps_known_placeholders() -> None:
    vault = PiiVault({"[POSTCODE_1]": "SW1A 1AA"})
    assert vault.reveal_json({"a": ["[POSTCODE_1]", "[POSTCODE_9]"], "n": 3}) == {"a": ["SW1A 1AA", "[POSTCODE_9]"],
                                                                                  "n": 3}


def test_customer_rendering_masks_financial_identifiers() -> None:
    vault = PiiVault({"[ACCOUNT_NUMBER_1]": "41234567", "[SORT_CODE_1]": "20-00-00",
                      "[EMAIL_1]": "a@example.com", "[NI_NUMBER_1]": "AB123456C"})
    rendered = vault.render_for_customer("[ACCOUNT_NUMBER_1] [SORT_CODE_1] [EMAIL_1] [NI_NUMBER_1] [EMAIL_7]")
    assert rendered == "****4567 **-**-00 a@example.com your National Insurance number [redacted]"


def test_redact_json_walks_nested_structures() -> None:
    vault = PiiVault()
    out = R.redact_json({"rows": [{"note": "call 07700 900123"}], "amount": "10.00"}, vault)
    assert out == {"rows": [{"note": "call [UK_PHONE_1]"}], "amount": "10.00"}
