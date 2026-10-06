"""PII detection and reversible pseudonymisation for UK banking text.

Raw identifiers are replaced with placeholders such as ``[SORT_CODE_1]`` before any text reaches an LLM.
The mapping lives in a per-session ``PiiVault`` (persisted in Redis), so:
  * the agent service can swap placeholders back to real values *inside tool calls* (the MCP servers need
    the real values; the model never sees them), and
  * the gateway can render placeholders in replies as masked values for the customer (``****4567``).

Detection is pattern-based with checksums where one exists (Luhn for cards, mod-97 for IBANs, the HMRC
prefix rules for National Insurance numbers). Names are found by an optional NER model plus the names the
bank already knows for this customer. See ``docs/compliance.md`` for the known gaps.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class PiiType(StrEnum):
    CARD_NUMBER = "CARD_NUMBER"
    IBAN = "IBAN"
    NI_NUMBER = "NI_NUMBER"
    SORT_CODE = "SORT_CODE"
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    EMAIL = "EMAIL"
    UK_PHONE = "UK_PHONE"
    POSTCODE = "POSTCODE"
    ADDRESS = "ADDRESS"
    PERSON_NAME = "PERSON_NAME"


@dataclass(frozen=True)
class Finding:
    type: PiiType
    start: int
    end: int
    value: str


class NameDetector(Protocol):
    def find_names(self, text: str) -> list[tuple[int, int]]: ...


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_valid(iban: str) -> bool:
    compact = iban.replace(" ", "").upper()
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


# HMRC: first letter not D, F, I, Q, U, V; second not D, F, I, O, Q, U, V; some prefixes never issued.
_NI_INVALID_PREFIXES = {"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"}
_NI = re.compile(r"\b([A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z])\s?(\d{2})\s?(\d{2})\s?(\d{2})\s?([A-D])\b", re.I)
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_IBAN = re.compile(r"\bGB\d{2}\s?[A-Z]{4}(?:\s?\d){14}\b", re.I)
_SORT_CODE = re.compile(r"\b\d{2}[- ]\d{2}[- ]\d{2}\b")
_SORT_CODE_PLAIN = re.compile(r"(?i)sort\s*code\D{0,15}?\b(\d{6})\b")
_ACCOUNT = re.compile(r"\b\d{8}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# UK numbers: +44 or 0, then 9-10 more digits starting 1, 2, 3, 7, 8 or 9, with optional spaces/dashes.
_PHONE = re.compile(r"(?<![\d+])(?:\+44\s?(?:\(0\)\s?)?|0)[1-37-9](?:[\s-]?\d){8,9}(?!\d)")
_POSTCODE = re.compile(r"\b(?:GIR ?0AA|[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]? ?\d[ABD-HJLNP-UW-Z]{2})\b", re.I)
_STREET = re.compile(
    r"(?:\b(?:flat|apartment|apt)\s+)?\b\d{1,4}[A-Za-z]?,?\s+(?:[A-Z][A-Za-z'’-]+\s+){1,3}"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Lane|Ln|Close|Drive|Dr|Way|Court|Ct|Place|Pl|Gardens|Gdns|Crescent|"
    r"Terrace|Grove|Square|Sq|Hill|Park|Mews|Row|Walk|Rise|View|Green|Parade)\b", re.I)
_TITLED_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr)\.?\s+([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2})")
_SELF_NAME = re.compile(r"(?i:\bmy name is|\bthis is|\bname:)\s+([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2})")

# When two findings overlap, the higher-priority (lower number) one wins, then the longer one.
_PRIORITY = {t: i for i, t in enumerate(PiiType)}


class SpacyNameDetector:
    """PERSON entities from a spaCy model (install the ``ner`` extra and a model, e.g. en_core_web_sm)."""

    def __init__(self, model: str = "en_core_web_sm") -> None:
        import spacy

        self._nlp = spacy.load(model, disable=["lemmatizer", "textcat"])

    def find_names(self, text: str) -> list[tuple[int, int]]:
        return [(ent.start_char, ent.end_char) for ent in self._nlp(text).ents if ent.label_ == "PERSON"]


def default_name_detector() -> NameDetector | None:
    try:
        return SpacyNameDetector()
    except Exception:  # spaCy or the model isn't installed: fall back to patterns + known names
        return None


class Redactor:
    def __init__(self, name_detector: NameDetector | None = None) -> None:
        self.name_detector = name_detector

    def find(self, text: str, known_names: Iterable[str] = ()) -> list[Finding]:
        found: list[Finding] = []

        def add(kind: PiiType, start: int, end: int) -> None:
            found.append(Finding(kind, start, end, text[start:end]))

        for m in _CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and luhn_valid(digits):
                add(PiiType.CARD_NUMBER, m.start(), m.end())
        for m in _IBAN.finditer(text):
            if iban_valid(m.group()):
                add(PiiType.IBAN, m.start(), m.end())
        for m in _NI.finditer(text):
            if m.group(1).upper() not in _NI_INVALID_PREFIXES:
                add(PiiType.NI_NUMBER, m.start(), m.end())
        for m in _SORT_CODE.finditer(text):
            add(PiiType.SORT_CODE, m.start(), m.end())
        for m in _SORT_CODE_PLAIN.finditer(text):
            add(PiiType.SORT_CODE, m.start(1), m.end(1))
        # Any standalone 8-digit number is treated as an account number. That over-redacts some
        # reference numbers and yyyymmdd dates; in a bank, over-redaction is the safe failure.
        for m in _ACCOUNT.finditer(text):
            add(PiiType.ACCOUNT_NUMBER, m.start(), m.end())
        for m in _EMAIL.finditer(text):
            add(PiiType.EMAIL, m.start(), m.end())
        for m in _PHONE.finditer(text):
            add(PiiType.UK_PHONE, m.start(), m.end())
        for m in _POSTCODE.finditer(text):
            add(PiiType.POSTCODE, m.start(), m.end())
        for m in _STREET.finditer(text):
            add(PiiType.ADDRESS, m.start(), m.end())
        for pattern in (_TITLED_NAME, _SELF_NAME):
            for m in pattern.finditer(text):
                add(PiiType.PERSON_NAME, m.start(1), m.end(1))
        for name in {n.strip() for n in known_names if n and len(n.strip()) >= 2}:
            for m in re.finditer(rf"\b{re.escape(name)}\b", text, re.I):
                add(PiiType.PERSON_NAME, m.start(), m.end())
        if self.name_detector is not None:
            for start, end in self.name_detector.find_names(text):
                add(PiiType.PERSON_NAME, start, end)
        return _resolve_overlaps(found)

    def redact(self, text: str, vault: PiiVault, known_names: Iterable[str] = ()) -> str:
        findings = self.find(text, known_names)
        out, cursor = [], 0
        for f in findings:
            out.append(text[cursor:f.start])
            out.append(vault.placeholder(f.type, f.value))
            cursor = f.end
        out.append(text[cursor:])
        return "".join(out)

    def redact_json(self, value: Any, vault: PiiVault, known_names: Iterable[str] = ()) -> Any:
        """Redact every string inside a JSON-like structure (used on MCP tool results)."""
        names = list(known_names)
        if isinstance(value, str):
            return self.redact(value, vault, names)
        if isinstance(value, list):
            return [self.redact_json(v, vault, names) for v in value]
        if isinstance(value, dict):
            return {k: self.redact_json(v, vault, names) for k, v in value.items()}
        return value


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    chosen: list[Finding] = []
    for f in sorted(findings, key=lambda f: (_PRIORITY[f.type], -(f.end - f.start), f.start)):
        if all(f.end <= c.start or f.start >= c.end for c in chosen):
            chosen.append(f)
    return sorted(chosen, key=lambda f: f.start)


_PLACEHOLDER = re.compile(r"\[(" + "|".join(t.value for t in PiiType) + r")_(\d+)\]")


class PiiVault:
    """Per-session mapping between placeholders and original values. The same value always maps to
    the same placeholder within a session, so the model can refer to "[ACCOUNT_NUMBER_1]" consistently."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._by_placeholder: dict[str, str] = dict(mapping or {})
        self._by_value = {(self._type_of(p), _norm(v)): p for p, v in self._by_placeholder.items()}
        self._new: dict[str, str] = {}

    @staticmethod
    def _type_of(placeholder: str) -> str:
        return placeholder[1:].rsplit("_", 1)[0]

    def placeholder(self, kind: PiiType, value: str) -> str:
        key = (kind.value, _norm(value))
        if key in self._by_value:
            return self._by_value[key]
        index = 1 + sum(1 for p in self._by_placeholder if self._type_of(p) == kind.value)
        placeholder = f"[{kind.value}_{index}]"
        self._by_placeholder[placeholder] = value
        self._by_value[key] = placeholder
        self._new[placeholder] = value
        return placeholder

    @property
    def new_entries(self) -> dict[str, str]:
        """Placeholders created since this vault was loaded (to persist)."""
        return dict(self._new)

    def reveal(self, text: str) -> str:
        """Swap placeholders back to original values. Only for text going to a trusted backend."""
        return _PLACEHOLDER.sub(lambda m: self._by_placeholder.get(m.group(0), m.group(0)), text)

    def reveal_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.reveal(value)
        if isinstance(value, list):
            return [self.reveal_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.reveal_json(v) for k, v in value.items()}
        return value

    def render_for_customer(self, text: str) -> str:
        """Placeholders in a reply shown to the authenticated customer: echo back their own contact details,
        but keep financial identifiers masked even to them."""

        def render(m: re.Match[str]) -> str:
            value = self._by_placeholder.get(m.group(0))
            if value is None:
                return "[redacted]"
            digits = re.sub(r"\D", "", value)
            match PiiType(m.group(1)):
                case PiiType.CARD_NUMBER | PiiType.ACCOUNT_NUMBER | PiiType.IBAN:
                    return f"****{digits[-4:]}"
                case PiiType.SORT_CODE:
                    return f"**-**-{digits[-2:]}"
                case PiiType.NI_NUMBER:
                    return "your National Insurance number"
                case _:
                    return value

        return _PLACEHOLDER.sub(render, text)


def _norm(value: str) -> str:
    return re.sub(r"[\s-]", "", value).lower()
