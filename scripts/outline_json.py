"""Syntax-only outline JSON repair. Never completes truncated content or invents values."""
from __future__ import annotations

import json
import re


def repair_json_syntax(text: str) -> str:
    """Remove a BOM/fence and trailing commas, respecting quoted/escaped text.

    Missing values, missing braces, multiple documents and broken strings stay
    invalid. In particular a truncated model reply is never 'completed'.
    """
    text = text.strip().lstrip("\ufeff").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    out: list[str] = []
    quoted = escaped = False
    for i, char in enumerate(text):
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char == ",":
            tail = text[i + 1:].lstrip()
            if not tail.startswith(("}", "]")):
                out.append(char)
        else:
            out.append(char)
    repaired = "".join(out)
    # Preserve the existing parser's tolerance for prose after one complete
    # document, while rejecting a second JSON value, duplicate keys and NaN.
    decoder = json.JSONDecoder(object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    value, end = decoder.raw_decode(repaired.lstrip())
    repaired = repaired.lstrip()
    trailing = re.sub(r"^```(?:json)?\s*", "", repaired[end:].strip(), flags=re.I)
    if trailing:
        try:
            decoder.raw_decode(trailing)
        except json.JSONDecodeError:
            if trailing.startswith(("{", "[")):
                raise json.JSONDecodeError("Extra structured data", repaired, end)
        else:
            raise json.JSONDecodeError("Extra JSON value", repaired, end)
    return repaired[:end]


def _unique_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("Non-finite JSON number")
