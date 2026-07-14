#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synchroniczny klient protokolu FSAPI (Frontier Silicon / UNDOK), zbudowany
na 'requests' dla srodowiska Kodi (brak aiohttp na wielu platformach Kodi).

Odwzorowuje ten sam zestaw wywolan XML co pakiet 'afsapi' uzywany przez
RadioService w aplikacji desktopowej FSRadio (radio_service.py) - te same
endpointy GET/SET/LIST_GET_NEXT, ta sama logika sesji (pin + sid), ta sama
semantyka nawigacji (nav_state, navigate, select_item).

Referencja protokolu: pakiet PyPI 'afsapi' (AFSAPI.__call, handle_list,
nav_select_folder/nav_select_item/nav_select_parent_folder, get_modes,
get_presets, select_preset, get_play_*).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

import requests


class FSApiError(Exception):
    pass


class InvalidPinError(FSApiError):
    pass


class FSConnectionError(FSApiError):
    pass


@dataclass
class PlayerMode:
    key: int
    label: str
    id: Optional[str] = None
    selectable: bool = True


@dataclass
class Preset:
    key: int
    type: Optional[int]
    name: str


@dataclass
class NavEntry:
    key: int
    name: str
    is_folder: bool
    graphic_url: Optional[str] = None
    raw_type: Optional[int] = None


@dataclass
class NowPlaying:
    station_name: Optional[str] = None
    artist: Optional[str] = None
    text: Optional[str] = None
    graphic_url: Optional[str] = None
    status: Optional[str] = None


PLAY_STATUS_NAMES = {
    0: "STOPPED",
    1: "UNKNOWN",
    2: "PLAYING",
    3: "PAUSED",
    6: "LOADING",
    7: "ERROR",
}


