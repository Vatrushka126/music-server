import json
import os
import uvicorn
import cloudscraper
import certifi

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="Hitmo Music Streamer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://hitmos.pro"


@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "Music server is running"
    }


@app.get("/search")
def search_music(q: str):

    if not q:
        raise HTTPException(
            status_code=400,
            detail="Параметр запроса 'q' обязателен"
        )

    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "darwin",
            "mobile": False
        }
    )

    search_url = f"{BASE_URL}/search?q={q.replace(' ', '+')}"

    try:

        response = scraper.get(
            search_url,
            timeout=20,
            verify=certifi.where(),
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": "https://hitmos.pro/",
                "Upgrade-Insecure-Requests": "1"
            }
        )

        print("FINAL URL:", response.url)
        print("STATUS:", response.status_code)
        print("HEADERS:", dict(response.headers))

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Hitmo ответил ошибкой: {response.status_code}"
            )

        html_content = response.content.decode(
            "utf-8",
            errors="ignore"
        )

        soup = BeautifulSoup(
            html_content,
            "html.parser"
        )

        tracks_data = []

        tracks = (
            soup.find_all("li", class_="tracks__item")
            or soup.find_all("div", class_="track-item")
        )

        for track in tracks:

            try:

                title_el = (
                    track.find(
                        "div",
                        class_="track__title"
                    )
                    or track.find(
                        "div",
                        class_="track-title"
                    )
                )

                desc_el = (
                    track.find(
                        "div",
                        class_="track__desc"
                    )
                    or track.find(
                        "div",
                        class_="track-desc"
                    )
                )

                link_el = (
                    track.find(
                        "a",
                        class_="track__download-btn"
                    )
                    or track.find(
                        "a",
                        class_="download-btn"
                    )
                )

                if title_el and link_el:

                    title = title_el.get_text().strip()

                    artist = (
                        desc_el.get_text().strip()
                        if desc_el
                        else "Неизвестный исполнитель"
                    )

                    mp3_url = link_el.get("href")

                    if not mp3_url:
                        continue

                    if mp3_url.startswith("/"):
                        mp3_url = BASE_URL + mp3_url

                    tracks_data.append({
                        "title": title,
                        "artist": artist,
                        "stream_url": mp3_url
                    })

            except Exception:
                continue

        output_data = {
            "success": True,
            "results": tracks_data
        }

        json_bytes = json.dumps(
            output_data,
            ensure_ascii=False
        ).encode("utf-8")

        return Response(
            content=json_bytes,
            media_type="application/json; charset=utf-8"
        )

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Внутренняя ошибка парсера: {str(e)}"
        )


if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )
