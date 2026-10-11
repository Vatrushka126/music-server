import os
import re
import json
import time
import threading
import base64

from html import unescape
from urllib.parse import urljoin, urlparse, quote

import certifi
import requests
import cloudscraper

from bs4 import BeautifulSoup

from fastapi import FastAPI, Query, Request
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import uvicorn


# ============================================================
# URL
# ============================================================

HITMO_URL = "https://hitmos.pro"
HITMOZ_URL = "https://ru.hitmoz.org"
LIGHTAUDIO_URL = "https://web.ligaudio.ru"
MUZOFOND_URL = "https://muzofond.fm"
MUZPAB_URL = "https://muzpab.xn--41a.ws"

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org"

DEEZER_URL = "https://api.deezer.com"

MUSIC_HITMO_URL = "https://music.hitmo.net"
HITMO_STREAM_URL = "https://d1.hitmo.net/"

BASE_URL = "https://music-server-za5e.onrender.com"


COVER_PROXY = BASE_URL + "/cover?url="


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Music API",
    version="1.0.4"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# HELPERS
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = unescape(str(value))

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def safe_int(
    value,
    default=0
):

    try:
        return int(value)

    except Exception:
        return default


def normalize_album_title(
    title
):

    title = clean_text(
        title
    )

    title = re.sub(
        r"\s*\[(?:album|ep|single)\]\s*$",
        "",
        title,
        flags=re.IGNORECASE
    )

    return title.strip()


def make_cover_proxy(
    url
):

    if not url:
        return None

    return COVER_PROXY + quote(
        url,
        safe=""
    )


# ============================================================
# COVER URL
# ============================================================

def extract_cover_url(
    item,
    meta,
    base_url
):

    cover_url = ""

    # 1. Пробуем взять обложку непосредственно из data-musmeta

    if isinstance(meta, dict):

        for key in (
            "cover_url",
            "cover",
            "image",
            "image_url",
            "poster",
            "thumbnail",
            "thumb",
            "art",
            "artwork"
        ):

            value = clean_text(
                meta.get(key)
            )

            if value:

                cover_url = value

                break

    # 2. Если в data-musmeta нет — ищем картинку в карточке

    if not cover_url:

        image = item.select_one(
            "img"
        )

        if image:

            for attribute in (
                "data-src",
                "data-original",
                "data-lazy-src",
                "src"
            ):

                value = clean_text(
                    image.get(
                        attribute,
                        ""
                    )
                )

                if value:

                    cover_url = value

                    break

    # 3. Иногда картинка лежит в background-image

    if not cover_url:

        element = item.select_one(
            "[style*='background-image']"
        )

        if element:

            style = element.get(
                "style",
                ""
            )

            match = re.search(
                r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)",
                style,
                flags=re.IGNORECASE
            )

            if match:

                cover_url = clean_text(
                    match.group(1)
                )

    if not cover_url:

        return None

    # 4. Убираем мусорные значения

    if cover_url.startswith(
        "data:"
    ):

        return None

    if cover_url.startswith(
        "//"
    ):

        cover_url = "https:" + cover_url

    elif not (
        cover_url.startswith("http://")
        or
        cover_url.startswith("https://")
    ):

        cover_url = urljoin(
            base_url.rstrip("/") + "/",
            cover_url
        )

    # 5. Отдаём картинку через наш proxy

    return make_cover_proxy(
        cover_url
    )


# ============================================================
# STREAM URL
# ============================================================

def normalize_stream_url(
    stream_url,
    base_url
):

    stream_url = clean_text(
        stream_url
    )

    if not stream_url:
        return ""

    # Если URL уже полный — ничего не меняем
    if stream_url.startswith(
        "http://"
    ) or stream_url.startswith(
        "https://"
    ):

        return stream_url

    # Относительный URL превращаем в полный
    return urljoin(
        base_url.rstrip("/") + "/",
        stream_url
    )


# ============================================================
# SCRAPER
# ============================================================

def create_scraper():

    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "darwin",
            "desktop": True
        }
    )


scraper = create_scraper()


def get_headers(
    referer=None
):

    headers = {

        "User-Agent": (
            "Mozilla/5.0 "
            "(Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 "
            "Safari/537.36"
        ),

        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;"
            "q=0.9,"
            "image/avif,"
            "image/webp,"
            "*/*;q=0.8"
        ),

        "Accept-Language": (
            "ru-RU,ru;q=0.9,"
            "en-US;q=0.8,"
            "en;q=0.7"
        )
    }

    if referer:

        headers["Referer"] = referer

    return headers


# ============================================================
# MUSICBRAINZ SESSION
# ============================================================

musicbrainz_session = requests.Session()

# ВАЖНО: MusicBrainz просит в User-Agent реальный контакт.
# Замените you@example.com на свой email или URL проекта.
musicbrainz_session.headers.update({

    "User-Agent": (
        "MusicAggregator/1.0 "
        "(you@example.com)"
    ),

    "Accept": "application/json"
})

musicbrainz_lock = threading.Lock()

musicbrainz_last_request = 0.0


# ============================================================
# MUSICBRAINZ REQUEST
# ============================================================

