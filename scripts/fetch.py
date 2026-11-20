#!/usr/bin/env python3
"""Fetch public outage summaries from Japanese transmission/distribution operators.

Important:
- Only an explicitly published, unambiguous total is treated as a numeric count.
- A missing/ambiguous value is never converted to zero.
- A failed source remains status="error"; stale values are not counted as current.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re
import sys
from typing import Optional

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9), "JST")
OUTPUT_PATH = Path("docs/data.json")
TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WeatherStationOutageMonitor/1.0; +https://yuuki-jp0220.github.io/weather-station/)",
    "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

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

def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")

def extract_count(text: str) -> tuple[Optional[int], Optional[str], str]:
    """Return (exact_count, display_label, evidence_kind)."""
    normalized = re.sub(r"\s+", " ", text).replace("，", ",")
    no_outage_phrases = (
        "現在、停電は発生しておりません",
        "現在停電は発生しておりません",
        "現在、停電はありません",
        "現在停電はありません",
    )
    if any(phrase in normalized for phrase in no_outage_phrases):
        return 0, "0", "explicit_no_outage"

    # Approximate expressions are deliberately not converted to fabricated integers.
    approx = re.search(r"(10\s*軒未満|10\s*戸未満|10\s*件未満|100\s*軒未満|100\s*戸未満)", normalized)
    if approx:
        return None, approx.group(1), "approximate"

    # Accept only clearly labelled service-area totals, never an arbitrary regional row.
    total_patterns = [
        r"(?:全道計|全道合計|全域合計|全エリア合計|サービスエリア全域|管内合計|全県合計|合計|総計)\s*[:：]?\s*([0-9][0-9,]*)\s*(?:軒|戸|件)",
        r"(?:全道計|全道合計|全域合計|全エリア合計|サービスエリア全域|管内合計|全県合計|合計|総計)[^。]{0,35}?([0-9][0-9,]*)\s*(?:軒|戸|件)",
    ]
    for pattern in total_patterns:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1).replace(",", "")), match.group(1) + "（公式表示）", "labelled_total"

    return None, None, "no_unambiguous_total"

def fetch_company(company: dict) -> dict:
    result = {
        **company,
        "count": None,
        "count_label": None,
        "status": "error",
        "fetched_at": now_jst(),
        "error": None,
        "evidence": None,
    }
    try:
        response = requests.get(company["url"], headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        content_type = response.headers.get("Content-Type", "").lower()

        if "json" in content_type or response.text.lstrip().startswith(("{", "[")):
            payload = response.json()
            # No undocumented key guessing: only accept common explicit summary keys.
            candidates = []
            if isinstance(payload, dict):
                for key in ("total_count", "totalCount", "outage_count", "outageCount", "teidenKosu"):
                    if key in payload:
                        candidates.append(payload[key])
            if len(candidates) == 1:
                raw = str(candidates[0])
                if re.fullmatch(r"\s*[0-9][0-9,]*\s*", raw):
                    result["count"] = int(raw.replace(",", "").strip())
                    result["count_label"] = str(result["count"])
                    result["status"] = "ok"
                    result["evidence"] = "explicit_json_total"
                    return result
            raise ValueError("JSON応答に確認可能な全国・全エリア合計項目がありません")

        if "xml" in content_type or response.text.lstrip().startswith("<?xml"):
            # XML is parsed as text for conservative label-based total extraction.
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(" ", strip=True)
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            text = soup.get_text(" ", strip=True)

        count, label, evidence = extract_count(text)
        if count is None:
            if label:
                result["count_label"] = label
                result["status"] = "approximate"
                result["error"] = "公式表示が概数のため、全国合計には加算していません"
                result["evidence"] = evidence
                return result
            raise ValueError("公式ページから明確な全エリア合計を確認できませんでした（動的表示・表記変更の可能性）")

        result["count"] = count
        result["count_label"] = str(count)
        result["status"] = "ok"
        result["evidence"] = evidence
        return result

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] {company['name']}: {result['error']}", file=sys.stderr)
        return result

def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results = [None] * len(COMPANIES)
    with ThreadPoolExecutor(max_workers=min(10, len(COMPANIES))) as pool:
        future_map = {pool.submit(fetch_company, company): i for i, company in enumerate(COMPANIES)}
        for future in as_completed(future_map):
            results[future_map[future]] = future.result()

    successful = [item for item in results if item["status"] == "ok"]
    all_exact = len(successful) == len(COMPANIES)
    total_count = sum(item["count"] for item in successful) if all_exact else None

    output = {
        "updated_at": now_jst(),
        "total_count": total_count,
        "total_status": "ok" if all_exact else "incomplete",
        "successful_companies": len(successful),
        "company_count": len(COMPANIES),
        "companies": results,
        "note": "全国合計は10社すべてで正確な合計値を取得できた場合のみ表示します。取得失敗・概数は0として扱いません。",
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完了: 成功 {len(successful)}/{len(COMPANIES)} 社 / 全国合計: {total_count if total_count is not None else '未確定'} / {output['updated_at']}")

if __name__ == "__main__":
    main()
