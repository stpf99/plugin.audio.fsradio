#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synchroniczny SSDP discovery dla radiow Frontier Silicon (UNDOK) - port
discovery.py (aplikacja desktopowa FSRadio) z asyncio na zwykle sockety,
bo Kodi wywoluje plugin jako krotkotrwaly proces (bez petli zdarzen w tle).
"""

from __future__ import annotations

import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

import requests

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
FSAPI_SEARCH_TARGET = "urn:schemas-frontier-silicon-com:fs_reference:fsapi:1"
SEARCH_TARGETS = (
    FSAPI_SEARCH_TARGET,
    "urn:schemas-upnp-org:device:MediaRenderer:1",
)

MSEARCH_TEMPLATE = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: {addr}:{port}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: {mx}\r\n"
    "ST: {st}\r\n"
    "\r\n"
)


@dataclass
class DiscoveredRadio:
    ip: str
    location: Optional[str] = None
    friendly_name: Optional[str] = None
    device_url: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None

    def display_name(self) -> str:
        if self.friendly_name:
            return f"{self.friendly_name} ({self.ip})"
        return self.ip


def _parse_ssdp_headers(raw: str) -> dict:
    headers = {}
    for line in raw.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().upper()] = value.strip()
    return headers


def _search_once(st: str, mx: int, timeout: float) -> list:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError:
        pass
    sock.settimeout(timeout)

    msg = MSEARCH_TEMPLATE.format(addr=SSDP_ADDR, port=SSDP_PORT, mx=mx, st=st).encode("utf-8")
    results = []
    try:
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            results.append((addr[0], _parse_ssdp_headers(text)))
    finally:
        sock.close()
    return results


def _probe_device_xml(location: str) -> dict:
    info = {}
    try:
        resp = requests.get(location, timeout=3)
        root = ET.fromstring(resp.content)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        device = root.find(f"{ns}device")
        if device is not None:
            fn = device.find(f"{ns}friendlyName")
            mf = device.find(f"{ns}manufacturer")
            md = device.find(f"{ns}modelName")
            if fn is not None and fn.text:
                info["friendly_name"] = fn.text.strip()
            if mf is not None and mf.text:
                info["manufacturer"] = mf.text.strip()
            if md is not None and md.text:
                info["model"] = md.text.strip()
    except Exception:
        pass
    return info


def discover_radios(timeout: float = 3.0, mx: int = 2, probe_details: bool = True) -> List[DiscoveredRadio]:
    by_ip = {}
    for st in SEARCH_TARGETS:
        try:
            results = _search_once(st, mx=mx, timeout=timeout)
        except (PermissionError, OSError):
            continue
        for ip, headers in results:
            entry = by_ip.setdefault(ip, {"headers_list": []})
            entry["headers_list"].append(headers)

    radios: List[DiscoveredRadio] = []
    for ip, entry in by_ip.items():
        location = None
        for headers in entry["headers_list"]:
            loc = headers.get("LOCATION")
            if loc:
                location = loc
                break

        radio = DiscoveredRadio(ip=ip, location=location)
        if probe_details and location:
            xml_info = _probe_device_xml(location)
            radio.friendly_name = xml_info.get("friendly_name")
            radio.manufacturer = xml_info.get("manufacturer")
            radio.model = xml_info.get("model")
        radio.device_url = f"http://{ip}:80"
        radios.append(radio)

    return radios