def musicbrainz_get(
    path,
    params=None,
    timeout=(15, 25)
):

    global musicbrainz_last_request

    url = MUSICBRAINZ_URL + path

    max_attempts = 2

    total_deadline = 40

    started_at = time.time()

    for attempt in range(
        1,
        max_attempts + 1
    ):

        if (
            attempt > 1
            and
            time.time() - started_at
            > total_deadline
        ):

            print(
                "MUSICBRAINZ DEADLINE EXCEEDED"
            )

            break

        with musicbrainz_lock:

            now = time.time()

            wait_time = (
                1.1 -
                (
                    now -
                    musicbrainz_last_request
                )
            )

            if wait_time > 0:

                time.sleep(
                    wait_time
                )

            print()
            print("----------------------------------------")
            print("MUSICBRAINZ REQUEST")
            print("ATTEMPT:", attempt)
            print("URL:", url)
            print("PARAMS:", params)
            print("----------------------------------------")
            print("CERTIFI PATH:", certifi.where())
            print("MUSICBRAINZ HOST:", url)
            print("SSL VERIFY:", True)

            try:

                response = (
                    musicbrainz_session.get(
                        url,
                        params=params or {},
                        timeout=timeout,
                        verify=certifi.where()
                    )
                )

                musicbrainz_last_request = (
                    time.time()
                )

                print(
                    "MUSICBRAINZ STATUS:",
                    response.status_code
                )

                print(
                    "MUSICBRAINZ FINAL URL:",
                    response.url
                )

                if response.status_code == 200:

                    return response

                print(
                    "MUSICBRAINZ RESPONSE:",
                    response.text[:500]
                )

            except requests.exceptions.Timeout as e:

                musicbrainz_last_request = (
                    time.time()
                )

                print(
                    "MUSICBRAINZ TIMEOUT:",
                    repr(e)
                )

            except requests.exceptions.RequestException as e:

                musicbrainz_last_request = (
                    time.time()
                )

                print(
                    "MUSICBRAINZ REQUEST ERROR:",
                    repr(e)
                )

            except Exception as e:

                musicbrainz_last_request = (
                    time.time()
                )

                print(
                    "MUSICBRAINZ UNKNOWN ERROR:",
                    repr(e)
                )

        if attempt < max_attempts:

            delay = attempt

            print(
                "MUSICBRAINZ RETRY IN:",
                delay,
                "SECONDS"
            )

            time.sleep(
                delay
            )

    print(
        "MUSICBRAINZ ALL ATTEMPTS FAILED"
    )

    return None


# ============================================================
# ALBUM CACHE
# ============================================================

ALBUM_CACHE = {}

ALBUM_CACHE_LOCK = threading.Lock()

ALBUM_CACHE_TTL = 3600


def get_cached_albums(
    artist_name
):

    key = clean_text(
        artist_name
    ).lower()

    if not key:
        return None

    with ALBUM_CACHE_LOCK:

        item = ALBUM_CACHE.get(
            key
        )

        if not item:
            return None

        if (
            time.time()
            -
            item["time"]
            >
            ALBUM_CACHE_TTL
        ):

            del ALBUM_CACHE[key]

            return None

        results = item["results"]

        if not results:

            del ALBUM_CACHE[key]

            return None

        return results


def set_cached_albums(
    artist_name,
    results
):

    if not results:
        return

    key = clean_text(
        artist_name
    ).lower()

    if not key:
        return

    with ALBUM_CACHE_LOCK:

        ALBUM_CACHE[key] = {

            "time": time.time(),

            "results": results
        }


# ============================================================
# MUSICBRAINZ ALBUM LIST
# ============================================================

