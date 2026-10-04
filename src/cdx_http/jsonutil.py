"""JSON helpers.

* ``loads_decimal`` — the Decimal-safe parser probes should use (invariant 5).
* ``raw_object_members`` / ``raw_array_elements`` — return the *verbatim source text*
  of each member/element, so the recorder can store slices of a payload byte-for-byte.
  Re-serializing parsed JSON is not verbatim: ``0.00007903`` parses to a float and
  re-emits as ``7.903e-05``; a Decimal round-trip has similar problems.

The raw helpers assume their input is already known to be valid JSON (callers
``json.loads`` it first); they do not validate.
"""
from __future__ import annotations

import json
from decimal import Decimal
from json.decoder import scanstring

_WS = " \t\n\r"


def loads_decimal(data: bytes | str):
    """Parse JSON with every non-integer number as ``Decimal``."""
    return json.loads(data, parse_float=Decimal)


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in _WS:
        i += 1
    return i


def _value_end(s: str, i: int) -> int:
    """Index just past the JSON value that starts at ``s[i]``."""
    c = s[i]
    if c == '"':
        return scanstring(s, i + 1)[1]
    if c in "{[":
        depth = 0
        j = i
        n = len(s)
        while j < n:
            ch = s[j]
            if ch == '"':
                j = scanstring(s, j + 1)[1]
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ValueError("unterminated container")
    j = i
    n = len(s)
    while j < n and s[j] not in ",}]" + _WS:
        j += 1
    return j


def raw_object_members(s: str) -> dict[str, str]:
    """Map each top-level key of a JSON object to its value's verbatim source text."""
    i = _skip_ws(s, 0)
    if s[i] != "{":
        raise ValueError("not a JSON object")
    out: dict[str, str] = {}
    i = _skip_ws(s, i + 1)
    if s[i] == "}":
        return out
    while True:
        if s[i] != '"':
            raise ValueError(f"expected key at {i}")
        key, i = scanstring(s, i + 1)
        i = _skip_ws(s, i)
        if s[i] != ":":
            raise ValueError(f"expected ':' at {i}")
        i = _skip_ws(s, i + 1)
        end = _value_end(s, i)
        out[key] = s[i:end]
        i = _skip_ws(s, end)
        if s[i] == ",":
            i = _skip_ws(s, i + 1)
            continue
        if s[i] == "}":
            return out
        raise ValueError(f"expected ',' or '}}' at {i}")


def raw_array_elements(s: str) -> list[str]:
    """Verbatim source text of each top-level element of a JSON array."""
    i = _skip_ws(s, 0)
    if s[i] != "[":
        raise ValueError("not a JSON array")
    out: list[str] = []
    i = _skip_ws(s, i + 1)
    if s[i] == "]":
        return out
    while True:
        end = _value_end(s, i)
        out.append(s[i:end])
        i = _skip_ws(s, end)
        if s[i] == ",":
            i = _skip_ws(s, i + 1)
            continue
        if s[i] == "]":
            return out
        raise ValueError(f"expected ',' or ']' at {i}")


def one_line(raw: str) -> str:
    """Make valid JSON text safe for a JSON Lines record.

    Raw CR/LF cannot occur inside a valid JSON string, so any present are
    insignificant whitespace and replacing them with spaces does not change the value.
    """
    if "\n" in raw or "\r" in raw:
        return raw.replace("\r", " ").replace("\n", " ")
    return raw
