#!/usr/bin/env python3
"""
法學研討會資訊爬蟲
從各司法機關及大學法律系所網站爬取研討會資訊，產出 seminars.json
"""

import json
import os
import re
import hashlib
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# === 設定 ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(PROJECT_DIR, "data")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "seminars.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, ".last_run")

# 爬取頻率：每 3 天一次
SCRAPE_INTERVAL_DAYS = 3

# 只查詢此日期之後的活動
MIN_EVENT_DATE = "2026-04-01"

# 確保目錄存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(POSTERS_DIR, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "scraper.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# HTTP Session
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
})

# 活動形式關鍵字（研討會、公聽會等）
SEMINAR_KEYWORDS = [
    "研討會", "研討", "論壇", "公聽會", "聽證會", "座談會", "座談",
    "研習", "學術", "工作坊", "workshop", "seminar", "conference",
    "forum", "symposium", "講座", "演講", "學術活動", "發表會",
]

# 法律主題關鍵字（政府機關來源需額外比對此清單）
LAW_KEYWORDS = [
    "法律", "法制", "法規", "法案", "法學", "立法", "修法", "釋憲",
    "司法", "訴訟", "裁判", "判決", "審判", "檢察", "偵查",
    "憲法", "民法", "刑法", "行政法", "商法", "公法", "私法",
    "人權", "基本權", "正當程序", "法治",
    "著作權", "專利", "商標", "智慧財產", "個資", "隱私",
    "公平交易", "競爭法", "消費者保護", "消保",
    "勞動法", "勞基法", "環境法", "金融法", "證券", "保險法",
    "國際法", "條約", "公約", "海洋法",
    "刑事", "民事", "行政訴訟", "家事", "少年",
    "調解", "仲裁", "ADR", "法遵", "合規", "compliance",
    "反貪腐", "廉政", "洗錢防制", "資恐防制",
    "數位治理", "AI法制", "科技法", "電商法", "網路法",
    "性別平等", "性騷擾防治", "兒少保護", "長照法制",
    "都市計畫", "土地法", "建築法", "不動產",
    "選舉", "罷免", "公投", "政黨法",
    "稅法", "財稅", "關稅", "遺產稅",
]

# 日期正則
DATE_PATTERNS = [
    # 民國年格式：113年4月10日, 113/04/10
    r"(\d{2,3})\s*[年/]\s*(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?",
    # 西元年格式：2026-04-10, 2026/04/10
    r"(20\d{2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})",
    # 中文格式：2026年4月10日
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
]

# 時間正則
TIME_PATTERN = r"(\d{1,2}:\d{2})\s*[-~至]\s*(\d{1,2}:\d{2})"


def parse_date(text):
    """從文字中解析日期，回傳 YYYY-MM-DD 格式"""
    if not text:
        return None

    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            year = int(groups[0])
            month = int(groups[1])
            day = int(groups[2])

            # 民國年轉西元年
            if year < 200:
                year += 1911

            try:
                d = datetime(year, month, day)
                return d.strftime("%Y-%m-%d")
            except ValueError:
                continue

    return None


def parse_time(text):
    """從文字中解析時間範圍"""
    if not text:
        return None
    match = re.search(TIME_PATTERN, text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def is_seminar_related(text, category="university"):
    """檢查文字是否為法律相關研討會/公聽會。
    大學來源：只需符合活動形式關鍵字。
    政府來源：需同時符合活動形式 + 法律主題關鍵字。
    """
    if not text:
        return False
    text_lower = text.lower()
    has_seminar = any(kw in text_lower for kw in SEMINAR_KEYWORDS)
    if not has_seminar:
        return False
    # 大學法律系所本身就是法律領域，不需額外比對
    if category == "university":
        return True
    # 政府機關需額外確認與法律相關
    return any(kw in text_lower for kw in LAW_KEYWORDS)


def should_run():
    """檢查是否已達爬取間隔（每 3 天一次），回傳 True 表示應執行"""
    if not os.path.exists(LAST_RUN_FILE):
        return True
    try:
        with open(LAST_RUN_FILE, "r") as f:
            last_run_str = f.read().strip()
        last_run = datetime.fromisoformat(last_run_str)
        elapsed = datetime.now() - last_run
        return elapsed.days >= SCRAPE_INTERVAL_DAYS
    except (ValueError, OSError):
        return True


def record_run():
    """記錄本次執行時間"""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())


def is_after_min_date(date_str):
    """檢查日期是否在 MIN_EVENT_DATE 之後"""
    if not date_str:
        return False
    return date_str >= MIN_EVENT_DATE


