from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


app = FastAPI(title="壁纸提取小程序 API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

URL_PATTERN = re.compile(r"https?://[^\s，。；、]+", re.IGNORECASE)
ALLOWED_INPUT_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
TOKEN_TTL_SECONDS = 30 * 60
MEDIA_TOKENS: dict[str, tuple[float, str]] = {}
COOKIE_FILE = Path(__file__).with_name("cookies.txt")
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148"
)


class ParseRequest(BaseModel):
    text: str


def extract_douyin_url(text: str) -> str:
    match = URL_PATTERN.search(text.strip())
    if not match:
        raise HTTPException(status_code=400, detail="没有找到有效链接")

    url = match.group(0).rstrip("!！?？")
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_INPUT_HOSTS and not host.endswith(".douyin.com"):
        raise HTTPException(status_code=400, detail="目前只支持抖音链接")
    return url


def create_media_url(request: Request, remote_url: str) -> str:
    token = uuid.uuid4().hex
    MEDIA_TOKENS[token] = (time.time() + TOKEN_TTL_SECONDS, remote_url)
    filename = "video.mp4" if ".mp4" in remote_url.lower() else "image.jpg"
    return str(request.url_for("media_proxy", token=token, filename=filename))


def normalize_entry(info: dict[str, Any]) -> dict[str, Any]:
    entries = info.get("entries")
    if entries:
        first = next((item for item in entries if item), None)
        if first:
            return first
    return info


def choose_video_url(info: dict[str, Any]) -> str | None:
    for item in info.get("requested_downloads") or []:
        if item.get("url"):
            return item["url"]

    candidates = [
        item
        for item in info.get("formats") or []
        if item.get("url") and item.get("vcodec") not in (None, "none")
    ]
    if candidates:
        candidates.sort(
            key=lambda item: (
                item.get("height") or 0,
                item.get("tbr") or 0,
                item.get("filesize") or item.get("filesize_approx") or 0,
            ),
            reverse=True,
        )
        return candidates[0]["url"]
    return info.get("url")


def extract_images(info: dict[str, Any]) -> list[str]:
    images: list[str] = []
    for key in ("images", "thumbnails"):
        for item in info.get(key) or []:
            url = item if isinstance(item, str) else item.get("url")
            if url and url not in images:
                images.append(url)
    return images


def find_douyin_item(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        item_list = value.get("item_list")
        if isinstance(item_list, list):
            item = next((entry for entry in item_list if isinstance(entry, dict)), None)
            if item:
                return item
        for child in value.values():
            item = find_douyin_item(child)
            if item:
                return item
    elif isinstance(value, list):
        for child in value:
            item = find_douyin_item(child)
            if item:
                return item
    return None


def choose_note_image_url(image: dict[str, Any]) -> str | None:
    urls = image.get("url_list") or []
    jpeg_url = next(
        (
            url
            for url in urls
            if isinstance(url, str)
            and re.search(r"\.(?:jpe?g|png)(?:\?|$)", url, re.IGNORECASE)
        ),
        None,
    )
    return jpeg_url or next((url for url in urls if isinstance(url, str)), None)


async def extract_note_info(url: str) -> dict[str, Any]:
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        match = re.search(r"/(?:note|gallery)/(\d+)", url)
        if not match:
            resolved = await client.get(url, headers=headers)
            resolved.raise_for_status()
            match = re.search(r"/(?:note|gallery)/(\d+)", str(resolved.url))
        if not match:
            raise ValueError("该链接不是可识别的抖音图文作品")

        response = await client.get(
            f"https://www.iesdouyin.com/share/note/{match.group(1)}/",
            headers=headers,
        )
        response.raise_for_status()

    router_match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*</script>",
        response.text,
        re.DOTALL,
    )
    if not router_match:
        raise ValueError("图文页面中没有找到作品数据")

    item = find_douyin_item(json.loads(router_match.group(1)))
    if not item:
        raise ValueError("图文页面中没有找到作品信息")

    image_urls = [
        selected
        for image in item.get("images") or []
        if isinstance(image, dict)
        for selected in [choose_note_image_url(image)]
        if selected
    ]
    author = item.get("author") or {}
    return {
        "id": item.get("aweme_id"),
        "title": item.get("desc") or "抖音图文作品",
        "description": item.get("desc") or "",
        "uploader": author.get("nickname") or "",
        "images": [{"url": image_url} for image_url in image_urls],
        "thumbnail": image_urls[0] if image_urls else None,
    }


async def resolve_douyin_url(url: str) -> str:
    if re.search(r"/(?:note|gallery|video)/\d+", url):
        return url
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        response = await client.head(url, headers=headers)
        location = response.headers.get("location")
        if location:
            return str(httpx.URL(url).join(location))
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return str(response.url)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/parse")
async def parse_share_link(payload: ParseRequest, request: Request) -> dict[str, Any]:
    url = extract_douyin_url(payload.text)
    try:
        resolved_url = await resolve_douyin_url(url)
    except Exception:
        resolved_url = url

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "http_headers": {"User-Agent": DESKTOP_UA},
    }
    if COOKIE_FILE.exists():
        options["cookiefile"] = str(COOKIE_FILE)

    if re.search(r"/(?:note|gallery)/\d+", resolved_url):
        try:
            raw_info = await extract_note_info(resolved_url)
        except Exception as note_error:
            raise HTTPException(
                status_code=422,
                detail=f"抖音图文解析失败。作品可能不公开。详情：{note_error}",
            ) from note_error
    else:
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                raw_info = downloader.extract_info(resolved_url, download=False)
        except Exception as video_error:
            message = str(video_error)
            if len(message) > 180:
                message = message[:180] + "..."
            raise HTTPException(
                status_code=422,
                detail=f"抖音视频解析失败。作品可能不公开。详情：{message}",
            ) from video_error

    if not raw_info:
        raise HTTPException(status_code=422, detail="没有获取到作品信息")

    info = normalize_entry(raw_info)
    video_remote = choose_video_url(info)
    image_remotes = extract_images(info)
    thumbnail_remote = info.get("thumbnail") or (
        image_remotes[0] if image_remotes else None
    )

    video_url = create_media_url(request, video_remote) if video_remote else None
    image_urls = [create_media_url(request, item) for item in image_remotes[:20]]
    cover_url = (
        create_media_url(request, thumbnail_remote) if thumbnail_remote else None
    )
    if not video_url and not image_urls:
        raise HTTPException(status_code=422, detail="作品中没有找到可保存的媒体")

    return {
        "type": "video" if video_url else "images",
        "title": info.get("title") or info.get("description") or "抖音作品",
        "author": info.get("uploader") or info.get("creator") or "",
        "cover": cover_url,
        "video_url": video_url,
        "images": image_urls,
        "notice": "请只保存本人作品或已获授权的内容",
    }


@app.get("/api/media/{token}/{filename}", name="media_proxy")
async def media_proxy(token: str, filename: str):
    record = MEDIA_TOKENS.get(token)
    if not record:
        raise HTTPException(status_code=404, detail="资源链接不存在或已失效")

    expires_at, remote_url = record
    if expires_at < time.time():
        MEDIA_TOKENS.pop(token, None)
        raise HTTPException(status_code=410, detail="资源链接已过期，请重新解析")

    async def stream():
        headers = {"User-Agent": DESKTOP_UA, "Referer": "https://www.douyin.com/"}
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(60.0)
        ) as client:
            async with client.stream("GET", remote_url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(64 * 1024):
                    yield chunk

    media_type = "video/mp4" if filename.endswith(".mp4") else "image/jpeg"
    return StreamingResponse(stream(), media_type=media_type)
