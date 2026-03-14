import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional

import feedparser
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover - optional dependency during local dev
    build = None  # type: ignore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nexus")

app = FastAPI(title="News Aggregator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================
# CONFIG
# =============================================
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "YOUR_YOUTUBE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "YOUR_APIFY_TOKEN")

# In-memory store (replace with PostgreSQL in production)
news_store: List[dict] = []
keywords_store: List[str] = []
_store_lock = asyncio.Lock()


def normalize_keyword(raw: Optional[str]) -> str:
    if not raw:
        return ""
    cleaned = raw.strip()
    while cleaned.startswith("#"):
        cleaned = cleaned[1:].strip()
    cleaned = " ".join(cleaned.split())
    return cleaned


def find_keyword_match(keyword: Optional[str]) -> Optional[str]:
    normalized = normalize_keyword(keyword)
    if not normalized:
        return None
    needle = normalized.casefold()
    for existing in keywords_store:
        if existing.casefold() == needle:
            return existing
    return None


def normalize_sentiment(value: Optional[str]) -> str:
    if not value:
        return "neutral"
    mapped = value.strip().lower()
    if mapped in {"positive", "pos", "+", "good"}:
        return "positive"
    if mapped in {"negative", "neg", "-", "bad"}:
        return "negative"
    return "neutral"


def parse_ai_json(text: str) -> dict:
    if not text:
        return {}
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if "\n" in cleaned:
            cleaned = cleaned.split("\n", 1)[1]
        if "```" in cleaned:
            cleaned = cleaned.rsplit("```", 1)[0]

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse AI JSON: %s", cleaned)
        return {}

# =============================================
# MODELS
# =============================================


class NewsItem(BaseModel):
    id: str
    title: str
    url: str
    thumbnail: Optional[str]
    platform: str  # youtube | google | tiktok
    published_at: str
    summary: Optional[str]
    sentiment: Optional[str]  # positive | negative | neutral
    tags: List[str] = []
    view_count: Optional[int]
    keyword: str


class KeywordRequest(BaseModel):
    keyword: str


# =============================================
# GOOGLE NEWS RSS COLLECTOR
# =============================================


async def collect_google_news(keyword: str) -> List[dict]:
    """Collect from Google News RSS - free & no auth needed"""
    url = f"https://news.google.com/rss/search?q={keyword}&hl=vi&gl=VN&ceid=VN:vi"
    feed = feedparser.parse(url)
    items: List[dict] = []
    for entry in feed.entries[:20]:
        items.append(
            {
                "id": f"google_{hash(entry.link)}",
                "title": entry.title,
                "url": entry.link,
                "thumbnail": None,
                "platform": "google",
                "published_at": entry.get("published", datetime.now().isoformat()),
                "summary": None,
                "sentiment": None,
                "tags": [],
                "view_count": None,
                "keyword": keyword,
            }
        )
    return items


# =============================================
# YOUTUBE COLLECTOR
# =============================================


async def collect_youtube(keyword: str) -> List[dict]:
    """Collect from YouTube Data API v3"""
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY" or build is None:
        # Return mock data if no API key
        return [
            {
                "id": f"yt_mock_{i}",
                "title": f"[YouTube] Video về {keyword} #{i}",
                "url": f"https://youtube.com/watch?v=mock{i}",
                "thumbnail": f"https://picsum.photos/seed/yt{i}/320/180",
                "platform": "youtube",
                "published_at": (datetime.now() - timedelta(hours=i * 3)).isoformat(),
                "summary": None,
                "sentiment": None,
                "tags": [],
                "view_count": 1000 * i,
                "keyword": keyword,
            }
            for i in range(1, 6)
        ]

    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        request = youtube.search().list(
            part="snippet",
            q=keyword,
            type="video",
            order="date",
            maxResults=20,
            regionCode="VN",
        )
        response = request.execute()
        items = []
        for item in response.get("items", []):
            vid_id = item["id"]["videoId"]
            snippet = item["snippet"]
            items.append(
                {
                    "id": f"yt_{vid_id}",
                    "title": snippet["title"],
                    "url": f"https://youtube.com/watch?v={vid_id}",
                    "thumbnail": snippet["thumbnails"]["medium"]["url"],
                    "platform": "youtube",
                    "published_at": snippet["publishedAt"],
                    "summary": snippet.get("description", "")[:200],
                    "sentiment": None,
                    "tags": [],
                    "view_count": None,
                    "keyword": keyword,
                }
            )
        return items
    except Exception as exc:  # pragma: no cover - depends on external API
        logger.error("YouTube API error: %s", exc)
        return []


# =============================================
# TIKTOK COLLECTOR (TikWM + Apify fallback)
# =============================================


async def _collect_tiktok_via_tikwm(keyword: str) -> List[dict]:
    """Use public TikWM API to search TikTok videos by keyword."""
    payload = {
        "keywords": keyword,
        "count": 20,
        "cursor": 0,
        "region": "VN",
        "web": 1,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post("https://www.tikwm.com/api/feed/search", data=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # pragma: no cover - external API instability
        logger.warning("TikWM fallback error for %s: %s", keyword, exc)
        return []

    if data.get("code") != 0:
        logger.warning("TikWM returned non-success code for %s: %s", keyword, data.get("msg"))
        return []

    videos = (data.get("data") or {}).get("videos") or []
    items: List[dict] = []
    for video in videos:
        video_id = video.get("id") or video.get("video_id")
        if not video_id:
            continue
        author = (video.get("author") or {}).get("unique_id") or (video.get("author") or {}).get("sec_uid")
        url = video.get("share_url")
        if not url and author:
            url = f"https://www.tiktok.com/@{author}/video/{video_id}"
        published_raw = video.get("create_time") or video.get("createTime")
        try:
            published_at = datetime.utcfromtimestamp(int(published_raw)).isoformat() if published_raw else datetime.utcnow().isoformat()
        except (ValueError, OSError):
            published_at = datetime.utcnow().isoformat()
        items.append(
            {
                "id": f"tt_tikwm_{video_id}",
                "title": (video.get("title") or video.get("desc") or video.get("text") or "")[:200],
                "url": url or "",
                "thumbnail": video.get("cover") or video.get("origin_cover"),
                "platform": "tiktok",
                "published_at": published_at,
                "summary": None,
                "sentiment": None,
                "tags": [],
                "view_count": video.get("play_count") or video.get("playCount"),
                "keyword": keyword,
            }
        )

    if items:
        logger.info("TikWM returned %s TikTok items for keyword=%s", len(items), keyword)
    return items


async def collect_tiktok(keyword: str) -> List[dict]:
    """Collect TikTok posts via TikWM, fallback to Apify when needed."""

    tikwm_items = await _collect_tiktok_via_tikwm(keyword)
    if tikwm_items:
        return tikwm_items

    if not APIFY_TOKEN or APIFY_TOKEN == "YOUR_APIFY_TOKEN":
        logger.warning("TikWM returned no data and APIFY_TOKEN missing – skip TikTok keyword=%s", keyword)
        return []

    logger.info("TikWM empty for keyword=%s, falling back to Apify", keyword)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            payload = {
                "searchQueries": [keyword],
                "searches": [
                    {
                        "type": "search",
                        "value": keyword,
                    }
                ],
                "resultsPerPage": 20,
                "maxResults": 40,
                "shouldDownloadVideos": False,
            }
            run_resp = await client.post(
                "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs",
                params={"token": APIFY_TOKEN},
                json=payload,
            )
            run_resp.raise_for_status()
            run_data = run_resp.json().get("data", {})
            run_id = run_data.get("id")
            if not run_id:
                raise RuntimeError("Apify run ID missing")

            wait_resp = await client.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}/waitForFinish",
                params={"token": APIFY_TOKEN, "timeout": 120000},
            )
            wait_resp.raise_for_status()
            wait_data = wait_resp.json().get("data", {})
            status = wait_data.get("status")
            if status != "SUCCEEDED":
                logger.error("TikTok Apify run %s finished with status %s", run_id, status)
                return []

            dataset_id = wait_data.get("defaultDatasetId") or run_data.get("defaultDatasetId")
            dataset_url = (
                f"https://api.apify.com/v2/datasets/{dataset_id}/items"
                if dataset_id
                else f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items"
            )

            dataset_resp = await client.get(dataset_url, params={"token": APIFY_TOKEN})
            dataset_resp.raise_for_status()

            dataset_items = dataset_resp.json()
            logger.info(
                "Apify TikTok dataset for '%s': %s items (run_id=%s)",
                keyword,
                len(dataset_items) if isinstance(dataset_items, list) else "?",
                run_id,
            )

        items: List[dict] = []
        for video in dataset_items:
            video_id = video.get("id") or video.get("videoId") or video.get("itemId")
            if not video_id:
                continue
            published = video.get("createTime") or video.get("create_time")
            try:
                published_at = datetime.utcfromtimestamp(int(published)).isoformat() if published else datetime.utcnow().isoformat()
            except (ValueError, OSError):
                published_at = datetime.utcnow().isoformat()
            url = video.get("webVideoUrl") or video.get("shareUrl")
            if not url and video.get("authorUniqueId"):
                url = f"https://www.tiktok.com/@{video['authorUniqueId']}/video/{video_id}"
            items.append(
                {
                    "id": f"tt_{video_id}",
                    "title": (video.get("text") or "")[:200],
                    "url": url or "",
                    "thumbnail": (video.get("covers") or [None])[0],
                    "platform": "tiktok",
                    "published_at": published_at,
                    "summary": None,
                    "sentiment": None,
                    "tags": [],
                    "view_count": video.get("playCount"),
                    "keyword": keyword,
                }
            )

        if not items:
            logger.warning("Apify TikTok collector returned no items for keyword=%s", keyword)
        return items
    except Exception as exc:  # pragma: no cover - depends on external API
        logger.error("TikTok collection error for %s: %s", keyword, exc)
        return []


# =============================================
# AI PROCESSOR (Claude API)
# =============================================


async def ai_process_items(items: List[dict]) -> List[dict]:
    """Use OpenAI to summarize and classify news items"""
    if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY":
        sentiments = ["positive", "neutral", "negative"]
        tag_sets = [
            ["khuyến mãi", "sale"],
            ["khai trương", "sự kiện"],
            ["tin tức", "cập nhật"],
            ["sản phẩm mới", "review"],
        ]
        import random

        for i, item in enumerate(items):
            item["summary"] = item.get("summary") or (
                f"Bài viết về {item['keyword']} - được tổng hợp tự động từ {item['platform']}."
            )
            item["sentiment"] = normalize_sentiment(item.get("sentiment") or sentiments[i % len(sentiments)])
            item["tags"] = item.get("tags") or tag_sets[i % len(tag_sets)]
            random.shuffle(item["tags"])
        return items

    system_prompt = (
        "Bạn là trợ lý phân tích tin tức tiếng Việt, tóm tắt ngắn gọn và phân loại cảm xúc. "
        "Luôn trả về JSON hợp lệ với các khóa summary, sentiment (positive|negative|neutral), tags."
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45) as client:
        for item in items:
            prompt = (
                "Phân tích nội dung sau và trả về JSON:\n"
                f"Tiêu đề: {item['title']}\n"
                f"Nguồn: {item['platform']}\n"
                f"Mô tả: {item.get('summary') or ''}\n\n"
                "JSON cần có dạng: {\"summary\": \"<tóm tắt 1-2 câu tiếng Việt>\", \"sentiment\": \"positive|negative|neutral\", "
                "\"tags\": [\"tag1\", \"tag2\"]}."
            )

            try:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": OPENAI_MODEL,
                        "temperature": 0.2,
                        "max_tokens": 220,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                message_content = resp.json()["choices"][0]["message"].get("content", "")
                if isinstance(message_content, list):
                    parts = []
                    for block in message_content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts).strip()
                else:
                    content = str(message_content or "").strip()

                parsed = parse_ai_json(content)
                item["summary"] = parsed.get("summary") or item.get("summary") or ""
                item["sentiment"] = normalize_sentiment(parsed.get("sentiment") or item.get("sentiment"))
                raw_tags = parsed.get("tags")
                if isinstance(raw_tags, str):
                    tags = [raw_tags]
                elif isinstance(raw_tags, list):
                    tags = [str(tag) for tag in raw_tags if tag]
                else:
                    tags = []
                item["tags"] = tags or item.get("tags") or []
            except Exception as exc:  # pragma: no cover - external API
                logger.error("OpenAI processing error: %s", exc)
                item["sentiment"] = normalize_sentiment(item.get("sentiment"))

    return items


