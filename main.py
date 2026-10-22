"""
Прокси для приложения-плеера на Render.

Что умеет:
  GET /lyrics/get|search   -> lrclib.net/api/...   (тексты песен)
  GET /lastfm/similar      -> Last.fm artist.getsimilar (похожие исполнители)
  GET /health              -> проверка, что сервис жив

Переменные окружения (Render -> Environment):
  LASTFM_API_KEY  ключ Last.fm (обязателен для /lastfm/similar)
  API_KEY         необязательный пароль; если задан, приложение должно
                  присылать его в заголовке X-Api-Key
"""

import os
import threading
import time

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

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


@app.get("/health")
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
