#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared utilities for localization KB."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

SLOT_HINTS = {
    "name",
    "description",
    "desc",
    "header",
    "title",
    "tooltip",
    "content",
    "short",
    "long",
    "label",
    "objective",
    "reason",
}

STOP_TOKENS = {
    "ui",
    "game",
    "menu",
    "world",
    "popup",
    "panel",
    "slot",
    "icon",
    "info",
    "generic",
}


def normalize_text(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_camel(token: str) -> List[str]:
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", token)
    if not parts:
        return [token.lower()]
    return [p.lower() for p in parts]


def key_to_tokens(key: str) -> List[str]:
    tokens: List[str] = []
    for part in key.replace("\ufeff", "").split("_"):
        if not part:
            continue
        tokens.extend(split_camel(part))
    return [t for t in tokens if t]


def parse_key_structure(key: str) -> Tuple[str, str, str, str]:
    tokens = key_to_tokens(key)
    if not tokens:
        return "", "", "", ""

    domain = tokens[0]

    slot = ""
    for t in reversed(tokens):
        if t in SLOT_HINTS:
            slot = t
            break

    if slot:
        idx = len(tokens) - 1 - list(reversed(tokens)).index(slot)
        entity_tokens = tokens[1:idx]
    else:
        entity_tokens = tokens[1:]

    entity_tokens = [t for t in entity_tokens if t not in STOP_TOKENS]
    all_tokens = [t for t in tokens if t not in STOP_TOKENS]

    entity = " ".join(entity_tokens).strip()
    de = " ".join([domain] + entity_tokens).strip()
    all_norm = " ".join(all_tokens).strip()
    return domain, entity, slot, de if de else all_norm


def auto_pick_file(input_path: Path) -> Path:
    if input_path.is_file():
        return input_path

    if not input_path.exists():
        raise FileNotFoundError(f"Path not found: {input_path}")

    candidates = sorted(
        [p for p in input_path.glob("*") if p.is_file() and p.suffix.lower() in {".txt", ".json"}],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No .txt/.json localization file found in: {input_path}")
    return candidates[0]


def load_localization(path: Path) -> Dict[str, str]:
    raw = path.read_text(encoding="utf-8-sig")

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Root JSON is not an object")
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass

    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Root JSON is not an object")
    return {str(k): str(v) for k, v in data.items()}


def simple_tokens(text: str) -> List[str]:
    n = normalize_text(text)
    return n.split() if n else []
