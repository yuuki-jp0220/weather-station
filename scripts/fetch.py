#!/usr/bin/env python3
"""Conservative outage summary fetcher for the WEATHER STATION project.

A count is published only when the official page contains an explicit,
unambiguous service-area total or an explicit no-outage statement.
Unknown/ambiguous values are never converted to zero.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9), "JST")
OUTPUT_PATH = Path("docs/data.json")
TIMEOUT = 8

def create_session():
    session = requests.Session()
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        status=3,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; WeatherStationOutageMonitor/1.0; +https://yuuki-jp0220.github.io/weather-station/)",
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    return session

SESSION = create_session()

COMPANIES = [
    {"code": "hepco", "name": "北海道電力ネットワーク", "url": "https://teiden-info.hepco.co.jp/"},
    {"code": "tohoku", "name": "東北電力ネットワーク", "url": "https://nw.tohoku-epco.co.jp/teideninfo/evhindex.html"},
    {"code": "tepco", "name": "東京電力パワーグリッド", "url": "https://teideninfo.tepco.co.jp/"},
    {"code": "chuden", "name": "中部電力パワーグリッド", "url": "https://teiden.powergrid.chuden.co.jp/p/"},
    {"code": "rikuden", "name": "北陸電力送配電", "url": "https://www.rikuden.co.jp/nw/teiden/otj010.html"},
    {"code": "kepco", "name": "関西電力送配電", "url": "https://www.kansai-td.co.jp/teiden-info/"},
    {"code": "energia", "name": "中国電力ネットワーク", "url": "https://www.teideninfo.energia.co.jp/"},
    {"code": "yonden", "name": "四国電力送配電", "url": "https://www.yonden.co.jp/nw/teiden-info/"},
    {"code": "kyuden", "name": "九州電力送配電", "url": "https://www.kyuden.co.jp/td_teiden/kyushu.html"},
    {"code": "okiden", "name": "沖縄電力", "url": "https://www.okidenmail.jp/bosai/info/"},
]

def now_jst():
    return datetime.now(JST).isoformat(timespec="seconds")

def load_previous_data():
    try:
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return {item["code"]: item for item in data.get("companies", [])}
    except (OSError, ValueError, TypeError, KeyError):
        return {}

def extract_count(text):
    """Return (exact_count, approximate_label, evidence_kind)."""
    text = re.sub(r"\s+", " ", text).replace("，", ",")
    zero_phrases = (
        "現在、停電情報はございません",
        "現在停電情報はございません",
        "広域的な停電情報はございません",
        "現在、停電は発生しておりません",
        "現在停電は発生しておりません",
        "現在、停電は発生していません",
        "現在停電は発生していません",
        "現在、停電はありません",
        "現在停電はありません",
    )
    if any(phrase in text for phrase in zero_phrases):
        return 0, None, "explicit_no_outage"

    approx = re.search(r"(約\s*[0-9][0-9,]*\s*(?:軒|戸|件)|[0-9][0-9,]*\s*(?:軒|戸|件)\s*未満)", text)
    if approx:
        return None, approx.group(1), "approximate"

    total_labels = (
        "全道計", "全道合計", "全域合計", "全エリア合計",
        "サービスエリア全域", "管内合計", "全県合計",
        "関西エリア全域", "合計", "総計",
    )
    for label in total_labels:
        pattern = re.escape(label) + r"\s*[:：]?\s*([0-9][0-9,]*)\s*(?:軒|戸|件)"
        match = re.search(pattern, text)
        if match:
            return int(match.group(1).replace(",", "")), None, "labelled_total"
        pattern = re.escape(label) + r"[^。]{0,30}?([0-9][0-9,]*)\s*(?:軒|戸|件)"
        match = re.search(pattern, text)
        if match:
            return int(match.group(1).replace(",", "")), None, "labelled_total"

    return None, None, "no_unambiguous_total"

def fetch_company(company, previous):
    previous_item = previous.get(company["code"], {})
    result = {
        **company,
        "count": None,
        "count_label": None,
        "status": "error",
        "fetched_at": now_jst(),
        "error": None,
        "evidence": None,
        "last_success_count": previous_item.get("count") if previous_item.get("status") == "ok" else previous_item.get("last_success_count"),
    }
    try:
        response = SESSION.get(company["url"], timeout=TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        content_type = response.headers.get("Content-Type", "").lower()
        body = response.text.lstrip()

        if "json" in content_type or body.startswith(("{", "[")):
            payload = response.json()
            candidates = []
            if isinstance(payload, dict):
                for key in ("total_count", "totalCount", "outage_count", "outageCount", "teidenKosu"):
                    if key in payload:
                        candidates.append(payload[key])
            if len(candidates) != 1:
                raise ValueError("JSON内に一意の全エリア合計項目がありません")
            raw = str(candidates[0]).strip().replace(",", "")
            if not re.fullmatch(r"[0-9]+", raw):
                raise ValueError("JSONの合計値が整数ではありません")
            count = int(raw)
            evidence = "explicit_json_total"
            approximate = None
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            visible_text = soup.get_text(" ", strip=True)
            count, approximate, evidence = extract_count(visible_text)

        if count is None:
            if approximate:
                result["status"] = "approximate"
                result["count_label"] = approximate
                result["error"] = "公式表示が概数のため、正確な件数として扱っていません"
                result["evidence"] = evidence
                return result
            raise ValueError("明確な全エリア合計を確認できません（動的表示・ページ仕様変更の可能性）")

        result["count"] = count
        result["count_label"] = str(count)
        result["last_success_count"] = count
        result["status"] = "ok"
        result["error"] = None
        result["evidence"] = evidence
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[WARN] {company['name']}: {result['error']}", file=sys.stderr)
        return result

def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = load_previous_data()
    results = [None] * len(COMPANIES)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_company, company, previous): index for index, company in enumerate(COMPANIES)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    successful = [item for item in results if item["status"] == "ok"]
    complete = len(successful) == len(COMPANIES)
    output = {
        "updated_at": now_jst(),
        "total_count": sum(item["count"] for item in successful) if complete else None,
        "total_status": "ok" if complete else "incomplete",
        "successful_companies": len(successful),
        "company_count": len(COMPANIES),
        "companies": results,
        "note": "全国合計は10社すべての正確な件数を取得できた場合のみ表示。取得失敗・概数は0として扱いません。",
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完了: 成功 {len(successful)}/{len(COMPANIES)} 社 / 全国合計: {output['total_count'] if complete else '未確定'} / {output['updated_at']}")

if __name__ == "__main__":
    main()