# =============================================
# API ROUTES
# =============================================


@app.get("/")
async def root():
    return {"status": "running", "version": "1.0.0"}


@app.get("/api/news")
async def get_news(
    keyword: Optional[str] = None,
    platform: Optional[str] = None,
    sentiment: Optional[str] = None,
    limit: int = Query(50, le=200),
):
    """Get aggregated news feed"""
    results = news_store.copy()

    if keyword:
        normalized_kw = normalize_keyword(keyword)
        if normalized_kw:
            target = normalized_kw.casefold()
            results = [
                r
                for r in results
                if normalize_keyword(r.get("keyword")).casefold() == target
            ]
        else:
            results = []
    if platform:
        results = [r for r in results if r["platform"] == platform]
    if sentiment:
        results = [r for r in results if r.get("sentiment") == sentiment]

    results.sort(key=lambda x: x["published_at"], reverse=True)

    return {"total": len(results), "items": results[:limit]}


@app.post("/api/fetch")
async def fetch_news(background_tasks: BackgroundTasks, keyword: Optional[str] = None):
    """Trigger news collection for all keywords or specific one"""
    if keyword:
        normalized = normalize_keyword(keyword)
        if not normalized:
            raise HTTPException(status_code=400, detail="Keyword không hợp lệ")
        match = find_keyword_match(normalized)
        if match:
            keyword = match
        else:
            keywords_store.append(normalized)
            keyword = normalized
    kws = [keyword] if keyword else keywords_store.copy()
    background_tasks.add_task(run_collection, kws)
    return {"message": f"Collecting news for: {kws}", "status": "started"}


