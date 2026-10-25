"""
Прокси для приложения-плеера на Render.

Что умеет:
  GET /lyrics/get|search   -> lrclib.net/api/...   (тексты песен)
  GET /lastfm/similar      -> Last.fm artist.getsimilar (похожие исполнители)
  GET /yandex/artist?name= -> карточка исполнителя из Яндекс Музыки
  GET /yandex/artist/{id}/releases -> альбомы и синглы исполнителя
  GET /yandex/album/{id}/tracks    -> треки альбома
  GET /yandex/new-releases/tracks  -> треки новинок (для плейлиста)
  GET /health              -> проверка, что сервис жив

Переменные окружения (Render -> Environment):
  LASTFM_API_KEY  ключ Last.fm (обязателен для /lastfm/similar)
  API_KEY         необязательный пароль; если задан, приложение должно
                  присылать его в заголовке X-Api-Key
  YANDEX_TOKEN    токен Яндекс Музыки (нужен для /yandex/...)
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from yandex_music import Client

app = FastAPI()

LRCLIB_BASE = "https://lrclib.net/api/"

# Через прокси разрешены только эти пути lrclib.
LRCLIB_ALLOWED = {"get", "search"}

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()

API_KEY = os.environ.get("API_KEY", "").strip()

session = requests.Session()

session.headers.update({"User-Agent": "MusicPlayerApp/1.0"})


def forbidden(request: Request):

    # Пароль проверяется, только если он задан.
    if API_KEY and request.headers.get("x-api-key", "") != API_KEY:

        return JSONResponse(
            status_code=403,
            content={"success": False, "error": "Forbidden"}
        )

    return None


@app.api_route("/health", methods=["GET", "HEAD"])
def health():

    return {"ok": True}


# Бесплатный Render усыпляет сервис после 15 минут без запросов.
# Пока сервис проснулся, он сам раз в 10 минут обращается к своему
# публичному адресу (RENDER_EXTERNAL_URL задаёт сам Render), и это
# считается обычным входящим запросом: сервис не засыпает.
# Если вдруг уснёт, его разбудит первый запрос из приложения
# (приложение пингует /health при каждом запуске). Надёжнее всего
# дополнительно завести бесплатный монитор (UptimeRobot) на /health.
KEEP_ALIVE_SECONDS = 600


def keep_alive():

    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

    if not url:
        return

    while True:

        time.sleep(KEEP_ALIVE_SECONDS)

        try:

            requests.get(url + "/health", timeout=20)

        except Exception as e:

            print("KEEP ALIVE ERROR:", repr(e))


@app.on_event("startup")
def start_keep_alive():

    threading.Thread(
        target=keep_alive,
        daemon=True
    ).start()


@app.get("/lyrics/{path}")
def lyrics(path: str, request: Request):

    denied = forbidden(request)

    if denied:
        return denied

    if path not in LRCLIB_ALLOWED:

        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Unknown path"}
        )

    try:

        upstream = session.get(
            LRCLIB_BASE + path,
            params=dict(request.query_params),
            timeout=15
        )

    except Exception as e:

        print("LYRICS ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": "lrclib failed"}
        )

    headers = {}

    # Отличаем "текста нет" (404 от lrclib) от 404 самого Render.
    if upstream.status_code == 404:

        headers["X-Lrclib-Status"] = "404"

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get(
            "Content-Type",
            "application/json"
        ),
        headers=headers
    )


@app.get("/lastfm/similar")
def lastfm_similar(
    request: Request,
    artist: str = Query(...),
    limit: int = Query(20)
):

    denied = forbidden(request)

    if denied:
        return denied

    if not LASTFM_API_KEY:

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "LASTFM_API_KEY is not set"
            }
        )

    try:

        upstream = session.get(
            LASTFM_URL,
            params={
                "method": "artist.getsimilar",
                "artist": artist,
                "autocorrect": 1,
                "limit": max(1, min(limit, 100)),
                "api_key": LASTFM_API_KEY,
                "format": "json"
            },
            timeout=15
        )

    except Exception as e:

        print("LASTFM ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": "Last.fm failed"}
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json"
    )


# ==================================================
# ЯНДЕКС МУЗЫКА (библиотека yandex_music)
# ==================================================

YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "").strip()

_yandex_client = None


def get_yandex():

    # Клиент создаётся при первом обращении: если токена нет или
    # Яндекс не отвечает, остальной сервер продолжает работать.
    global _yandex_client

    if _yandex_client is None and YANDEX_TOKEN:

        try:

            _yandex_client = Client(YANDEX_TOKEN).init()

            print("Яндекс Музыка подключена")

        except Exception as e:

            print("Ошибка подключения Яндекс Музыки:", repr(e))

    return _yandex_client


def yandex_unavailable():

    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "error": "YANDEX_TOKEN не задан или Яндекс не отвечает"
        }
    )


def ym_cover(uri, size="600x600"):

    if not uri:
        return None

    return "https://" + uri.replace("%%", size)


def ym_norm(text):

    return re.sub(r"[\W_]+", "", (text or "").lower())


def ym_artist_names(artists):

    return ", ".join(
        a.name for a in (artists or []) if getattr(a, "name", None)
    )


@app.get("/yandex/artist")
def yandex_artist(
    request: Request,
    name: str = Query(..., min_length=1)
):

    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    try:

        result = client.search(name, type_="artist")

        found = getattr(
            getattr(result, "artists", None),
            "results",
            None
        ) or []

        if not found:

            return {"success": False, "error": "Не найден"}

        wanted = ym_norm(name)

        artist = next(
            (a for a in found if ym_norm(a.name) == wanted),
            found[0]
        )

        cover = getattr(artist, "cover", None)

        return {
            "success": True,
            "artist": {
                "id": artist.id,
                "name": artist.name,
                "cover_url": ym_cover(
                    getattr(cover, "uri", None),
                    "600x600"
                ),
                "exact": ym_norm(artist.name) == wanted
            }
        }

    except Exception as e:

        print("YANDEX ARTIST ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )


@app.get("/yandex/artists/search")
def yandex_artists_search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(15, ge=1, le=40)
):

    # Исполнители в том порядке, в котором их выдаёт Яндекс Музыка.
    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    try:

        result = client.search(q, type_="artist")

        found = getattr(
            getattr(result, "artists", None),
            "results",
            None
        ) or []

        artists = []

        for artist in found[:limit]:

            cover = getattr(artist, "cover", None)

            artists.append({
                "id": artist.id,
                "name": artist.name,
                "cover_url": ym_cover(
                    getattr(cover, "uri", None),
                    "400x400"
                )
            })

        return {"success": True, "artists": artists}

    except Exception as e:

        print("YANDEX ARTISTS SEARCH ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )


def ym_collect_albums(fetch, also, seen, out):

    # Постранично собирает альбомы исполнителя (до 4 страниц по 50).
    for page in range(4):

        result = fetch(page)

        items = getattr(result, "albums", None) or []

        for album in items:

            key = ym_norm(album.title)

            if album.id is None or (key, also) in seen:
                continue

            seen.add((key, also))

            release_date = (
                getattr(album, "release_date", None) or ""
            )[:10]

            album_type = getattr(album, "type", None) or ""

            out.append({
                "id": album.id,
                "title": album.title,
                "year": getattr(album, "year", None),
                "release_date": release_date,
                "cover_url": ym_cover(
                    getattr(album, "cover_uri", None)
                ),
                "track_count": getattr(album, "track_count", 0) or 0,
                "kind": "single" if album_type == "single" else "album",
                "also": also,
                "artists": ym_artist_names(
                    getattr(album, "artists", None)
                )
            })

        pager = getattr(result, "pager", None)

        total = getattr(pager, "total", 0) or 0

        if not items or (page + 1) * 50 >= total:
            break


@app.get("/yandex/artist/{artist_id}/releases")
def yandex_artist_releases(artist_id: int, request: Request):

    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    albums = []

    seen = set()

    try:

        # Собственные релизы исполнителя.
        ym_collect_albums(
            lambda page: client.artists_direct_albums(
                artist_id,
                page=page,
                page_size=50,
                sort_by="year"
            ),
            False,
            seen,
            albums
        )

    except Exception as e:

        print("YANDEX RELEASES ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )

    try:

        # Релизы, где исполнитель участвует (синглы с другими
        # авторами). Не у всех версий библиотеки есть этот метод.
        ym_collect_albums(
            lambda page: client.artists_also_albums(
                artist_id,
                page=page,
                page_size=50,
                sort_by="year"
            ),
            True,
            seen,
            albums
        )

    except Exception as e:

        print("YANDEX ALSO ALBUMS:", repr(e))

    return {"success": True, "albums": albums}


def ym_album_tracks(client, album_id):

    album = client.albums_with_tracks(album_id)

    tracks = []

    for volume in getattr(album, "volumes", None) or []:

        for track in volume:

            title = track.title or ""

            version = getattr(track, "version", None)

            tracks.append({
                "id": track.id,
                "title": f"{title} ({version})" if version else title,
                "title_short": title,
                "artists": ym_artist_names(
                    getattr(track, "artists", None)
                ),
                "duration": int(
                    (getattr(track, "duration_ms", 0) or 0) / 1000
                )
            })

    return album, tracks


# Треки всех релизов исполнителя одним запросом: приложение просит это
# один раз вместо отдельного запроса на каждый альбом. Результат
# хранится в памяти.
_release_tracks_cache = {}

RELEASE_TRACKS_CACHE_SECONDS = 30 * 60


@app.get("/yandex/artist/{artist_id}/release-tracks")
def yandex_artist_release_tracks(artist_id: int, request: Request):

    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    cached = _release_tracks_cache.get(artist_id)

    if (
        cached
        and time.time() - cached[0] < RELEASE_TRACKS_CACHE_SECONDS
    ):

        return {"success": True, "albums": cached[1]}

    albums = []

    seen = set()

    try:

        ym_collect_albums(
            lambda page: client.artists_direct_albums(
                artist_id,
                page=page,
                page_size=50,
                sort_by="year"
            ),
            False,
            seen,
            albums
        )

    except Exception as e:

        print("YANDEX RELEASE TRACKS ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )

    try:

        ym_collect_albums(
            lambda page: client.artists_also_albums(
                artist_id,
                page=page,
                page_size=50,
                sort_by="year"
            ),
            True,
            seen,
            albums
        )

    except Exception as e:

        print("YANDEX ALSO ALBUMS:", repr(e))

    def load(album):

        try:

            _, tracks = ym_album_tracks(client, album["id"])

            return str(album["id"]), tracks

        except Exception as e:

            print("RELEASE TRACKS ALBUM ERROR:", album["id"], repr(e))

            return str(album["id"]), None

    with ThreadPoolExecutor(max_workers=8) as pool:

        results = list(pool.map(load, albums[:80]))

    data = {key: tracks for key, tracks in results if tracks is not None}

    if data:

        _release_tracks_cache[artist_id] = (time.time(), data)

    return {"success": True, "albums": data}


@app.get("/yandex/album/{album_id}/tracks")
def yandex_album_tracks(album_id: int, request: Request):

    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    try:

        _, tracks = ym_album_tracks(client, album_id)

        return {"success": True, "tracks": tracks}

    except Exception as e:

        print("YANDEX ALBUM ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )


# Треки новинок собираются долго (запрос на каждый альбом), поэтому
# результат хранится в памяти несколько часов.
_new_cache = {"at": 0.0, "data": []}

NEW_CACHE_SECONDS = 3 * 60 * 60


@app.get("/yandex/new-releases/tracks")
def yandex_new_release_tracks(
    request: Request,
    limit: int = Query(60, ge=1, le=200),
    per_album: int = Query(2, ge=1, le=5)
):

    denied = forbidden(request)

    if denied:
        return denied

    client = get_yandex()

    if client is None:
        return yandex_unavailable()

    now = time.time()

    if (
        _new_cache["data"]
        and now - _new_cache["at"] < NEW_CACHE_SECONDS
    ):

        return {"success": True, "tracks": _new_cache["data"][:limit]}

    try:

        releases = client.new_releases()

        album_ids = list(releases.new_releases or [])[:40]

        def load(album_id):

            try:

                album, tracks = ym_album_tracks(client, album_id)

                # Обложка в высоком качестве (1000x1000).
                cover = ym_cover(
                    getattr(album, "cover_uri", None),
                    "1000x1000"
                )

                return [
                    {
                        "title": t["title_short"],
                        "artists": t["artists"],
                        "album": album.title,
                        "cover_url": cover,
                        "duration": t["duration"]
                    }
                    for t in tracks[:per_album]
                ]

            except Exception as e:

                print("NEW RELEASE ALBUM ERROR:", album_id, repr(e))

                return []

        with ThreadPoolExecutor(max_workers=6) as pool:

            parts = list(pool.map(load, album_ids))

        data = [track for part in parts for track in part]

        if data:

            _new_cache["at"] = now

            _new_cache["data"] = data

        return {"success": True, "tracks": data[:limit]}

    except Exception as e:

        print("YANDEX NEW RELEASES ERROR:", repr(e))

        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(e)}
        )