def get_musicbrainz_artist_albums(
    artist_name
):

    artist_name = clean_text(
        artist_name
    )

    if not artist_name:
        return []

    cached = get_cached_albums(
        artist_name
    )

    if cached:

        print(
            "ALBUMS FROM CACHE:",
            artist_name
        )

        return cached

    print("========================================")
    print("MUSICBRAINZ ALBUM SEARCH")
    print("ARTIST:", artist_name)
    print("========================================")

    safe_name = (
        artist_name
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    query = f'artist:"{safe_name}"'

    # Короткий таймаут: если MusicBrainz тормозит,
    # быстрее включится запасной источник (Deezer).
    response = musicbrainz_get(
        "/release-group/",
        params={
            "query": query,
            "fmt": "json",
            "limit": 20
        },
        timeout=(5, 10)
    )

    if response is None:

        print(
            "MUSICBRAINZ RESPONSE IS NONE"
        )

        return None

    try:

        data = response.json()

    except Exception as e:

        print(
            "MUSICBRAINZ JSON ERROR:",
            repr(e)
        )

        print(
            "RESPONSE:",
            response.text[:1000]
        )

        return None

    release_groups = data.get(
        "release-groups",
        []
    )

    print(
        "MUSICBRAINZ RELEASE GROUPS:",
        len(release_groups)
    )

    results = []

    for group in release_groups:

        if not isinstance(
            group,
            dict
        ):
            continue

        group_id = clean_text(
            group.get("id")
        )

        title = clean_text(
            group.get("title")
        )

        primary_type = clean_text(
            group.get(
                "primary-type"
            )
        )

        first_release_date = clean_text(
            group.get(
                "first-release-date"
            )
        )

        print(
            "GROUP:",
            title,
            "| TYPE:",
            primary_type,
            "| ID:",
            group_id
        )

        if primary_type not in (
            "Album",
            "EP"
        ):
            continue

        if not group_id or not title:
            continue

        year = ""

        if first_release_date:

            year = first_release_date[:4]

        cover_url = (
            f"{COVER_ART_ARCHIVE_URL}"
            f"/release-group/{group_id}"
            f"/front-250"
        )

        results.append({

            "id": group_id,

            "title": normalize_album_title(
                title
            ),

            "artist": artist_name,

            "year": year,

            "track_count": 0,

            "cover_url": make_cover_proxy(
                cover_url
            ),

            "url": (
                "https://musicbrainz.org/"
                f"release-group/{group_id}"
            )
        })

    results.sort(
        key=lambda x: (
            x.get(
                "year",
                ""
            ) or "0000"
        ),
        reverse=True
    )

    print(
        "FINAL ALBUM COUNT:",
        len(results)
    )

    if results:

        set_cached_albums(
            artist_name,
            results
        )

    return results


# ============================================================
# MUSICBRAINZ ALBUM DETAILS
# ============================================================

def get_musicbrainz_album(
    album_id
):

    album_id = clean_text(
        album_id
    )

    if not album_id:
        return None

    response = musicbrainz_get(
        f"/release-group/{album_id}",
        params={
            "inc": "releases+artist-credits",
            "fmt": "json"
        },
        timeout=(15, 30)
    )

    if response is None:
        return None

    try:

        group = response.json()

    except Exception as e:

        print(
            "MUSICBRAINZ GROUP JSON ERROR:",
            repr(e)
        )

        return None

    title = clean_text(
        group.get("title")
    )

    first_release_date = clean_text(
        group.get(
            "first-release-date"
        )
    )

    year = ""

    if first_release_date:

        year = first_release_date[:4]

    artist_name = ""

    credits = group.get(
        "artist-credit",
        []
    )

    for credit in credits:

        if not isinstance(
            credit,
            dict
        ):
            continue

        name = clean_text(
            credit.get("name")
        )

        if not name:

            artist = credit.get(
                "artist",
                {}
            )

            if isinstance(
                artist,
                dict
            ):

                name = clean_text(
                    artist.get("name")
                )

        artist_name += name

        artist_name += clean_text(
            credit.get(
                "joinphrase"
            )
        )

    cover = (
        f"{COVER_ART_ARCHIVE_URL}"
        f"/release-group/{album_id}"
        f"/front-500"
    )

    releases = group.get(
        "releases",
        []
    )

    if not releases:

        return {

            "id": album_id,

            "title": title,

            "artist": artist_name,

            "year": year,

            "release_id": None,

            "release_title": None,

            "release_date": None,

            "cover_url": make_cover_proxy(
                cover
            ),

            "track_count": 0,

            "tracks": [],

            "url": (
                "https://musicbrainz.org/"
                f"release-group/{album_id}"
            )
        }

    selected_release = None

    for release in releases:

        if not isinstance(
            release,
            dict
        ):
            continue

        status = clean_text(
            release.get(
                "status"
            )
        ).lower()

        if status == "official":

            selected_release = release

            break

    if selected_release is None:

        selected_release = releases[0]

    release_id = clean_text(
        selected_release.get(
            "id"
        )
    )

    release_title = clean_text(
        selected_release.get(
            "title"
        )
    )

    release_date = clean_text(
        selected_release.get(
            "date"
        )
    )

    if not release_id:
        return None

    release_response = musicbrainz_get(
        f"/release/{release_id}",
        params={
            "inc": "recordings+artist-credits",
            "fmt": "json"
        },
        timeout=(15, 30)
    )

    if release_response is None:

        return {

            "id": album_id,

            "title": title,

            "artist": artist_name,

            "year": year,

            "release_id": release_id,

            "release_title": release_title,

            "release_date": release_date,

            "cover_url": make_cover_proxy(
                cover
            ),

            "track_count": 0,

            "tracks": [],

            "url": (
                "https://musicbrainz.org/"
                f"release-group/{album_id}"
            )
        }

    try:

        release_data = (
            release_response.json()
        )

    except Exception as e:

        print(
            "MUSICBRAINZ RELEASE JSON ERROR:",
            repr(e)
        )

        return None

    tracks = []

    for media in release_data.get(
        "media",
        []
    ):

        if not isinstance(
            media,
            dict
        ):
            continue

        disc = media.get(
            "position"
        )

        for track in media.get(
            "tracks",
            []
        ):

            if not isinstance(
                track,
                dict
            ):
                continue

            recording = track.get(
                "recording",
                {}
            )

            if not isinstance(
                recording,
                dict
            ):

                recording = {}

            track_id = clean_text(
                recording.get(
                    "id"
                )
            )

            track_title = clean_text(
                track.get(
                    "title"
                )
                or
                recording.get(
                    "title"
                )
            )

            duration = track.get(
                "length"
            )

            if duration is None:

                duration = recording.get(
                    "length"
                )

            duration = safe_int(
                duration
            )

            track_artist = ""

            track_credits = (
                recording.get(
                    "artist-credit",
                    []
                )
            )

            for credit in track_credits:

                if not isinstance(
                    credit,
                    dict
                ):
                    continue

                name = clean_text(
                    credit.get(
                        "name"
                    )
                )

                if not name:

                    artist = credit.get(
                        "artist",
                        {}
                    )

                    if isinstance(
                        artist,
                        dict
                    ):

                        name = clean_text(
                            artist.get(
                                "name"
                            )
                        )

                track_artist += name

                track_artist += clean_text(
                    credit.get(
                        "joinphrase"
                    )
                )

            if not track_artist:

                track_artist = artist_name

            tracks.append({

                "id": track_id,

                "position": track.get(
                    "position"
                ),

                "disc": disc,

                "title": track_title,

                "artist": track_artist,

                "duration": duration,

                "duration_seconds": (
                    round(
                        duration / 1000
                    )
                    if duration
                    else 0
                ),

                "url": (
                    "https://musicbrainz.org/"
                    f"recording/{track_id}"
                    if track_id
                    else None
                )
            })

    return {

        "id": album_id,

        "title": title,

        "artist": artist_name,

        "year": year,

        "release_id": release_id,

        "release_title": release_title,

        "release_date": release_date,

        "cover_url": make_cover_proxy(
            cover
        ),

        "track_count": len(
            tracks
        ),

        "tracks": tracks,

        "url": (
            "https://musicbrainz.org/"
            f"release-group/{album_id}"
        )
    }


# ============================================================
# DEEZER (запасной источник альбомов)
# ============================================================

DEEZER_PREFIX = "dz_"


def deezer_get(path, params=None):
    try:
        response = requests.get(
            DEEZER_URL + path,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=(5, 10)
        )
    except Exception as e:
        print("DEEZER REQUEST ERROR:", repr(e))
        return None

    if response.status_code != 200:
        print("DEEZER STATUS:", response.status_code)
        return None

    try:
        data = response.json()
    except Exception as e:
        print("DEEZER JSON ERROR:", repr(e))
        return None

    if isinstance(data, dict) and "error" in data:
        print("DEEZER API ERROR:", data["error"])
        return None

    return data


def find_deezer_artist_ids(artist_name):
    data = deezer_get(
        "/search/artist",
        {"q": artist_name, "limit": 25}
    )

    if not data:
        return []

    artists = data.get("data") or []

    exact = [
        a for a in artists
        if clean_text(a.get("name")).casefold()
        == artist_name.casefold()
    ]

    # Сначала профили с большим числом альбомов.
    exact.sort(
        key=lambda a: safe_int(a.get("nb_album")),
        reverse=True
    )

    if exact:
        return [a["id"] for a in exact[:5] if a.get("id")]

    # Точного совпадения нет — берём первого найденного.
    if artists:
        print("DEEZER ARTIST FALLBACK:", artists[0].get("name"))
        return [artists[0].get("id")]

    return []


def _deezer_collect_albums(items, artist_name, results, seen_ids, seen_keys):
    """Добавляет альбомы из списка Deezer в results без дублей."""

    for item in items:
        album_id = item.get("id")
        title = normalize_album_title(item.get("title"))

        if not album_id or not title:
            continue

        if album_id in seen_ids:
            continue

        record_type = clean_text(
            item.get("record_type")
        ).lower()  # album / single / ep / compile

        # Одинаковые по названию и типу релизы (разные издания) схлопываем.
        key = (title.casefold(), record_type)

        if key in seen_keys:
            continue

        seen_ids.add(album_id)
        seen_keys.add(key)

        cover = (
            item.get("cover_big")
            or item.get("cover_medium")
            or item.get("cover")
        )

        results.append({
            "id": f"{DEEZER_PREFIX}{album_id}",
            "title": title,
            "artist": artist_name,
            "year": clean_text(
                item.get("release_date")
            )[:4],
            "type": record_type,
            "track_count": 0,
            "cover_url": make_cover_proxy(cover),
            "url": item.get("link")
        })


def get_deezer_artist_albums(artist_name):
    artist_name = clean_text(artist_name)

    if not artist_name:
        return []

    cache_key = "deezer:" + artist_name

    cached = get_cached_albums(cache_key)

    if cached:
        print("DEEZER ALBUMS FROM CACHE:", artist_name)
        return cached

    artist_ids = find_deezer_artist_ids(artist_name)

    if not artist_ids:
        print("DEEZER ARTIST NOT FOUND:", artist_name)
        return []

    print("DEEZER ARTIST IDS:", artist_ids)

    results = []
    seen_ids = set()
    seen_keys = set()

    # --------------------------------------------------------
    # 1. Релизы с карточки артиста: /artist/{id}/albums
    # --------------------------------------------------------

    for artist_id in artist_ids:

        index = 0

        for _ in range(3):  # до 300 релизов на профиль

            data = deezer_get(
                f"/artist/{artist_id}/albums",
                {"limit": 100, "index": index}
            )

            if not data:
                break

            items = data.get("data") or []

            print(
                "DEEZER /artist/albums:",
                artist_id,
                "| RAW COUNT:",
                len(items),
                "| TOTAL:",
                data.get("total"),
                "| TYPES:",
                sorted({
                    clean_text(i.get("record_type")).lower()
                    for i in items
                })
            )

            _deezer_collect_albums(
                items,
                artist_name,
                results,
                seen_ids,
                seen_keys
            )

            if not data.get("next"):
                break

            index += 100

    # --------------------------------------------------------
    # 2. Дополнительно: поиск альбомов по имени артиста.
    #    Находит альбомы, которых нет в /artist/{id}/albums
    #    (например, если релиз привязан иначе).
    # --------------------------------------------------------

    safe_name = artist_name.replace('"', '')

    search_data = deezer_get(
        "/search/album",
        {"q": f'artist:"{safe_name}"', "limit": 100}
    )

    if search_data:

        found = []

        for item in search_data.get("data") or []:

            item_artist = item.get("artist") or {}

            same_name = (
                clean_text(item_artist.get("name")).casefold()
                == artist_name.casefold()
            )

            same_id = item_artist.get("id") in artist_ids

            if same_name or same_id:
                found.append(item)

        print(
            "DEEZER /search/album:",
            len(search_data.get("data") or []),
            "| MATCHED:",
            len(found)
        )

        # В поиске нет record_type — добираем его из деталей альбома.
        for item in found:

            if item.get("id") in seen_ids:
                continue

            if not item.get("record_type"):

                details = deezer_get(f"/album/{item.get('id')}")

                if details:
                    item = details

            _deezer_collect_albums(
                [item],
                artist_name,
                results,
                seen_ids,
                seen_keys
            )

    results.sort(
        key=lambda x: x.get("year") or "0000",
        reverse=True
    )

    print("DEEZER ALBUM COUNT:", len(results))

    if results:
        set_cached_albums(cache_key, results)

    return results


def get_deezer_album(album_id):
    raw_id = album_id[len(DEEZER_PREFIX):]

    if not raw_id.isdigit():
        return None

    album = deezer_get(f"/album/{raw_id}")

    if not album:
        return None

    tracks_data = deezer_get(
        f"/album/{raw_id}/tracks",
        {"limit": 200}
    )

    album_artist = clean_text(
        (album.get("artist") or {}).get("name")
    )

    tracks = []

    for track in (tracks_data or {}).get("data") or []:
        duration_seconds = safe_int(track.get("duration"))

        tracks.append({
            "id": str(track.get("id")),
            "position": track.get("track_position"),
            "disc": track.get("disk_number"),
            "title": clean_text(track.get("title")),
            "artist": clean_text(
                (track.get("artist") or {}).get("name")
            ) or album_artist,
            "duration": duration_seconds * 1000,
            "duration_seconds": duration_seconds,
            "url": track.get("link")
        })

    cover = (
        album.get("cover_xl")
        or album.get("cover_big")
        or album.get("cover_medium")
    )

    return {
        "id": album_id,
        "title": normalize_album_title(album.get("title")),
        "artist": album_artist,
        "year": clean_text(album.get("release_date"))[:4],
        "release_id": None,
        "release_title": None,
        "release_date": clean_text(album.get("release_date")),
        "cover_url": make_cover_proxy(cover),
        "track_count": len(tracks),
        "tracks": tracks,
        "url": album.get("link")
    }


# ============================================================
# HITMOZ ARTIST SEARCH
# ============================================================

def search_hitmoz_artists(
    query
):

    query = clean_text(
        query
    )

    if not query:
        return []

    try:

        response = scraper.get(
            f"{HITMOZ_URL}/search",
            params={
                "q": query
            },
            headers=get_headers(
                HITMOZ_URL
            ),
            timeout=15
        )

    except Exception as e:

        print(
            "HITMOZ ARTIST ERROR:",
            repr(e)
        )

        return []

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    seen = set()

    for item in soup.select(
        "ul.singers-list.album-list "
        "li.album-item"
    ):

        link = item.select_one(
            "a"
        )

        if not link:
            continue

        href = link.get(
            "href",
            ""
        )

        name = clean_text(
            link.get_text()
        )

        href = urljoin(
            HITMOZ_URL,
            href
        )

        match = re.search(
            r"/artist/(\d+)",
            href
        )

        if not match:
            continue

        artist_id = match.group(1)

        if artist_id in seen:
            continue

        seen.add(
            artist_id
        )

        results.append({

            "id": artist_id,

            "name": name,

            "url": href
        })

    return results


# ============================================================
# HITMOZ ARTIST TRACKS
# ============================================================

def get_hitmoz_artist_tracks(
    artist_id
):

    artist_id = clean_text(
        artist_id
    )

    if not artist_id:
        return []

    results = []

    seen = set()

    page_size = 48

    max_pages = 20

    for page in range(
        max_pages
    ):

        if page == 0:

            url = (
                f"{HITMOZ_URL}"
                f"/artist/{artist_id}"
            )

        else:

            start = (
                page *
                page_size
            )

            url = (
                f"{HITMOZ_URL}"
                f"/artist/{artist_id}"
                f"/start/{start}"
            )

        print(
            "HITMOZ ARTIST:",
            url
        )

        try:

            response = scraper.get(
                url,
                headers=get_headers(
                    HITMOZ_URL
                ),
                timeout=15
            )

        except Exception as e:

            print(
                "HITMOZ TRACK ERROR:",
                repr(e)
            )

            break

        if response.status_code != 200:
            break

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        items = soup.select(
            "[data-musmeta]"
        )

        if not items:
            break

        added = 0

        for item in items:

            raw_meta = item.get(
                "data-musmeta"
            )

            if not raw_meta:
                continue

            try:

                meta = json.loads(
                    raw_meta
                )

            except Exception:

                try:

                    meta = json.loads(
                        unescape(
                            raw_meta
                        )
                    )

                except Exception:

                    continue

            if not isinstance(
                meta,
                dict
            ):
                continue

            title = clean_text(
                meta.get(
                    "title"
                )
                or
                meta.get(
                    "name"
                )
            )

            artist = clean_text(
                meta.get(
                    "artist"
                )
            )

            stream_url = clean_text(
                meta.get(
                    "url"
                )
                or
                meta.get(
                    "stream_url"
                )
                or
                meta.get(
                    "file"
                )
            )

            cover_url = extract_cover_url(
                item,
                meta,
                HITMOZ_URL
            )

            stream_url = normalize_stream_url(
                stream_url,
                HITMOZ_URL
            )

            if not title:
                continue

            key = (
                title.lower(),
                artist.lower(),
                stream_url
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            print(
                "HITMOZ STREAM:",
                stream_url
            )

            results.append({

                "title": title,

                "artist": artist,

                "stream_url": stream_url,

                "cover_url": cover_url,

                "source": "hitmoz"
            })

            added += 1

        if added == 0:
            break

    return results


# ============================================================
# MUSIC.HITMO.NET — ARTIST SEARCH
# ============================================================

def search_music_hitmo_artist(artist_name):
    artist_name = clean_text(artist_name)

    if not artist_name:
        return None

    try:
        response = scraper.get(
            f"{MUSIC_HITMO_URL}/search/{quote(artist_name)}",
            params={"type": "artists"},
            headers=get_headers(MUSIC_HITMO_URL),
            timeout=15
        )
    except Exception as e:
        print("MUSIC.HITMO SEARCH ERROR:", repr(e))
        return None

    if response.status_code != 200:
        print("MUSIC.HITMO SEARCH STATUS:", response.status_code)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    artist_links = soup.select('a[href*="/artists/"]')

    # Сначала ищем точное совпадение имени.
    for link in artist_links:
        href = link.get("href", "")
        displayed_name = clean_text(
            link.get("title") or link.get_text()
        )

        displayed_name = re.sub(
            r"^Артист\s+",
            "",
            displayed_name,
            flags=re.IGNORECASE
        ).strip()

        match = re.search(r"/artists/(\d+)", href)

        if (
            match
            and displayed_name.casefold() == artist_name.casefold()
        ):
            return match.group(1)

    # Если точного совпадения нет, используем первый найденный профиль.
    for link in artist_links:
        match = re.search(
            r"/artists/(\d+)",
            link.get("href", "")
        )

        if match:
            print(
                "MUSIC.HITMO ARTIST FALLBACK:",
                clean_text(link.get("title") or link.get_text())
            )
            return match.group(1)

    print("MUSIC.HITMO ARTIST NOT FOUND:", artist_name)
    return None


# ============================================================
# MUSIC.HITMO.NET — STREAM URL
# ============================================================

def make_music_hitmo_stream_url(
    track_id,
    artist,
    title,
    subtract_offset=False
):
    track_id = clean_text(track_id)
    artist = clean_text(artist)
    title = clean_text(title)

    if not track_id or not artist or not title:
        return ""

    if subtract_offset:
        try:
            track_id = str(
                int(track_id) - 2595762
            )
        except Exception:
            return ""

    path = (
        f"/t/{track_id}/"
        f"{artist} - {title} (hitmo.net).mp3"
    )

    encoded_path = base64.b64encode(
        path.encode("utf-8")
    ).decode("ascii")

    return f"{HITMO_STREAM_URL}{encoded_path}.mp3"


# ============================================================
# MUSIC.HITMO.NET — ARTIST TRACKS
# ============================================================

def get_music_hitmo_artist_tracks(artist_name):
    artist_name = clean_text(artist_name)

    if not artist_name:
        return []

    artist_id = search_music_hitmo_artist(artist_name)

    if not artist_id:
        return []

    artist_url = f"{MUSIC_HITMO_URL}/artists/{artist_id}"

    print("MUSIC.HITMO ARTIST PAGE:", artist_url)

    try:
        response = scraper.get(
            artist_url,
            headers=get_headers(MUSIC_HITMO_URL),
            timeout=20
        )
    except Exception as e:
        print("MUSIC.HITMO TRACKS ERROR:", repr(e))
        return []

    if response.status_code != 200:
        print("MUSIC.HITMO ARTIST STATUS:", response.status_code)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    seen = set()

    for item in soup.select("li"):
        title_link = item.select_one(
            'a.__adv_name[href*="/track/"]'
        )

        if not title_link:
            continue

        match = re.search(
            r"/track/(\d+)",
            title_link.get("href", "")
        )

        if not match:
            continue

        track_id = match.group(1)

        title = clean_text(
            title_link.get("title")
            or title_link.get_text()
        )

        artist_element = item.select_one(".__adv_artist")

        track_artist = clean_text(
            artist_element.get_text()
            if artist_element
            else artist_name
        )

        if not title:
            continue

        # Обложка трека.
        cover_url = None
        image = item.select_one("img")

        if image:
            for attribute in (
                "data-src",
                "data-original",
                "src"
            ):
                value = clean_text(image.get(attribute, ""))

                if value and not value.startswith("data:"):
                    cover_url = value
                    break

        if cover_url:
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url
            elif not cover_url.startswith(("http://", "https://")):
                cover_url = urljoin(
                    MUSIC_HITMO_URL + "/",
                    cover_url
                )

            cover_url = make_cover_proxy(cover_url)

        stream_url = make_music_hitmo_stream_url(
            track_id,
            track_artist,
            title
        )

        key = (track_id, title.casefold())

        if key in seen:
            continue

        seen.add(key)

        results.append({
            "title": title,
            "artist": track_artist,
            "stream_url": stream_url,
            "cover_url": cover_url,
            "source": "music_hitmo",
            "track_id": track_id
        })

    print(
        "MUSIC.HITMO TRACKS FOUND:",
        artist_name,
        len(results)
    )

    return results


# ============================================================
# MUSIC.HITMO.NET — GLOBAL TRACK SEARCH
# ============================================================

def search_music_hitmo(query):
    query = clean_text(query)

    if not query:
        return []

    search_url = (
        f"{MUSIC_HITMO_URL}/search/{quote(query)}"
    )

    print(
        "MUSIC.HITMO SEARCH:",
        search_url
    )

    try:
        response = scraper.get(
            search_url,
            headers=get_headers(MUSIC_HITMO_URL),
            timeout=20
        )
    except Exception as e:
        print(
            "MUSIC.HITMO GLOBAL SEARCH ERROR:",
            repr(e)
        )
        return []

    print(
        "MUSIC.HITMO SEARCH STATUS:",
        response.status_code
    )

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []
    seen = set()

    # Поисковая страница music.hitmo.net
    # содержит треки в div.__adv_list_track
    for item in soup.select(
        "div.__adv_list_track"
    ):

        title_link = item.select_one(
            'a.__adv_name[href*="/track/"]'
        )

        if not title_link:
            continue

        match = re.search(
            r"/track/(\d+)",
            title_link.get("href", "")
        )

        if not match:
            continue

        track_id = match.group(1)

        title = clean_text(
            title_link.get("title")
            or title_link.get_text(
                " ",
                strip=True
            )
        )

        artist_element = item.select_one(
            ".__adv_artist"
        )

        track_artist = clean_text(
            artist_element.get_text(
                " ",
                strip=True
            )
            if artist_element
            else ""
        )

        if not title:
            continue

        cover_url = None

        image = item.select_one("img")

        if image:
            for attribute in (
                "data-src",
                "data-original",
                "data-lazy-src",
                "src"
            ):
                value = clean_text(
                    image.get(
                        attribute,
                        ""
                    )
                )

                if (
                    value
                    and not value.startswith("data:")
                ):
                    cover_url = value
                    break

        if cover_url:

            if cover_url.startswith("//"):
                cover_url = (
                    "https:"
                    + cover_url
                )

            elif not cover_url.startswith(
                (
                    "http://",
                    "https://"
                )
            ):
                cover_url = urljoin(
                    MUSIC_HITMO_URL + "/",
                    cover_url
                )

            cover_url = make_cover_proxy(
                cover_url
            )

        stream_url = make_music_hitmo_stream_url(
            track_id,
            track_artist,
            title
        )

        if not stream_url:
            continue

        key = (
            track_id,
            title.casefold(),
            track_artist.casefold()
        )

        if key in seen:
            continue

        seen.add(key)

        print()
        print("MUSIC.HITMO RESULT:")
        print("TITLE:", title)
        print("ARTIST:", track_artist)
        print("TRACK ID:", track_id)
        print("STREAM:", stream_url)

        results.append({
            "title": title,
            "artist": track_artist,
            "stream_url": stream_url,
            "cover_url": cover_url,
            "source": "music_hitmo",
            "track_id": track_id
        })

    print(
        "MUSIC.HITMO SEARCH RESULTS:",
        len(results)
    )

    return results


# ============================================================
# HITMO SEARCH
# ============================================================

def search_hitmo(
    query
):

    query = clean_text(
        query
    )

    if not query:
        return []

    try:

        response = scraper.get(
            f"{HITMO_URL}/search",
            params={
                "q": query
            },
            headers=get_headers(
                HITMO_URL
            ),
            timeout=15
        )

    except Exception as e:

        print(
            "HITMO SEARCH ERROR:",
            repr(e)
        )

        return []

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    for item in soup.select(
        "[data-musmeta]"
    ):

        raw_meta = item.get(
            "data-musmeta"
        )

        if not raw_meta:
            continue

        try:

            meta = json.loads(
                raw_meta
            )

        except Exception:

            try:

                meta = json.loads(
                    unescape(
                        raw_meta
                    )
                )

            except Exception:

                continue

        if not isinstance(
            meta,
            dict
        ):
            continue

        title = clean_text(
            meta.get(
                "title"
            )
            or
            meta.get(
                "name"
            )
        )

        artist = clean_text(
            meta.get(
                "artist"
            )
        )

        stream_url = clean_text(
            meta.get(
                "url"
            )
            or
            meta.get(
                "stream_url"
            )
            or
            meta.get(
                "file"
            )
        )

        # Получаем обложку Hitmo
        cover_url = extract_cover_url(
            item,
            meta,
            HITMO_URL
        )

        # Исправление относительного URL Hitmo
        stream_url = normalize_stream_url(
            stream_url,
            HITMO_URL
        )

        if not title:
            continue

        print(
            "HITMO STREAM:",
            stream_url
        )

        print(
            "HITMO COVER:",
            cover_url
        )

        results.append({

            "title": title,

            "artist": artist,

            "stream_url": stream_url,

            "cover_url": cover_url,

            "source": "hitmo"
        })

    return results


# ============================================================
# GENERIC SEARCH
# ============================================================

def search_generic_site(
    base_url,
    query,
    source_name
):

    query = clean_text(
        query
    )

    if not query:
        return []

    try:

        response = scraper.get(
            f"{base_url}/search",
            params={
                "q": query
            },
            headers=get_headers(
                base_url
            ),
            timeout=15
        )

    except Exception as e:

        print(
            source_name,
            "ERROR:",
            repr(e)
        )

        return []

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    for item in soup.select(
        "[data-musmeta]"
    ):

        raw_meta = item.get(
            "data-musmeta"
        )

        if not raw_meta:
            continue

        try:

            meta = json.loads(
                raw_meta
            )

        except Exception:

            try:

                meta = json.loads(
                    unescape(
                        raw_meta
                    )
                )

            except Exception:

                continue

        if not isinstance(
            meta,
            dict
        ):
            continue

        title = clean_text(
            meta.get(
                "title"
            )
            or
            meta.get(
                "name"
            )
        )

        artist = clean_text(
            meta.get(
                "artist"
            )
        )

        stream_url = clean_text(
            meta.get(
                "url"
            )
            or
            meta.get(
                "stream_url"
            )
            or
            meta.get(
                "file"
            )
        )

        cover_url = extract_cover_url(
            item,
            meta,
            base_url
        )

        stream_url = normalize_stream_url(
            stream_url,
            base_url
        )

        if not title:
            continue

        results.append({

            "title": title,

            "artist": artist,

            "stream_url": stream_url,

            "cover_url": cover_url,

            "source": source_name
        })

    return results


# ============================================================
# COVER PROXY
# ============================================================

ALLOWED_COVER_HOSTS = {

    "statcore.hitmcdn.com",

    "static.hitmcdn.com",

    "cover.lightaudio.ru",

    "muzofond.fm",

    "www.muzofond.fm",

    "muzpab.xn--41a.ws",

    "avatars.yandex.net",

    "coverartarchive.org",

    "archive.org"
}


@app.get("/cover")
async def cover(
    url: str = Query(...)
):

    if not url:

        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "URL is required"
            }
        )

    try:

        parsed = urlparse(
            url
        )

    except Exception:

        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid URL"
            }
        )

    if parsed.scheme not in (
        "http",
        "https"
    ):

        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid URL scheme"
            }
        )

    hostname = (
        parsed.hostname
        or ""
    ).lower()

    # Обложки Deezer лежат на *.dzcdn.net
    if (
        hostname not in ALLOWED_COVER_HOSTS
        and not hostname.endswith(".dzcdn.net")
    ):

        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "error": "Host is not allowed"
            }
        )

    try:

        response = scraper.get(
            url,
            headers=get_headers(),
            timeout=10
        )

    except Exception as e:

        print(
            "COVER ERROR:",
            repr(e)
        )

        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "error": "Cover request failed"
            }
        )

    if response.status_code != 200:

        return JSONResponse(
            status_code=response.status_code,
            content={
                "success": False,
                "error": "Cover not found"
            }
        )

    content_type = response.headers.get(
        "Content-Type",
        "image/jpeg"
    )

    return Response(
        content=response.content,
        media_type=content_type
    )


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
async def root():

    return {

        "success": True,

        "service": "music-api",

        "version": "1.0.3"
    }