def generate_id(source_id, title, date):
    """產生唯一 ID"""
    raw = f"{source_id}-{title}-{date}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:6]
    date_part = (date or "nodate").replace("-", "")
    return f"{source_id}-{date_part}-{short_hash}"


def fetch_page(url, timeout=15):
    """下載網頁內容"""
    try:
        resp = session.get(url, timeout=timeout, verify=True)
        resp.raise_for_status()

        # 嘗試偵測編碼
        if resp.encoding and resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp.text
    except requests.RequestException as e:
        logger.error(f"無法載入 {url}: {e}")
        return None


def find_poster_image(soup, base_url):
    """嘗試從頁面找到海報圖片"""
    # 尋找可能的海報圖片
    poster_keywords = ["poster", "海報", "banner", "活動圖", "event"]

    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "")
        title_attr = img.get("title", "")

        # 檢查圖片是否像海報（較大的圖片或含關鍵字）
        combined = (src + alt + title_attr).lower()
        if any(kw in combined for kw in poster_keywords):
            return urljoin(base_url, src)

        # 檢查圖片尺寸屬性
        width = img.get("width", "")
        height = img.get("height", "")
        try:
            if int(width) > 400 or int(height) > 400:
                return urljoin(base_url, src)
        except (ValueError, TypeError):
            pass

    return None


def download_poster(poster_url, seminar_id):
    """下載海報圖片到本地"""
    if not poster_url:
        return None

    try:
        ext = os.path.splitext(urlparse(poster_url).path)[1] or ".jpg"
        filename = f"{seminar_id}{ext}"
        filepath = os.path.join(POSTERS_DIR, filename)

        if os.path.exists(filepath):
            return f"data/posters/{filename}"

        resp = session.get(poster_url, timeout=15)
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            f.write(resp.content)

        logger.info(f"  下載海報: {filename}")
        return f"data/posters/{filename}"
    except Exception as e:
        logger.warning(f"  海報下載失敗: {e}")
        return None


