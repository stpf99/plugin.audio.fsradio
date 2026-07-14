#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin.audio.fsradio - sterowanie radiem sieciowym Frontier Silicon / UNDOK
z poziomu Kodi.

Wykorzystuje te sama logike protokolu FSAPI co aplikacja desktopowa
FSRadio (radio_service.py / discovery.py), przeniesiona tutaj do wersji
synchronicznej (bez petli asyncio w tle - kazde wywolanie pluginu w Kodi
to krotkotrwaly, oddzielny proces):

  resources/lib/fsapi_client.py   -> odpowiednik afsapi.AFSAPI
  resources/lib/radio_connect.py  -> odpowiednik RadioService.connect()
  resources/lib/ssdp_discovery.py -> odpowiednik discovery.py

Struktura menu:
  Root
   |- Szukaj radia w sieci (SSDP)
   |- Polacz recznie (adres z ustawien)
   |- Zrodla (Internet Radio, DAB, FM, ...)      -> lista modes -> nav_list (przegladanie folderow)
   |- Ulubione (presety)                          -> get_presets -> select_preset
   |- Teraz odtwarzane                            -> get_now_playing (info)
"""

import sys
import os
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon

LIB_DIR = os.path.join(
    xbmcaddon.Addon().getAddonInfo("path"), "resources", "lib"
)
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from fsapi_client import FSApiClient, FSApiError, Preset  # noqa: E402
import radio_connect  # noqa: E402
import ssdp_discovery  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if sys.argv else f"plugin://{ADDON_ID}"


def _(text_id: int) -> str:
    return ADDON.getLocalizedString(text_id)


def log(msg: str):
    xbmc.log(f"[{ADDON_ID}] {msg}", xbmc.LOGINFO)


def _safe_get_setting(setting_id: str, default: str = "") -> str:
    """
    Odczyt ustawienia odporny na roznice API miedzy wersjami Kodi.
    Niektore buildy (w tym Kodi 21 z pewnymi konfiguracjami skorek)
    rzucaja TypeError('Invalid setting type') zarowno z getSettingString,
    jak i z generycznego getSetting, jesli wewnetrzny rejestr typow
    addonu jest niespojny z resources/settings.xml (np. po aktualizacji
    definicji bez pelnego resetu zapisanych wartosci). Probujemy po
    kolei kazdej dostepnej metody i uzywamy pierwszej, ktora sie uda.
    """
    for method_name in ("getSettingString", "getSetting"):
        method = getattr(ADDON, method_name, None)
        if method is None:
            continue
        try:
            value = method(setting_id)
            if value is not None:
                return str(value)
        except Exception as e:
            log(f"_safe_get_setting({setting_id}) via {method_name} failed: {e}")
            continue
    return default


def _safe_set_setting(setting_id: str, value: str):
    for method_name in ("setSettingString", "setSetting"):
        method = getattr(ADDON, method_name, None)
        if method is None:
            continue
        try:
            method(setting_id, value)
            return
        except Exception as e:
            log(f"_safe_set_setting({setting_id}) via {method_name} failed: {e}")
            continue


def build_url(**kwargs) -> str:
    return BASE_URL + "?" + urllib.parse.urlencode(kwargs)


def get_params() -> dict:
    paramstring = sys.argv[2][1:] if len(sys.argv) > 2 else ""
    return dict(urllib.parse.parse_qsl(paramstring))


# ---------------- connection helpers ----------------

def _get_client() -> FSApiClient:
    """
    Buduje klienta polaczony z radiem, uzywajac adresu/pinu z ustawien
    addonu (analogicznie do pol RadioService.url_used / .pin w apce
    desktopowej). Rzuca wyjatek jesli polaczenie sie nie uda.
    """
    url = _safe_get_setting("radio_url").strip()
    pin = _safe_get_setting("radio_pin", "1234").strip() or "1234"
    try:
        timeout = int(_safe_get_setting("connect_timeout", "5") or "5")
    except ValueError:
        timeout = 5

    if not url:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"),
            _(30002),
            xbmcgui.NOTIFICATION_ERROR,
        )
        raise FSApiError("Brak skonfigurowanego adresu radia.")

    return radio_connect.connect(url, pin, timeout=timeout)


def _connect_with_progress() -> FSApiClient:
    dialog = xbmcgui.DialogProgress()
    dialog.create(ADDON.getAddonInfo("name"), _(30107))
    try:
        client = _get_client()
    except Exception as e:
        dialog.close()
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"),
            _(30108),
            xbmcgui.NOTIFICATION_ERROR,
        )
        import traceback
        log(f"connect error: {type(e).__name__}: {e}")
        log("connect traceback:\n" + traceback.format_exc())
        raise
    dialog.close()
    return client


def _set_saved_url_from_discovery(radio: "ssdp_discovery.DiscoveredRadio"):
    _safe_set_setting("radio_url", radio.device_url or radio.ip)


# ---------------- menu: root ----------------

def list_root():
    xbmcplugin.setPluginCategory(HANDLE, ADDON.getAddonInfo("name"))
    xbmcplugin.setContent(HANDLE, "files")

    items = [
        (build_url(action="discover"), _(30100), "DefaultNetwork.png", True),
        (build_url(action="connect"), _(30101), "DefaultAddonService.png", True),
        (build_url(action="modes"), _(30102), "DefaultMusicGenres.png", True),
        (build_url(action="presets"), _(30103), "DefaultMusicPlaylists.png", True),
        (build_url(action="nowplaying"), _(30104), "DefaultMusicSongs.png", False),
    ]
    for url, label, icon, is_folder in items:
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": icon, "thumb": icon})
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=is_folder)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------- SSDP discovery ----------------

def discover():
    try:
        ssdp_timeout = float(_safe_get_setting("ssdp_timeout", "3.0") or "3.0")
    except ValueError:
        ssdp_timeout = 3.0

    dialog = xbmcgui.DialogProgress()
    dialog.create(ADDON.getAddonInfo("name"), _(30106))
    try:
        radios = ssdp_discovery.discover_radios(timeout=ssdp_timeout)
    finally:
        dialog.close()

    if not radios:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"), _(30105), xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    labels = [r.display_name() for r in radios]
    idx = xbmcgui.Dialog().select(_(30100), labels)
    if idx < 0:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    chosen = radios[idx]
    _set_saved_url_from_discovery(chosen)
    xbmcgui.Dialog().notification(
        ADDON.getAddonInfo("name"), chosen.display_name(), xbmcgui.NOTIFICATION_INFO
    )
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, updateListing=True)
    xbmc.executebuiltin("Container.Refresh")


def connect_manual():
    try:
        _connect_with_progress()
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"), "OK", xbmcgui.NOTIFICATION_INFO
        )
    except Exception:
        pass
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True)


# ---------------- modes / browse ----------------

def list_modes():
    xbmcplugin.setPluginCategory(HANDLE, _(30102))
    xbmcplugin.setContent(HANDLE, "files")
    try:
        client = _connect_with_progress()
        modes = client.get_modes()
    except Exception:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    for mode in modes:
        li = xbmcgui.ListItem(label=mode.label)
        li.setArt({"icon": "DefaultMusicGenres.png"})
        url = build_url(action="browse", mode_key=mode.key, mode_label=mode.label)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


def browse(mode_key: str, mode_label: str, nav_key: str = None, is_root: bool = True):
    """
    Przeglada zawartosc trybu (Internet Radio / DAB / Podcasty / FM...),
    odzwierciedlajac logike enter_folder_or_play() z radio_service.py:
    najpierw probujemy wejsc jako folder, w razie niepowodzenia traktujemy
    element jako pozycje do odtworzenia.
    """
    xbmcplugin.setPluginCategory(HANDLE, mode_label)
    xbmcplugin.setContent(HANDLE, "files")

    try:
        client = _connect_with_progress()
        if is_root:
            client.set_mode(int(mode_key))
        if nav_key is not None:
            client.nav_select_folder(int(nav_key))
        entries = client.nav_list()
    except Exception:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not entries:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"), _(30110), xbmcgui.NOTIFICATION_INFO
        )

    if not is_root or nav_key is not None:
        li = xbmcgui.ListItem(label=f".. {_(30111)}")
        url = build_url(action="nav_up", mode_key=mode_key, mode_label=mode_label)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    for entry in entries:
        li = xbmcgui.ListItem(label=entry.name)
        if entry.graphic_url:
            li.setArt({"thumb": entry.graphic_url, "icon": entry.graphic_url})

        if entry.is_folder:
            url = build_url(
                action="browse",
                mode_key=mode_key,
                mode_label=mode_label,
                nav_key=entry.key,
                is_root="0",
            )
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        else:
            li.setInfo("music", {"title": entry.name})
            url = build_url(
                action="play_item",
                mode_key=mode_key,
                item_key=entry.key,
            )
            # WAZNE: to NIE jest playable media (radio gra fizycznie na
            # urzadzeniu, nie w Kodi) - dlatego isFolder=True, a nie
            # IsPlayable+setResolvedUrl. Kliknieciem tylko wykonujemy
            # komende FSAPI i wracamy do listy; Kodi nigdy nie wchodzi
            # w sciezke playera, wiec nie ma zawieszajacego sie dialogu
            # "info"/bufora czekajacego na strumien, ktorego i tak nie ma.
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)
    try:
        client = _connect_with_progress()
        client.nav_select_parent_folder()
        entries = client.nav_list()
    except Exception:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    xbmcplugin.setPluginCategory(HANDLE, mode_label)
    xbmcplugin.setContent(HANDLE, "files")

    li = xbmcgui.ListItem(label=f".. {_(30111)}")
    url = build_url(action="nav_up", mode_key=mode_key, mode_label=mode_label)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    for entry in entries:
        li = xbmcgui.ListItem(label=entry.name)
        if entry.graphic_url:
            li.setArt({"thumb": entry.graphic_url, "icon": entry.graphic_url})
        if entry.is_folder:
            url = build_url(
                action="browse",
                mode_key=mode_key,
                mode_label=mode_label,
                nav_key=entry.key,
                is_root="0",
            )
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        else:
            li.setProperty("IsPlayable", "true")
            li.setInfo("music", {"title": entry.name})
            url = build_url(action="play_item", mode_key=mode_key, item_key=entry.key)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def play_item(mode_key: str, item_key: str):
    """
    Odtwarza wybrany element: nav_select_item() przelacza radio na dany
    strumien (tak jak enter_folder_or_play() w radio_service.py w
    galezi 'except' - element nie jest folderem). Kodi playback samego
    dzwieku odbywa sie fizycznie NA RADIU (to zdalny sprzet, nie
    strumieniowanie audio do Kodi), wiec zwracamy pusty/potwierdzajacy
    ListItem zamiast URL strumienia.
    """
    try:
        client = _connect_with_progress()
        client.set_mode(int(mode_key))
        client.nav_select_item(int(item_key))
        now = client.get_now_playing()
    except Exception:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    label = now.station_name or now.text or "FSRadio"
    xbmcgui.Dialog().notification(
        ADDON.getAddonInfo("name"), label, xbmcgui.NOTIFICATION_INFO
    )
    # Nie ma lokalnego strumienia audio do odtworzenia w Kodi - radio gra
    # samo, fizycznie. Zwracamy "false", aby Kodi nie probowalo bufora,
    # ale UI juz pokazal potwierdzenie zmiany zrodla.
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


# ---------------- presets ----------------

def list_presets():
    xbmcplugin.setPluginCategory(HANDLE, _(30103))
    xbmcplugin.setContent(HANDLE, "songs")

    try:
        client = _connect_with_progress()
        presets = client.get_presets()
    except Exception:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not presets:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo("name"), _(30109), xbmcgui.NOTIFICATION_INFO
        )

    for preset in presets:
        li = xbmcgui.ListItem(label=preset.name)
        li.setProperty("IsPlayable", "true")
        li.setInfo("music", {"title": preset.name})
        url = build_url(action="play_preset", preset_key=preset.key)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def play_preset(preset_key: str):
    try:
        client = _connect_with_progress()
        client.select_preset(int(preset_key))
        now = client.get_now_playing()
    except Exception:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    label = now.station_name or now.text or "FSRadio"
    xbmcgui.Dialog().notification(
        ADDON.getAddonInfo("name"), label, xbmcgui.NOTIFICATION_INFO
    )
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


# ---------------- now playing ----------------

def show_now_playing():
    try:
        client = _connect_with_progress()
        now = client.get_now_playing()
        vol = client.get_volume()
    except Exception:
        return

    lines = [
        f"{_(30104)}:",
        f"  {now.station_name or '-'}",
        f"  {now.artist or ''}",
        f"  {now.text or ''}",
        f"  Status: {now.status or '-'}",
        f"  Volume: {vol}",
    ]
    xbmcgui.Dialog().textviewer(ADDON.getAddonInfo("name"), "\n".join(lines))


# ---------------- router ----------------

def router():
    params = get_params()
    action = params.get("action")

    if action is None:
        list_root()
    elif action == "discover":
        discover()
    elif action == "connect":
        connect_manual()
    elif action == "modes":
        list_modes()
    elif action == "browse":
        browse(
            mode_key=params.get("mode_key"),
            mode_label=params.get("mode_label", ""),
            nav_key=params.get("nav_key"),
            is_root=(params.get("is_root", "1") == "1"),
        )
    elif action == "nav_up":
        nav_up(mode_key=params.get("mode_key"), mode_label=params.get("mode_label", ""))
    elif action == "play_item":
        play_item(mode_key=params.get("mode_key"), item_key=params.get("item_key"))
    elif action == "presets":
        list_presets()
    elif action == "play_preset":
        play_preset(preset_key=params.get("preset_key"))
    elif action == "nowplaying":
        show_now_playing()
    else:
        list_root()


if __name__ == "__main__":
    router()