@app.get("/api/keywords")
async def get_keywords():
    return {"keywords": keywords_store}


@app.post("/api/keywords")
async def add_keyword(req: KeywordRequest):
    keyword = normalize_keyword(req.keyword)
    if not keyword:
        raise HTTPException(status_code=400, detail="Keyword không hợp lệ")
    if not find_keyword_match(keyword):
        keywords_store.append(keyword)
    return {"keywords": keywords_store}


@app.delete("/api/keywords/{keyword}")
async def remove_keyword(keyword: str):
    target = find_keyword_match(keyword)
    if target:
        keywords_store.remove(target)
    return {"keywords": keywords_store}


@app.get("/api/stats")
async def get_stats():
    """Dashboard statistics"""
    total = len(news_store)
    by_platform: dict[str, int] = {}
    by_sentiment: dict[str, int] = {}
    by_keyword: dict[str, int] = {}

    for item in news_store:
        p = item["platform"]
        s = item.get("sentiment", "neutral")
        k = item["keyword"]
        by_platform[p] = by_platform.get(p, 0) + 1
        by_sentiment[s] = by_sentiment.get(s, 0) + 1
        by_keyword[k] = by_keyword.get(k, 0) + 1

    trend = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        count = sum(1 for item in news_store if item["published_at"].startswith(day))
        trend.append({"date": day, "count": count})

    return {
        "total": total,
        "by_platform": by_platform,
        "by_sentiment": by_sentiment,
        "by_keyword": by_keyword,
        "trend": trend,
    }


