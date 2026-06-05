#!/usr/bin/env python3
"""
AI 사이클 모니터 — 데이터 수집기 (v3)
시세 + 신용 종합패널 + 뉴스 + EDGAR 8-K(AI 본문요약) + 한 문장 요약 -> data.json
표준 라이브러리만. Python 3.9+
"""
import json, os, urllib.request, urllib.parse, re, time, html, gzip
from datetime import datetime, timezone

UA = {"User-Agent": "ai-cycle-monitor/1.0 (personal research)"}
SEC_UA = {"User-Agent": "ai-cycle-monitor jiskim.boop@gmail.com", "Accept-Encoding": "gzip, deflate"}
APIKEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
# 실러 CAPE 수동값 (자동 스크래이핑 실패 시 사용 — 월 1회 multpl.com 확인 후 갱신)
CAPE_MANUAL = 42.7  # 2026-06 기준

def get(url, headers=UA, timeout=25):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

def claude(prompt, max_tokens=200):
    if not APIKEY: return None
    try:
        body = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": APIKEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read().decode())
        return "".join(b.get("text", "") for b in j.get("content", [])).strip() or None
    except Exception:
        return None

# ---- 시세 (신용 종목 BIZD·ARCC·OBDC·HYG 추가)
PRICE_SYMBOLS = [
    # AI 밸류체인 — 자본 흐름 상류→하류
    "NVDA","AVGO","AMD","TSM","ASML","MRVL",            # 반도체·연산
    "MU","SNDK","STX","WDC","005930.KS","000660.KS",    # 메모리(미국+한국: 삼성·SK하이닉스)
    "ANET","ALAB","CRDO","VRT","CIEN","COHR",            # 네트워킹·DC장비
    "VST","CEG","NRG","TLN","GEV","PWR",                 # 전력·유틸리티
    "FCX","SCCO","CPER","CCJ","URA","UEC",               # 원자재(구리3·우라늄3)
    "MSFT","GOOGL","AMZN","META","ORCL",                 # 하이퍼스케일러
    "CRWV",                                              # 네오클라우드
    # 신용·사모대출
    "BIZD","ARCC","OBDC","HYG",
    # 거시·시스템
    "%5EVIX","%5EVIX3M","%5EVVIX","%5ESKEW","%5EMOVE","DX-Y.NYB","%5EIRX","LQD","SPY","QQQ","%5ETNX",
    # 한국 지수
    "%5EKS11","%5EKQ11",
    # 자금흐름
    "GLD","BTC-USD","USO","JPY=X",
    # 선물 (24시간 — 장외/주말 시장 방향)
    "ES=F","NQ=F","YM=F","GC=F","CL=F","HG=F","ZN=F","EWY",
]
def fetch_quote(sym):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
    try:
        j=json.loads(get(url)); res=j["chart"]["result"][0]
        price=res["meta"]["regularMarketPrice"]
        closes=[c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        # 직전 거래일 종가 기준(배당락 조정된 chartPreviousClose는 BDC 등락률을 왜곡)
        prev=closes[-2] if len(closes)>1 else price
        sma=lambda n: round(sum(closes[-n:])/min(n,len(closes)),2) if closes else None
        chg=round((price/prev-1)*100,2) if prev else 0
        # 최근 5거래일 누적 변화율 (단기 모멘텀)
        chg5=round((price/closes[-6]-1)*100,2) if len(closes)>5 else None
        return {"price":round(price,2),"chg":chg,"chg5":chg5,
                "sma20":sma(20),"sma50":sma(50),"sma200":sma(200),
                "high3m":round(max(closes[-63:]),2) if closes else None,"ok":True}
    except Exception as e:
        return {"ok":False,"err":str(e)[:120]}
def fetch_prices():
    out={}
    for s in PRICE_SYMBOLS: out[s.replace("%5E","^")]=fetch_quote(s); time.sleep(0.6)
    return out

# ---- 뉴스
NEWS_QUERIES={
 "credit":['"private credit" redemption gate','"private credit" NAV discount',
   'BDC default OR markdown OR "non-accrual"','CoreWeave debt OR refinancing',
   'data center "asset-backed" OR ABS AI',
   '"Blue Owl" OR Blackstone OR Apollo private credit stress'],
 "fundamental":['hyperscaler capex guidance','Microsoft OR Amazon OR Meta OR Oracle capex AI',
   'AI capex cut OR slowdown OR depreciation'],
 "macro":['Fed rate decision OR FOMC','recession risk yield curve',
   'VIX market volatility selloff','credit spreads widening'],
 "flow":['dollar index DXY move','oil price WTI OR crude',
   'copper price OR commodities','Japan yen carry trade','gold price safe haven'],
}
TRIGGER=re.compile(r"\b(gate|redemption|default|downgrade|markdown|non-accrual|cut|miss|slowdown|write-?down|distress|halt)\b",re.I)
def clean(t): return html.unescape(re.sub("<[^>]+>"," ",t)).strip()
def nomd(s):
    s=re.sub(r'[#*`>_]+',' ',s)      # 마크다운 기호 제거
    return re.sub(r'\s+',' ',s).strip()
def fetch_news_query(q):
    url="https://news.google.com/rss/search?q="+urllib.parse.quote(q+" when:14d")+"&hl=en-US&gl=US&ceid=US:en"
    items=[]
    try:
        xml=get(url)
        for m in re.finditer(r"<item>(.*?)</item>",xml,re.S):
            b=m.group(1)
            title=clean((re.search(r"<title>(.*?)</title>",b,re.S) or [None,""])[1])
            link=clean((re.search(r"<link>(.*?)</link>",b,re.S) or [None,""])[1])
            pub=clean((re.search(r"<pubDate>(.*?)</pubDate>",b,re.S) or [None,""])[1])
            src=clean((re.search(r"<source[^>]*>(.*?)</source>",b,re.S) or [None,""])[1])
            if title: items.append({"title":title,"link":link,"pub":pub,"src":src,"trig":bool(TRIGGER.search(title))})
    except Exception as e:
        return [{"title":f"[수집 실패] {str(e)[:80]}","link":"","pub":"","src":"","trig":False}]
    return items
def dedupe(items):
    seen,out=set(),[]
    for it in items:
        k=re.sub(r"[^a-z0-9]","",it["title"].lower())[:60]
        if k in seen: continue
        seen.add(k); out.append(it)
    return out
def parse_date(s):
    for f in ("%a, %d %b %Y %H:%M:%S %Z","%a, %d %b %Y %H:%M:%S %z"):
        try: return datetime.strptime(s,f).timestamp()
        except Exception: pass
    return 0
def fetch_news():
    out={}
    for cat,qs in NEWS_QUERIES.items():
        items=[]
        for q in qs: items+=fetch_news_query(q); time.sleep(0.5)
        items=dedupe(items); items.sort(key=lambda x:parse_date(x["pub"]),reverse=True)
        out[cat]=items[:6]
    return out

# ---- EDGAR 8-K + AI 본문 한 줄 요약
EDGAR_CIKS={"OBDC (Blue Owl Capital)":"0001655888","ARCC (Ares Capital)":"0001287750",
 "BXSL (Blackstone Secured Lending)":"0001736035","FSK (FS KKR)":"0001422183",
 "GBDC (Golub Capital)":"0001476765"}
ITEM_MAP={"1.01":"중요계약 체결","1.03":"파산/법정관리","2.02":"실적 발표","2.03":"채무/의무 발생",
 "3.01":"상장규정 위반","3.02":"증권 매각","5.02":"임원 변경","7.01":"Reg FD 공시",
 "8.01":"기타 중요사건","9.01":"재무제표/첨부"}
def edgar_recent_8k(cik):
    j=json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json",headers=SEC_UA))
    r=j["filings"]["recent"]
    for i,form in enumerate(r["form"]):
        if form.startswith("8-K"):
            accn=r["accessionNumber"][i].replace("-","")
            doc=r["primaryDocument"][i]
            link=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{doc}"
            return {"form":form,"date":r["filingDate"][i],"link":link}
    return None
def summarize_filing(link):
    # 본문 텍스트 추출
    try:
        txt=clean(get(link,headers=SEC_UA))
    except Exception:
        txt=""
    items=sorted(set(re.findall(r"Item\s+(\d+\.\d+)",txt)))
    item_label=", ".join(f"Item {x}"+(f"({ITEM_MAP[x]})" if x in ITEM_MAP else "") for x in items[:3])
    if APIKEY and len(txt)>250:
        s=claude("다음은 미국 BDC의 8-K 공시 본문이다. 무슨 사건인지 한국어 평문 한 문장(45자 내외)으로 핵심만. "
                 "마크다운 기호(#, *, 머리말) 쓰지 말고 문장만 출력. 실적·배당·인수·자금조달·소송·임원변경 등 위주.\n\n"+txt[:3500], max_tokens=120)
        if s:
            s=nomd(s)
            if not re.search(r"(미제공|불가능|내용이 (없|포함)|제목과 기본|확인할 수 없)", s):
                return s
    return item_label or "내용 분류 불가"
def fetch_edgar():
    out=[]
    today=datetime.now(timezone.utc).date()
    for name,cik in EDGAR_CIKS.items():
        try:
            rec=edgar_recent_8k(cik)
            if rec:
                rec["summary"]=summarize_filing(rec["link"])
                try:
                    d=datetime.strptime(rec["date"],"%Y-%m-%d").date()
                    rec["new"]=(today-d).days<=3
                except Exception: rec["new"]=False
                out.append({"name":name,**rec})
        except Exception as e:
            out.append({"name":name,"form":"ERR","date":str(e)[:50],"link":"","summary":"","new":False})
        time.sleep(0.6)
    out.sort(key=lambda x:x.get("date",""),reverse=True)
    return out

# ---- 한 문장 요약
def summarize_news(news):
    hc=[it["title"] for it in news.get("credit",[]) if not it["title"].startswith("[")][:10]
    hf=[it["title"] for it in news.get("fundamental",[]) if not it["title"].startswith("[")][:6]
    if APIKEY and (hc or hf):
        s=claude("다음은 AI 인프라 신용·사모대출 환매(CREDIT)와 하이퍼스케일러 capex(FUNDAMENTAL) 관련 최신 영문 "
            "헤드라인이다. 현재 상황을 한국어 평문 한 문장(80자 내외)으로 과장 없이 사실 위주 요약. 신용 스트레스 정도와 "
            "capex 흐름을 함께. 마크다운 기호(#, *, 머리말)나 제목 없이 한 문장만 출력.\n\n[CREDIT]\n"+"\n".join("- "+h for h in hc)+"\n\n[FUNDAMENTAL]\n"+"\n".join("- "+h for h in hf))
        if s: return {"text":nomd(s),"by":"claude"}
    trig=sum(1 for it in news.get("credit",[]) if it.get("trig"))
    n=len([it for it in news.get("credit",[]) if not it["title"].startswith("[")])
    lvl=("신용 경보 다수" if trig>=3 else "신용 경계 일부" if trig>=1 else "신용 특이신호 적음")
    return {"text":f"신용 뉴스 {n}건 중 트리거 {trig}건 — {lvl}. (AI 요약은 API 키 설정 시)","by":"heuristic"}

# ---- FRED 유동성·시스템 스트레스 (선행지표)
FRED_SERIES = {
    "nfci":      "NFCI",          # 금융상황지수 (양수=긴축)
    "nfcicredit":"NFCICREDIT",    # 신용 서브지수 (조기경보)
    "hyoas":     "BAMLH0A0HYM2",  # HY 옵션조정스프레드 (%)
    "sofr":      "SOFR",          # 레포 금리
    "iorb":      "IORB",          # 지준부리금리
    # 구조적 버블 압력 (분기/월간, 느림)
    "dsr":       "TDSP",          # 가계 부채상환비율 (%)
    "hhdebt":    "HDTGPDUSQ163N", # 가계부채/GDP (%)
    "ffr":       "DFF",           # 연방기금금리 (%)
}
def fred_latest(series_id):
    if not FRED_KEY: return None
    url=("https://api.stlouisfed.org/fred/series/observations?series_id="+series_id+
         "&api_key="+FRED_KEY+"&file_type=json&sort_order=desc&limit=8")
    try:
        raw=get(url)
        if raw is None: return None
        obs=json.loads(raw).get("observations",[])
        vals=[(o["date"],float(o["value"])) for o in obs if o["value"] not in (".","")]
        if not vals: return None
        date,latest=vals[0]
        prev=vals[1][1] if len(vals)>1 else None
        return {"date":date,"value":round(latest,3),
                "prev":round(prev,3) if prev is not None else None,
                "chg":round(latest-prev,3) if prev is not None else None}
    except Exception:
        return None

def fetch_cape():
    # 실러 CAPE — multpl.com 스크래이핑 시도 (403 차단 잦음 → 실패 시 수동값 사용)
    for url in ["https://www.multpl.com/shiller-pe/table/by-month",
                "https://www.multpl.com/shiller-pe"]:
        try:
            raw=get(url, headers={"User-Agent":"Mozilla/5.0 (research)"})
            if not raw: continue
            m=re.search(r'Current Shiller PE Ratio[^0-9]*([0-9]+\.[0-9]+)', raw)
            if m: return round(float(m.group(1)),2)
        except Exception:
            continue
    return None

def fetch_fred():
    if not FRED_KEY:
        return {"ok":False,"note":"FRED_API_KEY 미설정"}
    out={"ok":True}
    for k,sid in FRED_SERIES.items():
        out[k]=fred_latest(sid); time.sleep(0.2)
    # SOFR-IORB 스프레드 (레포 경색 근사, bp)
    try:
        if out.get("sofr") and out.get("iorb"):
            out["sofr_iorb"]=round((out["sofr"]["value"]-out["iorb"]["value"])*100,1)  # bp
    except Exception:
        out["sofr_iorb"]=None
    # 실러 CAPE (스크래이핑 시도 → 실패 시 수동값)
    _cape=fetch_cape()
    out["cape"]=_cape if _cape is not None else CAPE_MANUAL
    out["cape_manual"]=(_cape is None)
    return out

def main():
    news=fetch_news()
    data={"updated":datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "prices":fetch_prices(),"news":news,"edgar":fetch_edgar(),
          "fred":fetch_fred(),"summary":summarize_news(news)}
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=1)
    print("data.json 저장:",data["updated"],"| summary:",data["summary"]["by"],"| fred:",data["fred"].get("ok"))

if __name__=="__main__":
    main()
