#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Logika laczenia z radiem - port `_normalize_candidates` / `connect()` z
radio_service.py (aplikacja desktopowa FSRadio), dostosowany do
synchronicznego FSApiClient (fsapi_client.py) uzywanego w pluginie Kodi.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from fsapi_client import FSApiClient, FSApiError, FSConnectionError, InvalidPinError


def normalize_candidates(user_input: str) -> List[str]:
    """Identyczna logika jak RadioService._normalize_candidates."""
    s = user_input.strip()
    if not s:
        return []
    if not s.startswith(("http://", "https://")):
        s = "http://" + s

    if re.search(r"/(device|fsapi)(/)?$", s):
        return [s]

    host_port = s.rstrip("/")
    host_only = host_port.split("//", 1)[1]
    has_port = ":" in host_only
    candidates = [
        (host_port + "/device") if has_port else (host_port + ":80/device"),
        (host_port + "/fsapi") if has_port else (host_port + ":80/fsapi"),
        host_port,
    ]
    seen, uniq = set(), []
    for c in candidates:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


def connect(user_url: str, pin, timeout: int = 5, max_attempts: int = 3) -> FSApiClient:
    """
    Laczy sie z radiem z retry (5s -> 10s -> 15s), tak jak
    RadioService.connect() w aplikacji desktopowej. Zwraca gotowy FSApiClient
    lub podnosi wyjatek z ostatnim bledem po wyczerpaniu wszystkich prob.
    """
    candidates = normalize_candidates(user_url)
    if not candidates:
        raise FSApiError("Pusty adres radia.")

    last_err: Optional[Exception] = None
    for base in candidates:
        for attempt in range(max_attempts):
            current_timeout = timeout + (attempt * 5)
            try:
                client = FSApiClient.create(base, pin, current_timeout)
                # tania proba - jak w radio_service.py: get_friendly_name()
                client.get_friendly_name()
                return client
            except Exception as e:  # noqa: BLE001 - probujemy kolejnych kandydatow
                last_err = e
                if attempt < max_attempts - 1:
                    time.sleep(0.5)
                    continue
                break

    tried = ", ".join(candidates)
    raise FSConnectionError(
        f"Nie udalo sie polaczyc z radiem. Probowano: {tried}. Ostatni blad: {last_err}"
    )