@app.get("/health")
async def health():

    return {
        "success": True
    }


# ============================================================
# GLOBAL SEARCH
# ============================================================

@app.get("/search")
async def search(
    q: str = Query(...)
):

    query = clean_text(
        q
    )

    if not query:

        return {
            "success": True,
            "results": []
        }

    results = []

    results.extend(
        search_hitmo(
            query
        )
    )

    results.extend(
        search_music_hitmo(
            query
        )
    )

    results.extend(
        search_generic_site(
            LIGHTAUDIO_URL,
            query,
            "lightaudio"
        )
    )

    results.extend(
        search_generic_site(
            MUZOFOND_URL,
            query,
            "muzofond"
        )
    )

    results.extend(
        search_generic_site(
            MUZPAB_URL,
            query,
            "muzpab"
        )
    )

    unique = []

    seen = set()

    for item in results:

        key = (

            clean_text(
                item.get(
                    "title"
                )
            ).lower(),

            clean_text(
                item.get(
                    "artist"
                )
            ).lower(),

            clean_text(
                item.get(
                    "stream_url"
                )
            )
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        unique.append(
            item
        )

    return {

        "success": True,

        "results": unique
    }


# ============================================================
# ARTIST SEARCH
# ============================================================

@app.get("/artists")
async def artists(
    q: str = Query(...)
):

    query = clean_text(
        q
    )

    results = search_hitmoz_artists(
        query
    )

    return {

        "success": True,

        "results": results
    }


# ============================================================
# ARTIST TRACKS
# ============================================================

@app.get("/artist/{artist_id}")
async def artist_tracks(
    artist_id: str,
    name: str = Query("")
):
    # Оригинальный источник Hitmoz — оставляем.
    hitmoz_results = get_hitmoz_artist_tracks(artist_id)

    # Имя для поиска на music.hitmo.net.
    artist_name = clean_text(name)

    # Если приложение не передало имя, пробуем получить его
    # из результатов Hitmoz.
    if not artist_name and hitmoz_results:
        artist_name = clean_text(
            hitmoz_results[0].get("artist", "")
        )

        if "," in artist_name:
            artist_name = artist_name.split(",")[0].strip()

    # Добавляем новый источник.
    music_hitmo_results = []

    if artist_name:
        music_hitmo_results = get_music_hitmo_artist_tracks(
            artist_name
        )

    # Объединяем оба источника.
    results = []
    seen = set()

    for item in hitmoz_results + music_hitmo_results:
        key = (
            clean_text(item.get("title")).casefold(),
            clean_text(item.get("artist")).casefold(),
            clean_text(item.get("stream_url"))
        )

        if key in seen:
            continue

        seen.add(key)
        results.append(item)

    return {
        "success": True,
        "results": results
    }


# ============================================================
# ALBUM LIST (MusicBrainz -> запасной вариант Deezer)
# ============================================================

@app.get("/albums")
def albums(
    name: str = Query(...)
):

    artist_name = clean_text(
        name
    )

    if not artist_name:

        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "results": [],
                "error": (
                    "Artist name is required"
                )
            }
        )

    print()
    print("========================================")
    print("ALBUM REQUEST:", artist_name)
    print("========================================")

    results = (
        get_musicbrainz_artist_albums(
            artist_name
        )
    )

    # MusicBrainz не ответил (None) или ничего не нашёл ([]).
    if not results:

        print("FALLBACK TO DEEZER:", artist_name)

        deezer_results = get_deezer_artist_albums(
            artist_name
        )

        if deezer_results:
            results = deezer_results

    # None значит, что MusicBrainz не ответил и Deezer ничего не дал.
    if results is None:

        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "results": [],
                "error": (
                    "MusicBrainz и Deezer недоступны, "
                    "попробуйте позже"
                )
            }
        )

    return {

        "success": True,

        "artist": artist_name,

        "results": results
    }


