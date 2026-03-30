#!/usr/bin/env python3
"""
Facebook 粉專搜尋模組
透過 Brave Search API 搜尋指定粉專的研討會資訊，再用 Gemini AI 結構化判讀
不直接爬取 Facebook，改用搜尋引擎間接取得公開資訊
"""

import json
import os
import re
import hashlib
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# === API 設定 ===
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# 搜尋關鍵字組合
SEARCH_QUERIES = [
    'site:facebook.com "研討會" (法律 OR 法學 OR 司法) 2026',
    'site:facebook.com "講座" (法律 OR 法學) 2026',
    'site:facebook.com "座談會" (法律 OR 法制) 2026',
    'site:facebook.com "論壇" (法律 OR 司法改革) 2026',
    '元照出版 研討會 2026',
    '月旦法學 講座 2026',
    '台灣法學會 研討會 2026',
    '司法改革基金會 座談會 2026',
]


def brave_search(query, count=10):
    """使用 Brave Search API 搜尋

    Args:
        query: 搜尋關鍵字
        count: 結果數量

    Returns:
        list of search result dicts
    """
    if not BRAVE_API_KEY:
        return []

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": count,
        "search_lang": "zh-hant",
        "country": "tw",
        "freshness": "pm",  # 過去一個月
    }

    try:
        resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        logger.info(f"    Brave 搜尋 '{query[:30]}...' 找到 {len(results)} 筆結果")
        return results
    except requests.RequestException as e:
        logger.warning(f"    Brave Search API 失敗: {e}")
        return []
    except (json.JSONDecodeError, KeyError):
        return []


def collect_search_results():
    """執行所有搜尋查詢，收集原始結果"""
    all_results = []
    seen_urls = set()

    for query in SEARCH_QUERIES:
        results = brave_search(query, count=5)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "description": r.get("description", ""),
                    "age": r.get("age", ""),
                })

    logger.info(f"  Brave 搜尋共收集 {len(all_results)} 筆不重複結果")
    return all_results


def gemini_analyze_results(search_results, min_event_date):
    """用 Gemini AI 分析搜尋結果，萃取研討會結構化資訊"""
    if not GEMINI_API_KEY or not search_results:
        return []

    items_text = ""
    for i, r in enumerate(search_results[:20]):  # 最多 20 筆
        items_text += f"\n--- 結果 {i+1} ---\n"
        items_text += f"標題: {r['title']}\n"
        items_text += f"網址: {r['url']}\n"
        items_text += f"摘要: {r['description'][:200]}\n"

    prompt = f"""你是法律學術活動辨識專家。以下是從搜尋引擎找到的結果，請判斷哪些是台灣的法律相關研討會、座談會、論壇、演講、講座等學術活動。

判斷標準：
1. 必須是對外公開的法律相關學術活動
2. 活動日期在 {min_event_date} 之後
3. 排除一般新聞報導、商品廣告、招生資訊
4. 如果無法確認是研討會活動，則排除

請以 JSON 陣列格式回覆，只保留確認為研討會活動的項目，每筆包含：
- "title": 活動完整名稱
- "source": 主辦機構
- "date": 活動日期（YYYY-MM-DD，無法確定則為 null）
- "time": 活動時間（HH:MM-HH:MM，無法確定則為 null）
- "location": 活動地點（無法確定則為 null）
- "description": 活動簡介（100字以內）
- "url": 原始連結
- "tags": 標籤陣列（最多3個）

只回傳 JSON 陣列，不要其他文字。搜尋不到活動則回傳 []。

{items_text}"""

    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        results = json.loads(text)
        logger.info(f"  Gemini 判讀出 {len(results)} 筆有效研討會")
        return results
    except Exception as e:
        logger.warning(f"  Gemini 分析失敗: {e}")
        return []