# =============================================
# BACKGROUND COLLECTION TASK
# =============================================


async def _process_and_store_items(items: List[dict]) -> int:
    """Process a chunk of items then merge into the shared store."""
    if not items:
        return 0

    processed = await ai_process_items(items)

    for item in processed:
        cleaned_kw = normalize_keyword(item.get("keyword"))
        if cleaned_kw:
            item["keyword"] = cleaned_kw

    global news_store
    async with _store_lock:
        existing_ids = {existing["id"] for existing in news_store}
        new_items = [item for item in processed if item["id"] not in existing_ids]
        if not new_items:
            return 0
        news_store = new_items + news_store
        news_store = news_store[:500]
        return len(new_items)


async def run_collection(keywords: List[str]):
    if not keywords:
        logger.info("run_collection called but no keywords provided")
        return

    total_added = 0

    for kw in keywords:
        collectors = (
            ("google", collect_google_news),
            ("youtube", collect_youtube),
            ("tiktok", collect_tiktok),
        )

        for platform, collector in collectors:
            try:
                chunk = await collector(kw)
            except Exception as exc:  # pragma: no cover - depends on external API
                logger.error("Collector error for %s (%s): %s", kw, platform, exc)
                continue

            added = await _process_and_store_items(chunk)
            if added:
                total_added += added
                logger.info(
                    "Stored %s new %s items for keyword=%s (run total=%s)",
                    added,
                    platform,
                    kw,
                    total_added,
                )

    logger.info("Collection finished. Added %s new items. Store size=%s", total_added, len(news_store))


@app.on_event("startup")
async def startup_event():
    cleaned_keywords: List[str] = []
    seen: set[str] = set()
    for kw in keywords_store:
        normalized = normalize_keyword(kw)
        key = normalized.casefold()
        if normalized and key not in seen:
            cleaned_keywords.append(normalized)
            seen.add(key)
    keywords_store.clear()
    keywords_store.extend(cleaned_keywords)

    if keywords_store:
        asyncio.create_task(run_collection(keywords_store))