# ============================================================
# ALBUM DETAILS
# ============================================================

@app.get("/albums/{album_id}")
def album_details(
    album_id: str
):

    if album_id.startswith(DEEZER_PREFIX):
        album = get_deezer_album(album_id)
    else:
        album = get_musicbrainz_album(album_id)

    if album is None:

        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "error": "Album not found"
            }
        )

    return {

        "success": True,

        "album": album
    }


# ============================================================
# ЗАПОМНЕННЫЕ АЛЬБОМЫ
#
# Хранятся не файлы треков, а только описание альбома: название,
# обложка, дата и список ссылок на треки в нужном порядке.
#
# Где хранится:
#  - если заданы переменные S3_BUCKET, S3_KEY_ID и S3_SECRET
#    (Yandex Object Storage), то в файле saved_albums.json в
#    этом бакете. Так данные переживают перезапуск контейнера.
#  - иначе в локальном файле (SAVED_ALBUMS_FILE, по умолчанию
#    /tmp/saved_albums.json). В Serverless Containers он
#    пропадает при перезапуске контейнера.
# ============================================================

SAVED_ALBUMS_LOCK = threading.Lock()

SAVED_ALBUMS_FILE = os.environ.get(
    "SAVED_ALBUMS_FILE",
    "/tmp/saved_albums.json"
)