def gemini_direct_search(min_event_date):
    """直接用 Gemini + Google Search grounding 搜尋（Brave 不可用時的備案）"""
    if not GEMINI_API_KEY:
        return []

    logger.info("  使用 Gemini + Google Search grounding 搜尋...")

    prompt = f"""請搜尋台灣近期的法律相關研討會、座談會、論壇、講座等學術活動。

搜尋重點：
- Facebook 粉專：元照出版、月旦法學、台灣法學會、司法改革基金會、法律白話文運動
- 大學法學院的活動公告
- 活動日期在 {min_event_date} 之後

請以 JSON 陣列格式回傳，每筆包含：
- "title": 活動名稱
- "source": 主辦機構
- "date": 活動日期（YYYY-MM-DD，無法確定則為 null）
- "time": 活動時間（HH:MM-HH:MM，無法確定則為 null）
- "location": 活動地點（無法確定則為 null）
- "description": 活動簡介（100字以內）
- "url": 原始連結
- "tags": 標籤陣列（最多3個）

只回傳 JSON 陣列。搜尋不到則回傳 []。"""

    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
        "tools": [{"google_search": {}}],
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        results = json.loads(text)
        logger.info(f"  Gemini 搜尋到 {len(results)} 筆研討會")
        return results
    except Exception as e:
        logger.warning(f"  Gemini 搜尋失敗: {e}")
        return []


def build_seminars(raw_results, min_event_date):
    """將分析結果轉換為標準研討會格式"""
    seminars = []

    for item in raw_results:
        title = item.get("title", "").strip()
        if not title:
            continue

        date = item.get("date") or datetime.now().strftime("%Y-%m-%d")
        if date < min_event_date:
            continue

        source = item.get("source", "Facebook搜尋")
        raw_id = f"fb-{source}-{title}-{date}"
        short_hash = hashlib.md5(raw_id.encode()).hexdigest()[:6]
        date_part = date.replace("-", "")
        seminar_id = f"fb-{date_part}-{short_hash}"

        item_url = item.get("url", "")
        seminars.append({
            "id": seminar_id,
            "title": title,
            "source": source,
            "category": "facebook",
            "date": date,
            "time": item.get("time") or "",
            "location": item.get("location") or "",
            "description": item.get("description") or "",
            "url": item_url,
            "sourceUrl": item_url,
            "posterUrl": None,
            "logoUrl": "assets/logos/facebook.svg",
            "tags": item.get("tags", [])[:3],
            "status": "pending",
            "verifiedFields": {
                "title": True,
                "date": bool(item.get("date")),
                "url": bool(item_url),
                "time": bool(item.get("time")),
                "location": bool(item.get("location")),
                "description": bool(item.get("description")),
            },
            "confidence": "medium",
            "aiVerified": True,
        })

    return seminars


def scrape_facebook(min_event_date="2026-04-01"):
    """搜尋 Facebook 粉專的法學研討會資訊

    策略：
    1. 優先使用 Brave Search API 搜尋 → Gemini AI 分析判讀
    2. 備案：直接用 Gemini + Google Search grounding

    Returns:
        list of seminar dicts
    """
    if not GEMINI_API_KEY and not BRAVE_API_KEY:
        logger.info("未設定 BRAVE_API_KEY 或 GEMINI_API_KEY，跳過 Facebook 搜尋")
        return []

    logger.info("=" * 30)
    logger.info("開始搜尋 Facebook 粉專研討會資訊")

    raw_results = []

    # 策略 1：Brave Search + Gemini 分析
    if BRAVE_API_KEY:
        logger.info("使用 Brave Search API 搜尋...")
        search_results = collect_search_results()
        if search_results and GEMINI_API_KEY:
            raw_results = gemini_analyze_results(search_results, min_event_date)
        elif search_results:
            # 沒有 Gemini 時，直接用搜尋結果（精準度較低）
            for r in search_results:
                raw_results.append({
                    "title": r["title"],
                    "source": "Facebook搜尋",
                    "date": None,
                    "url": r["url"],
                    "description": r["description"],
                    "tags": [],
                })

    # 策略 2：Gemini 直接搜尋（備案）
    if not raw_results and GEMINI_API_KEY:
        raw_results = gemini_direct_search(min_event_date)

    # 轉換為標準格式
    seminars = build_seminars(raw_results, min_event_date)
    logger.info(f"Facebook 搜尋最終保留 {len(seminars)} 筆")
    logger.info("=" * 30)
    return seminars