class FSApiClient:
    """
    Blokujacy (synchroniczny) klient FSAPI - jedno wywolanie = jedno
    zadanie HTTP GET z parametrami pin/sid, tak jak robi to AFSAPI.__call
    w oryginalnym pakiecie async, tylko bez petli asyncio (w Kodi kazde
    wywolanie pluginu to osobny, krotkotrwaly proces, wiec watek w tle
    z radio_service.py nie jest tu potrzebny).
    """

    def __init__(self, webfsapi_endpoint: str, pin, timeout: int = 8):
        self.webfsapi_endpoint = webfsapi_endpoint.rstrip("/")
        self.pin = str(pin)
        self.timeout = timeout
        self.sid: Optional[str] = None
        self._modes_cache: Optional[List[PlayerMode]] = None
        self._session = requests.Session()

    # ---------------- discovery / bootstrap ----------------
    @staticmethod
    def get_webfsapi_endpoint(fsapi_device_url: str, timeout: int = 8) -> str:
        """GET base device URL, parse <webfsapi>...</webfsapi> from XML."""
        try:
            resp = requests.get(fsapi_device_url, timeout=timeout)
            resp.raise_for_status()
            doc = ET.fromstring(resp.content)
            api = doc.find("webfsapi")
            if api is not None and api.text:
                return api.text
            raise FSApiError(f"Brak <webfsapi> w odpowiedzi z {fsapi_device_url}")
        except requests.exceptions.Timeout as err:
            raise FSConnectionError(f"Brak odpowiedzi z {fsapi_device_url}") from err
        except requests.exceptions.ConnectionError as err:
            raise FSConnectionError(f"Nie mozna polaczyc z {fsapi_device_url}") from err

    @classmethod
    def create(cls, fsapi_device_url: str, pin, timeout: int = 8) -> "FSApiClient":
        endpoint = cls.get_webfsapi_endpoint(fsapi_device_url, timeout)
        return cls(endpoint, pin, timeout)

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    # ---------------- low-level call ----------------
    def _call(self, path: str, extra: Optional[dict] = None,
               force_new_session: bool = False, retry_with_session: bool = True) -> ET.Element:
        params = {"pin": self.pin}
        if force_new_session or not self.sid:
            if force_new_session:
                self.sid = None
        if self.sid:
            params["sid"] = self.sid
        if extra:
            params.update(extra)

        url = f"{self.webfsapi_endpoint}/{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as err:
            raise FSConnectionError(f"{self.webfsapi_endpoint} nie odpowiedzial w {self.timeout}s") from err
        except requests.exceptions.ConnectionError as err:
            raise FSConnectionError(f"Nie mozna polaczyc z {self.webfsapi_endpoint}") from err

        if resp.status_code == 403:
            raise InvalidPinError("Odmowa dostepu - bledny PIN")
        if resp.status_code == 404:
            if not force_new_session and retry_with_session:
                return self._call(path, extra, force_new_session=True, retry_with_session=False)
            raise FSApiError("Bledny sid lub nieprawidlowe polecenie")
        if resp.status_code >= 400:
            raise FSApiError(f"Nieoczekiwana odpowiedz HTTP {resp.status_code}")

        doc = ET.fromstring(resp.content)
        status_el = doc.find("status")
        status = status_el.text if status_el is not None else None
        if status == "FS_NODE_BLOCKED" and not force_new_session and retry_with_session:
            return self._call(path, extra, force_new_session=True, retry_with_session=False)
        return doc

    def _create_session(self) -> Optional[str]:
        doc = self._call("CREATE_SESSION", retry_with_session=False)
        el = doc.find("sessionId")
        self.sid = el.text if el is not None else None
        return self.sid

    def _ensure_session(self):
        if not self.sid:
            self._create_session()

    def handle_get(self, item: str) -> ET.Element:
        self._ensure_session()
        return self._call(f"GET/{item}")

    def handle_set(self, item: str, value) -> bool:
        self._ensure_session()
        doc = self._call(f"SET/{item}", {"value": value})
        status_el = doc.find("status")
        return status_el is not None and status_el.text == "FS_OK"

    @staticmethod
    def _extract_value_text(doc: ET.Element, tag_hint: Optional[str] = None) -> Optional[str]:
        value_el = doc.find("value")
        if value_el is None:
            return None
        for child in value_el:
            if tag_hint is None or child.tag == tag_hint:
                return child.text
        return None

    def get_str(self, item: str) -> Optional[str]:
        doc = self.handle_get(item)
        return self._extract_value_text(doc)

    def get_int(self, item: str) -> Optional[int]:
        val = self._extract_value_text(self.handle_get(item))
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def handle_list(self, list_name: str):
        """Generator: (key:str, fields:dict) dla kazdego elementu listy."""
        self._ensure_session()
        start = -1
        count = 50
        while True:
            doc = self._call(f"LIST_GET_NEXT/{list_name}/{start}", {"maxItems": count})
            status_el = doc.find("status")
            if status_el is None or status_el.text != "FS_OK":
                return
            items = doc.findall("item")
            for item in items:
                key = item.get("key", "-1")
                fields = {}
                for field in item.findall("field"):
                    name = field.get("name")
                    child = list(field)
                    if child:
                        tag = child[0].tag
                        text = child[0].text
                        if tag in ("u8", "u16", "u32", "s8", "s16", "s32", "e8"):
                            try:
                                fields[name] = int(text)
                            except (TypeError, ValueError):
                                fields[name] = None
                        else:
                            fields[name] = text
                yield key, fields
            end_reached = doc.find("listend") is not None
            start += count
            if end_reached or not items:
                return

    # ---------------- device basics ----------------
    def get_friendly_name(self) -> Optional[str]:
        return self.get_str("netRemote.sys.info.friendlyName")

    def get_power(self) -> bool:
        return bool(self.get_int("netRemote.sys.power") or 0)

    def set_power(self, on: bool) -> bool:
        return self.handle_set("netRemote.sys.power", 1 if on else 0)

    def get_volume(self) -> int:
        return self.get_int("netRemote.sys.audio.volume") or 0

    def set_volume(self, value: int) -> bool:
        return self.handle_set("netRemote.sys.audio.volume", int(value))

    def get_play_status(self) -> Optional[str]:
        code = self.get_int("netRemote.play.status")
        if code is None:
            return None
        return PLAY_STATUS_NAMES.get(code, str(code))

    def get_play_name(self) -> Optional[str]:
        return self.get_str("netRemote.play.info.name")

    def get_play_text(self) -> Optional[str]:
        return self.get_str("netRemote.play.info.text")

    def get_play_artist(self) -> Optional[str]:
        return self.get_str("netRemote.play.info.artist")

    def get_play_graphic(self) -> Optional[str]:
        return self.get_str("netRemote.play.info.graphicUri")

    def play_control(self, value: int) -> bool:
        """1=play/resume, 2=pause, 3=next, 4=previous (per fsapi convention)."""
        return self.handle_set("netRemote.play.control", value)

    def get_now_playing(self) -> NowPlaying:
        np = NowPlaying()
        for fn, attr in (
            (self.get_play_name, "station_name"),
            (self.get_play_artist, "artist"),
            (self.get_play_text, "text"),
            (self.get_play_graphic, "graphic_url"),
        ):
            try:
                setattr(np, attr, fn())
            except Exception:
                pass
        try:
            np.status = self.get_play_status()
        except Exception:
            pass
        return np

    # ---------------- modes ----------------
    def get_modes(self) -> List[PlayerMode]:
        if self._modes_cache is not None:
            return self._modes_cache
        modes = []
        for key, fields in self.handle_list("netRemote.sys.caps.validModes"):
            label = fields.get("label") or f"Mode {key}"
            modes.append(PlayerMode(key=int(key), label=str(label), id=fields.get("id")))
        self._modes_cache = modes
        return modes

    def get_current_mode_key(self) -> Optional[int]:
        return self.get_int("netRemote.sys.mode")

    def get_current_mode(self) -> Optional[PlayerMode]:
        cur = self.get_current_mode_key()
        if cur is None:
            return None
        for m in self.get_modes():
            if m.key == cur:
                return m
        return None

    def set_mode(self, mode) -> bool:
        key = mode.key if isinstance(mode, PlayerMode) else int(mode)
        return self.handle_set("netRemote.sys.mode", key)

    # ---------------- navigation ----------------
    def _enable_nav_if_necessary(self):
        state = self.get_int("netRemote.nav.state")
        if state != 1:
            self.handle_set("netRemote.nav.state", 1)

    def nav_reset(self) -> bool:
        return self.handle_set("netRemote.nav.state", 0)

    def nav_list(self) -> List[NavEntry]:
        self._enable_nav_if_necessary()
        entries = []
        for key, item in self.handle_list("netRemote.nav.list"):
            name = item.get("name") or f"Item {key}"
            raw_type = item.get("type")
            entries.append(
                NavEntry(
                    key=int(key),
                    name=str(name),
                    is_folder=(raw_type == 0 or raw_type is None),
                    graphic_url=item.get("graphicUri") or None,
                    raw_type=raw_type,
                )
            )
        return entries

    def nav_select_folder(self, key: int) -> bool:
        self._enable_nav_if_necessary()
        return self.handle_set("netRemote.nav.action.navigate", int(key))

    def nav_select_parent_folder(self) -> bool:
        self._enable_nav_if_necessary()
        return self.handle_set("netRemote.nav.action.navigate", 0xFFFFFFFF)

    def nav_select_item(self, key: int) -> bool:
        self._enable_nav_if_necessary()
        return self.handle_set("netRemote.nav.action.selectItem", int(key))

    def enter_folder_or_play(self, entry: NavEntry) -> str:
        try:
            self.nav_select_folder(entry.key)
            return "folder"
        except Exception:
            self.nav_select_item(entry.key)
            return "item"

    # ---------------- presets ----------------
    def get_presets(self) -> List[Preset]:
        self._enable_nav_if_necessary()
        presets = []
        for key, fields in self.handle_list("netRemote.nav.presets"):
            name = fields.get("name")
            if not name:
                continue
            presets.append(Preset(key=int(key), type=fields.get("type"), name=str(name).strip()))
        return presets

    def select_preset(self, preset) -> bool:
        self._enable_nav_if_necessary()
        key = preset.key if isinstance(preset, Preset) else int(preset)
        return self.handle_set("netRemote.nav.action.selectPreset", key)
