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
    "BIZD","ARCC","OBDC","HYG","BKLN","SRLN","JBBB",
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

# ---- 한 문장 요약 (AI 섹터 + 거시 통합)
def summarize_news(news, prices=None, fred=None):
    hc=[it["title"] for it in news.get("credit",[]) if not it["title"].startswith("[")][:8]
    hf=[it["title"] for it in news.get("fundamental",[]) if not it["title"].startswith("[")][:5]
    hm=[it["title"] for it in news.get("macro",[]) if not it["title"].startswith("[")][:5]
    # 핵심 지표 스냅샷
    snap=[]
    p=prices or {}
    def g(sym):
        q=p.get(sym); return q.get("price") if q and q.get("ok") else None
    def gc(sym):
        q=p.get(sym); return q.get("chg") if q and q.get("ok") else None
    vix=g("^VIX"); v3=g("^VIX3M"); tnx=g("^TNX"); dxy=g("DX-Y.NYB")
    spy_c=gc("SPY"); qqq_c=gc("QQQ")
    if vix is not None:
        ts = (vix/v3) if (v3 and v3>0) else None
        snap.append(f"VIX {vix:.0f}" + (" (기간구조 역전)" if ts and ts>=1 else ""))
    if tnx is not None: snap.append(f"미10년 금리 {tnx:.2f}%")
    if dxy is not None: snap.append(f"달러 DXY {dxy:.1f}")
    if spy_c is not None: snap.append(f"S&P {spy_c:+.1f}%")
    if fred and fred.get("ok"):
        if fred.get("hyoas") and fred["hyoas"].get("value") is not None:
            snap.append(f"HY스프레드 {fred['hyoas']['value']:.2f}%")
        if fred.get("nfci") and fred["nfci"].get("value") is not None:
            snap.append(f"NFCI {fred['nfci']['value']:+.2f}")
        if fred.get("cape") is not None:
            snap.append(f"실러CAPE {fred['cape']:.0f}")
    snaptxt=" · ".join(snap) if snap else "(지표 없음)"
    if APIKEY and (hc or hf or hm or snap):
        s=claude(
            "당신은 거시·신용 시장 애널리스트다. 아래 데이터로 '지금 시장이 어떤 상황인지' 투자자에게 "
            "브리핑하듯 한국어로 2~3문장(200자 내외)으로 친절하고 자세히 설명하라. "
            "다음을 자연스럽게 녹여라: (1)변동성·옵션 시장이 보내는 신호 (2)신용·자금 시스템 상태 "
            "(3)AI 섹터 자본흐름 (4)금리·달러 등 거시 흐름 (5)밸류에이션 부담. "
            "단순 수치 나열이 아니라 '무엇을 의미하는지' 해석을 담되, 과장·투자권유 없이 사실 위주로. "
            "마크다운 기호나 제목 없이 본문만 출력.\n\n"
            "[핵심 지표]\n"+snaptxt+"\n\n[신용·사모대출 뉴스]\n"+("\n".join("- "+h for h in hc) or "- 특이사항 없음")+
            "\n\n[AI capex 뉴스]\n"+("\n".join("- "+h for h in hf) or "- 특이사항 없음")+
            "\n\n[거시 뉴스]\n"+("\n".join("- "+h for h in hm) or "- 특이사항 없음"), max_tokens=400)
        if s: return {"text":nomd(s),"by":"claude"}
    # 폴백: 키 없을 때도 사람이 읽기 좋게 풀어서
    trig=sum(1 for it in news.get("credit",[]) if it.get("trig"))
    parts=[]
    if vix is not None:
        if vix>=28: parts.append(f"변동성(VIX {vix:.0f})이 공포 구간으로 시장 불안이 큽니다")
        elif vix>=20: parts.append(f"변동성(VIX {vix:.0f})이 다소 높아 경계가 필요합니다")
        else: parts.append(f"변동성(VIX {vix:.0f})은 안정적입니다")
    if fred and fred.get("ok") and fred.get("hyoas") and fred["hyoas"].get("value") is not None:
        hy=fred["hyoas"]["value"]
        if hy>=7: parts.append(f"신용 스프레드(HY {hy:.1f}%)가 경색 구간입니다")
        elif hy>=5: parts.append(f"신용 스프레드(HY {hy:.1f}%)가 다소 벌어졌습니다")
        else: parts.append(f"신용 시스템(HY {hy:.1f}%)은 안정적입니다")
    if trig>0: parts.append(f"신용 트리거 뉴스가 {trig}건 감지됐습니다")
    if fred and fred.get("cape") is not None and fred["cape"]>=40:
        parts.append(f"다만 밸류에이션(CAPE {fred['cape']:.0f})은 역사적 극단이라 하락 시 충격이 클 수 있습니다")
    body = ". ".join(parts) if parts else "현재 특이 신호가 적습니다"
    return {"text":body+". (상세 AI 요약은 API 키 설정 시 제공)","by":"heuristic"}

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

