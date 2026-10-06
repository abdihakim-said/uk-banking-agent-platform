"""Browser test against the running stack (``make up``). Uses the offline model unless ANTHROPIC_API_KEY is set
for the agent, so assertions check behaviour and data rather than exact wording."""

from __future__ import annotations

import os
import re

from playwright.sync_api import Locator, Page, expect

BASE_URL = os.environ.get("WEB_URL", "http://localhost:3080")


def sign_in(page: Page, persona: str) -> None:
    page.goto(BASE_URL)
    page.get_by_test_id(f"persona-{persona}").click()
    expect(page.locator(".msg.system").filter(has_text=re.compile(r"Signed in as \w+\."))).to_be_visible()


def ask(page: Page, text: str) -> None:
    before = page.get_by_test_id("bot-message").count()
    page.get_by_label("Message").fill(text)
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_test_id("bot-message")).to_have_count(before + 1, timeout=60_000)


def last_reply(page: Page) -> Locator:
    return page.get_by_test_id("bot-message").last


def test_balance_with_pii_hidden_from_ai(page: Page) -> None:
    sign_in(page, "amina")
    ask(page, "My account number is 41234567. What's my balance?")
    expect(page.get_by_test_id("pii-badge").last).to_contain_text("account number")
    expect(last_reply(page)).to_contain_text("1842.37")
    expect(last_reply(page)).not_to_contain_text("41234567")


def test_two_stage_confirmation(page: Page) -> None:
    # A statement request can be repeated, so the test stays valid against a long-running stack
    # (a card can only be replaced once).
    sign_in(page, "amina")
    ask(page, "Can you send me last month's statement?")
    expect(last_reply(page)).to_contain_text("Reply YES")
    before = page.get_by_test_id("bot-message").count()
    last_reply(page).get_by_test_id("confirm-yes").click()
    expect(page.get_by_test_id("bot-message")).to_have_count(before + 1, timeout=60_000)
    expect(last_reply(page)).to_contain_text("submitted")


def test_handoff_and_customer_isolation(page: Page) -> None:
    sign_in(page, "daniel")
    ask(page, "I want to make a complaint")
    expect(page.get_by_test_id("handoff-banner")).to_be_visible()
    ask(page, "What's my balance?")
    expect(last_reply(page)).to_contain_text("312.04")
    expect(last_reply(page)).not_to_contain_text("1842.37")