SAVED_ALBUMS_KEY = "saved_albums.json"

_saved_albums = None


def _s3_client():

    bucket = os.environ.get("S3_BUCKET")

    if not bucket:
        return None, None

    try:

        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=os.environ.get(
                "S3_ENDPOINT",
                "https://storage.yandexcloud.net"
            ),
            aws_access_key_id=os.environ.get("S3_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET"),
            region_name=os.environ.get(
                "S3_REGION",
                "ru-central1"
            )
        )

        return client, bucket

    except Exception as error:

        print("S3 unavailable:", error)

        return None, None


def _load_saved_albums():

    global _saved_albums

    if _saved_albums is not None:
        return _saved_albums

    data = {}

    client, bucket = _s3_client()

    try:

        if client is not None:

            body = client.get_object(
                Bucket=bucket,
                Key=SAVED_ALBUMS_KEY
            )["Body"].read()

            data = json.loads(body.decode("utf-8"))

        elif os.path.exists(SAVED_ALBUMS_FILE):

            with open(SAVED_ALBUMS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

    except Exception as error:

        # Файла ещё нет или он недоступен: начинаем с пустого.
        print("Saved albums load:", error)

        data = {}

    if not isinstance(data, dict):
        data = {}

    _saved_albums = data

    return _saved_albums


def _persist_saved_albums(data):

    payload = json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")

    client, bucket = _s3_client()

    if client is not None:

        client.put_object(
            Bucket=bucket,
            Key=SAVED_ALBUMS_KEY,
            Body=payload,
            ContentType="application/json"
        )

        return

    with open(SAVED_ALBUMS_FILE, "wb") as f:
        f.write(payload)


@app.get("/saved_albums")
def saved_albums_list(
    artist_id: str = Query("")
):

    with SAVED_ALBUMS_LOCK:

        data = _load_saved_albums()

        items = [
            item
            for item in data.values()
            if not artist_id
            or str(
                (item.get("album") or {}).get("artist_id", "")
            ) == artist_id
        ]

    return {
        "success": True,
        "albums": items
    }


@app.get("/saved_albums/{album_id}")
def saved_album_get(
    album_id: str
):

    with SAVED_ALBUMS_LOCK:

        item = _load_saved_albums().get(album_id)

    if item is None:

        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "error": "Not saved"
            }
        )

    return {
        "success": True,
        "album": item
    }