# ---- 차트용 과거 시계열 (B: 과거 90일, 주간 다운샘플)
def fetch_series_yahoo(sym, points=13):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d"
    try:
        j=json.loads(get(url)); res=j["chart"]["result"][0]
        closes=[c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if not closes: return None
        if len(closes)<=points: return [round(c,2) for c in closes]
        step=len(closes)/points
        out=[round(closes[min(int(i*step),len(closes)-1)],2) for i in range(points)]
        out[-1]=round(closes[-1],2)
        return out
    except Exception:
        return None

def fred_series(series_id, points=13):
    if not FRED_KEY: return None
    url=("https://api.stlouisfed.org/fred/series/observations?series_id="+series_id+
         "&api_key="+FRED_KEY+"&file_type=json&sort_order=desc&limit=120")
    try:
        raw=get(url)
        if raw is None: return None
        obs=json.loads(raw).get("observations",[])
        vals=[float(o["value"]) for o in obs if o["value"] not in (".","")]
        if not vals: return None
        vals=vals[::-1]
        if len(vals)<=points: return [round(v,3) for v in vals]
        step=len(vals)/points
        out=[round(vals[min(int(i*step),len(vals)-1)],3) for i in range(points)]
        out[-1]=round(vals[-1],3)
        return out
    except Exception:
        return None

def fetch_charts(fred):
    ch={}
    # 야후 기반
    vix=fetch_series_yahoo("%5EVIX")
    vix3m=fetch_series_yahoo("%5EVIX3M")
    ch["vix"]=vix
    ch["tnx"]=fetch_series_yahoo("%5ETNX")
    ch["dxy"]=fetch_series_yahoo("DX-Y.NYB")
    ch["bizd"]=fetch_series_yahoo("BIZD")  # 사모대출 신용 프록시
    # VIX 기간구조 (VIX/VIX3M, >=1 역전=위험) — 두 시계열 길이 맞춰 비율
    if vix and vix3m and len(vix)==len(vix3m):
        ch["vixts"]=[round(a/b,3) if b else None for a,b in zip(vix,vix3m)]
    else:
        ch["vixts"]=None
    # FRED 기반
    ch["hyoas"]=fred_series("BAMLH0A0HYM2")
    ch["nfci"]=fred_series("NFCI")
    # SOFR-IORB (레포 경색, bp) — 두 시계열 차이
    sofr=fred_series("SOFR"); iorb=fred_series("IORB")
    if sofr and iorb and len(sofr)==len(iorb):
        ch["sofr_iorb"]=[round((a-b)*100,1) for a,b in zip(sofr,iorb)]
    else:
        ch["sofr_iorb"]=None
    cape=(fred or {}).get("cape")
    ch["cape"]=[cape]*8 if cape is not None else None
    return ch

def main():
    news=fetch_news()
    prices=fetch_prices()
    fred=fetch_fred()
    charts=fetch_charts(fred)
    data={"updated":datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "prices":prices,"news":news,"edgar":fetch_edgar(),
          "fred":fred,"charts":charts,"summary":summarize_news(news,prices,fred)}
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=1)
    print("data.json 저장:",data["updated"],"| summary:",data["summary"]["by"],"| fred:",data["fred"].get("ok"),"| charts:",sum(1 for v in charts.values() if v))

if __name__=="__main__":
    main()