def scrape_source(source):
    """爬取單一來源的研討會資訊"""
    source_id = source["id"]
    source_name = source["name"]
    category = source["category"]
    url = source["url"]
    selectors = source.get("selectors", {})

    logger.info(f"開始爬取: {source_name} ({url})")

    html = fetch_page(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []

    # 使用設定的選擇器或通用邏輯
    list_selector = selectors.get("list", "")
    items = []

    # 嘗試多種選擇器
    for sel in list_selector.split(","):
        sel = sel.strip()
        if sel:
            found = soup.select(sel)
            if found:
                items = found
                break

    # 如果選擇器找不到，用通用方式：找所有連結
    if not items:
        items = soup.find_all("a", href=True)

    for item in items:
        try:
            # 取得標題和連結
            title = ""
            link = ""

            if item.name == "a":
                title = item.get_text(strip=True)
                link = item.get("href", "")
            else:
                # 從項目中找連結
                title_selectors = selectors.get("title", "a").split(",")
                for ts in title_selectors:
                    ts = ts.strip()
                    el = item.select_one(ts)
                    if el:
                        title = el.get_text(strip=True)
                        if el.name == "a":
                            link = el.get("href", "")
                        elif el.find("a"):
                            link = el.find("a").get("href", "")
                        break

                if not title:
                    title = item.get_text(strip=True)

            # 清理標題
            title = re.sub(r"\s+", " ", title).strip()

            # 篩選研討會相關內容（政府機關需同時符合法律主題）
            if not title or len(title) < 5 or not is_seminar_related(title, category):
                continue

            # 完整連結（確保是個別文章頁，而非列表首頁）
            if link:
                link = urljoin(url, link)
                # 排除指向首頁或列表頁本身的連結
                if link.rstrip("/") == url.rstrip("/"):
                    link = ""
                # 排除 javascript:, mailto:, # 開頭的無效連結
                if link and (link.startswith("javascript:") or link.startswith("mailto:") or link == "#"):
                    link = ""

            # 解析日期
            text_content = item.get_text(" ", strip=True)
            date = parse_date(text_content)

            # 嘗試從日期選擇器取得日期
            if not date:
                date_selectors = selectors.get("date", "").split(",")
                for ds in date_selectors:
                    ds = ds.strip()
                    if ds:
                        date_el = item.select_one(ds)
                        if date_el:
                            date = parse_date(date_el.get_text(strip=True))
                            if date:
                                break

            # 如果還是沒有日期，用今天
            if not date:
                date = datetime.now().strftime("%Y-%m-%d")

            # 只保留 MIN_EVENT_DATE 之後的活動
            if not is_after_min_date(date):
                continue

            # 解析時間
            time_str = parse_time(text_content) or ""

            # 產生 ID
            seminar_id = generate_id(source_id, title, date)

            # 嘗試找海報（如果有詳細頁面）
            poster_url = None
            poster_local = None
            if link:
                try:
                    detail_html = fetch_page(link)
                    if detail_html:
                        detail_soup = BeautifulSoup(detail_html, "lxml")
                        poster_url = find_poster_image(detail_soup, link)
                        if poster_url:
                            poster_local = download_poster(poster_url, seminar_id)
                except Exception:
                    pass

            # 驗證連結可達性：確保 link 是完整且有效的 URL
            article_url = ""
            if link and link.startswith("http"):
                article_url = link
            elif link:
                # 相對路徑已在前面用 urljoin 處理過
                article_url = link if link.startswith("http") else ""

            seminar = {
                "id": seminar_id,
                "title": title,
                "source": source_name,
                "category": category,
                "date": date,
                "time": time_str,
                "location": "",
                "description": "",
                "url": article_url or url,
                "sourceUrl": url,
                "posterUrl": poster_local,
                "logoUrl": f"assets/logos/{source_id}.svg",
                "tags": [],
            }

            # 嘗試從詳細頁面取得更多資訊
            if link and detail_html:
                try:
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    # 找描述文字
                    content_el = detail_soup.select_one(
                        ".field-body, .content, article, .post-content, .entry-content, "
                        ".main-content, #content"
                    )
                    if content_el:
                        desc = content_el.get_text(" ", strip=True)
                        seminar["description"] = desc[:300] + ("..." if len(desc) > 300 else "")

                        # 從描述中解析地點
                        location_match = re.search(
                            r"(?:地[點址點]|場地|地點)\s*[:：]\s*(.+?)(?:[。\n]|$)",
                            desc
                        )
                        if location_match:
                            seminar["location"] = location_match.group(1).strip()[:100]

                        # 從描述中解析時間
                        if not time_str:
                            time_str = parse_time(desc)
                            if time_str:
                                seminar["time"] = time_str
                except Exception:
                    pass

            results.append(seminar)
            logger.info(f"  找到: {title[:40]}...")

        except Exception as e:
            logger.debug(f"  處理項目失敗: {e}")
            continue

    logger.info(f"  {source_name} 共找到 {len(results)} 筆研討會資訊")
    return results


def merge_with_existing(new_seminars):
    """與既有資料合併，避免重複"""
    existing = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing = data.get("seminars", [])
        except (json.JSONDecodeError, KeyError):
            pass

    existing_ids = {s["id"] for s in existing}
    merged = list(existing)

    for s in new_seminars:
        if s["id"] not in existing_ids:
            merged.append(s)
            existing_ids.add(s["id"])

    # 依日期排序
    merged.sort(key=lambda x: x.get("date", ""), reverse=False)

    # 只保留 MIN_EVENT_DATE 之後的活動，並移除超過 180 天前的資料
    cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    effective_cutoff = max(cutoff, MIN_EVENT_DATE)
    merged = [s for s in merged if s.get("date", "") >= effective_cutoff]

    return merged


def main():
    # 檢查爬取頻率（每 3 天一次）
    force = "--force" in os.sys.argv
    if not force and not should_run():
        logger.info(f"距離上次爬取未滿 {SCRAPE_INTERVAL_DAYS} 天，跳過本次執行。")
        logger.info("如需強制執行，請加上 --force 參數。")
        return

    logger.info("=" * 50)
    logger.info(f"開始爬取法學研討會資訊（只查詢 {MIN_EVENT_DATE} 之後的活動）")
    logger.info(f"爬取頻率：每 {SCRAPE_INTERVAL_DAYS} 天一次")
    logger.info("=" * 50)

    # 載入來源設定
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    all_seminars = []

    for source in config["sources"]:
        try:
            results = scrape_source(source)
            all_seminars.extend(results)
        except Exception as e:
            logger.error(f"爬取 {source['name']} 時發生錯誤: {e}")
            continue

    logger.info(f"\n本次共爬取到 {len(all_seminars)} 筆研討會資訊")

    # 合併既有資料
    merged = merge_with_existing(all_seminars)

    # 輸出 JSON
    output = {
        "lastUpdated": datetime.now().isoformat(),
        "seminars": merged,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"資料已寫入 {OUTPUT_FILE}（共 {len(merged)} 筆）")

    # 記錄執行時間
    record_run()
    logger.info("爬取完成！下次執行時間：3 天後。")


if __name__ == "__main__":
    main()