@app.post("/saved_albums")
async def saved_album_save(
    request: Request
):

    try:
        body = await request.json()
    except Exception:
        body = None

    album = (
        body.get("album")
        if isinstance(body, dict)
        else None
    )

    if (
        not isinstance(album, dict)
        or not str(album.get("id", "")).strip()
    ):

        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Bad album"
            }
        )

    album_id = str(album["id"])

    body["saved_at"] = int(time.time())

    with SAVED_ALBUMS_LOCK:

        data = _load_saved_albums()

        data[album_id] = body

        try:

            _persist_saved_albums(data)

        except Exception as error:

            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Save failed: " + str(error)
                }
            )

    return {
        "success": True
    }


@app.delete("/saved_albums/{album_id}")
def saved_album_delete(
    album_id: str
):

    with SAVED_ALBUMS_LOCK:

        data = _load_saved_albums()

        data.pop(album_id, None)

        try:

            _persist_saved_albums(data)

        except Exception as error:

            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Save failed: " + str(error)
                }
            )

    return {
        "success": True
    }


# ============================================================
# CLEAR CACHE
# ============================================================

@app.get("/albums/cache/clear")
async def clear_album_cache_endpoint():

    with ALBUM_CACHE_LOCK:

        ALBUM_CACHE.clear()

    return {

        "success": True,

        "message": "Album cache cleared"
    }


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
