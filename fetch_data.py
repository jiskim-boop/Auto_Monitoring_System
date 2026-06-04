#!/usr/bin/env python3
"""
AI 사이클 모니터 — 데이터 수집기
GitHub Actions가 하루 2회 실행. 시세·뉴스·EDGAR 8-K를 모아 data.json으로 저장.
대시보드(index.html)는 같은 폴더의 data.json만 읽으므로 CORS 문제 없음.
의존성 없음(표준 라이브러리만). Python 3.9+
"""
import json, urllib.request, urllib.parse, re, time, html
from datetime import datetime, timezone

UA = {"User-Agent": "ai-cycle-monitor/1.0 (personal research; contact: github)"}

def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ---------------------------------------------------------------- 시세
# 자동 색칠용 종목. 바스켓은 구성원 평균으로 계산.
PRICE_SYMBOLS = ["CRWV", "VST", "CEG", "NRG", "TLN", "NVDA", "AVGO", "SMH", "%5ETNX"]

def fetch_quote(sym):
    """야후 차트 API. 현재가 + 3개월 종가 배열 반환."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=6mo&interval=1d")
    try:
        j = json.loads(get(url))
        res = j["chart"]["result"][0]
        price = res["meta"]["regularMarketPrice"]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        def sma(n): return round(sum(closes[-n:]) / min(n, len(closes)), 2) if closes else None
        hi_3m = round(max(closes[-63:]), 2) if closes else None
        return {"price": round(price, 2), "sma50": sma(50), "sma200": sma(200),
                "high3m": hi_3m, "ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)[:120]}

def fetch_prices():
    out = {}
    for s in PRICE_SYMBOLS:
        out[s.replace("%5E", "^")] = fetch_quote(s)
        time.sleep(0.6)  # 야후 rate-limit 배려
    return out

# ---------------------------------------------------------------- 뉴스 (Google News RSS)
NEWS_QUERIES = {
    "credit": [
        '"private credit" redemption gate',
        '"private credit" NAV discount',
        'BDC default OR markdown OR "non-accrual"',
        'CoreWeave debt OR refinancing',
        'data center "asset-backed" OR ABS AI',
        '"Blue Owl" OR Blackstone OR Apollo private credit stress',
    ],
    "fundamental": [
        'hyperscaler capex guidance',
        'Microsoft OR Amazon OR Meta OR Oracle capex AI',
        'AI capex cut OR slowdown OR depreciation',
    ],
}
TRIGGER = re.compile(r"\b(gate|redemption|default|downgrade|markdown|non-accrual|"
                     r"cut|miss|slowdown|write-?down|distress|halt)\b", re.I)

def clean(t):
    return html.unescape(re.sub("<[^>]+>", "", t)).strip()

def fetch_news_query(q):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(q + " when:14d")
           + "&hl=en-US&gl=US&ceid=US:en")
    items = []
    try:
        xml = get(url)
        for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
            block = m.group(1)
            title = clean((re.search(r"<title>(.*?)</title>", block, re.S) or [None, ""])[1])
            link = clean((re.search(r"<link>(.*?)</link>", block, re.S) or [None, ""])[1])
            pub = clean((re.search(r"<pubDate>(.*?)</pubDate>", block, re.S) or [None, ""])[1])
            src = clean((re.search(r"<source[^>]*>(.*?)</source>", block, re.S) or [None, ""])[1])
            if title:
                items.append({"title": title, "link": link, "pub": pub, "src": src,
                              "trig": bool(TRIGGER.search(title))})
    except Exception as e:
        return [{"title": f"[수집 실패] {q}: {str(e)[:80]}", "link": "", "pub": "", "src": "", "trig": False}]
    return items

def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def parse_date(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except Exception:
            pass
    return 0

def fetch_news():
    out = {}
    for cat, queries in NEWS_QUERIES.items():
        items = []
        for q in queries:
            items += fetch_news_query(q)
            time.sleep(0.5)
        items = dedupe(items)
        items.sort(key=lambda x: parse_date(x["pub"]), reverse=True)
        out[cat] = items[:12]  # 카테고리당 최신 12개
    return out

# ---------------------------------------------------------------- EDGAR 8-K
# 주요 BDC/사모크레딧 운용사. 8-K(중대사건) 최신 공시 = 뉴스보다 빠른 1차 신호.
EDGAR_CIKS = {
    "OBDC (Blue Owl Capital)": "0001655888",
    "ARCC (Ares Capital)": "0001287750",
    "BXSL (Blackstone Secured Lending)": "0001736035",
    "FSK (FS KKR)": "0001422183",
    "GBDC (Golub Capital)": "0001476765",
}

def fetch_edgar():
    out = []
    for name, cik in EDGAR_CIKS.items():
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            j = json.loads(get(url))
            recent = j["filings"]["recent"]
            forms = recent["form"]; dates = recent["filingDate"]
            accns = recent["accessionNumber"]; docs = recent["primaryDocument"]
            for i, form in enumerate(forms):
                if form.startswith("8-K"):
                    accn = accns[i].replace("-", "")
                    link = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                            f"{accn}/{docs[i]}")
                    out.append({"name": name, "form": form, "date": dates[i], "link": link})
                    break  # 가장 최근 8-K 1건
        except Exception as e:
            out.append({"name": name, "form": "ERR", "date": str(e)[:60], "link": ""})
        time.sleep(0.4)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out

# ---------------------------------------------------------------- 메인
def main():
    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prices": fetch_prices(),
        "news": fetch_news(),
        "edgar": fetch_edgar(),
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("data.json 저장 완료:", data["updated"])

if __name__ == "__main__":
    main()
