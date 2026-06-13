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
GC_TOKEN = os.environ.get("GOATCOUNTER_API_TOKEN", "").strip()
GC_SITE = os.environ.get("GOATCOUNTER_SITE", "https://jiskim.goatcounter.com").strip()
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
    "CRWV","NBIS",                                        # 네오클라우드·GPU임대 (수급 프록시)
    "SPCX",                                               # SpaceX·xAI (상장 — 표시 전용, 바스켓 미포함)
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
FUT_SET = {"ES=F","NQ=F","YM=F","GC=F","CL=F","HG=F","ZN=F"}
def _chart_json(sym):
    # 야후 chart: query1 404 시 query2로 재시도 (^VIX 등 간헐적 단일심볼 404 대응)
    last=None
    for host in ("query1","query2"):
        url=f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d&includePrePost=true"
        try:
            j=json.loads(get(url))
            if j.get("chart",{}).get("result"): return j
            last=Exception("empty result")
        except Exception as e:
            last=e
            if "404" not in str(e): break   # 404가 아니면 호스트 바꿔도 무의미
    raise last or Exception("chart fetch failed")

def fetch_quote(sym):
    try:
        j=_chart_json(sym); res=j["chart"]["result"][0]
        meta=res["meta"]
        reg=meta.get("regularMarketPrice")
        pre=meta.get("preMarketPrice"); post=meta.get("postMarketPrice")
        # 현재가: 애프터>프리>정규 (장외 변동 반영). meta 값이 가장 최신 체결가.
        price = post if post is not None else (pre if pre is not None else reg)
        closes=[c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        sma=lambda n: round(sum(closes[-n:])/min(n,len(closes)),2) if closes else None
        is_fut = sym in FUT_SET
        # 등락률 기준선:
        # - 장외(애프터/프리) 가격이면 → 오늘 정규장 종가(closes[-1]) 대비 (장외 변동만)
        # - 정규장/선물이면 → 전 거래일 종가(closes[-2]) 대비
        # 주의: meta.chartPreviousClose는 '6개월 차트 시작 직전'이라 어제 종가 아님 → 사용 금지
        extended = (post is not None) or (pre is not None)
        if extended and len(closes)>=1:
            prev = closes[-1]
        elif len(closes)>1:
            prev = closes[-2]
        else:
            prev = reg
        chg=round((price/prev-1)*100,2) if prev else 0
        chg5=round((price/closes[-6]-1)*100,2) if len(closes)>5 else None
        # 세션: 선물은 거의 24시간이라 '24h'로, 주식·지수는 pre/post/reg
        if is_fut:
            sess = "24h"
        else:
            sess = "post" if post is not None else ("pre" if pre is not None else "reg")
        # 가격 신선도(체결시각) — 디버그/표시용
        mt = meta.get("regularMarketTime")
        return {"price":round(price,2),"chg":chg,"chg5":chg5,
                "sma20":sma(20),"sma50":sma(50),"sma200":sma(200),
                "high3m":round(max(closes[-63:]),2) if closes else None,
                "sess":sess,"mt":mt,"ok":True}
    except Exception as e:
        # 폴백: quote API에서 현재가/전일종가만이라도 확보 (chart 404 우회)
        try:
            qj=json.loads(get(f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={sym}"))
            r=(qj.get("quoteResponse",{}).get("result") or [None])[0]
            if r and r.get("regularMarketPrice") is not None:
                price=r.get("postMarketPrice") or r.get("preMarketPrice") or r["regularMarketPrice"]
                prevc=r.get("regularMarketPreviousClose")
                chg=round((price/prevc-1)*100,2) if prevc else r.get("regularMarketChangePercent")
                return {"price":round(price,2),"chg":chg,"chg5":None,
                        "sma20":None,"sma50":None,"sma200":None,"high3m":None,
                        "sess":"reg","mt":r.get("regularMarketTime"),"ok":True,"degraded":True}
        except Exception: pass
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
   'AI capex cut OR slowdown OR depreciation',
   'H100 OR H200 OR GPU rental price','HBM memory shortage OR oversupply',
   'Nvidia GPU lead time OR allocation','TSMC CoWoS capacity OR utilization'],
 "macro":['Fed rate decision OR FOMC','recession risk yield curve',
   'VIX market volatility selloff','credit spreads widening',
   'geopolitical risk markets','Middle East war oil OR conflict markets',
   'military strike OR invasion markets','sanctions tariff escalation markets'],
 "flow":['dollar index DXY move','oil price WTI OR crude',
   'copper price OR commodities','Japan yen carry trade','gold price safe haven'],
}
TRIGGER=re.compile(r"\b(gate|redemption|default|downgrade|markdown|non-accrual|cut|miss|slowdown|write-?down|distress|halt)\b",re.I)
# 지정학·돌발 거시 중대 트리거 (강한 조합 — 오탐 최소화)
GEO_TRIGGER=re.compile(r"\b(invasion|invades?|airstrike|missile strike|nuclear|martial law|coup|oil embargo|strait of hormuz|declares? war|state of emergency|attack on)\b",re.I)
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
            if title: items.append({"title":title,"link":link,"pub":pub,"src":src,"trig":bool(TRIGGER.search(title)),"geo":bool(GEO_TRIGGER.search(title))})
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
def fetch_edgar(prev=None):
    out=[]
    today=datetime.now(timezone.utc).date()
    cache={}
    for e in ((prev or {}).get("edgar") or []):
        lk=e.get("link"); sm=e.get("summary")
        if lk and sm and e.get("form")!="ERR": cache[lk]=sm
    for name,cik in EDGAR_CIKS.items():
        try:
            rec=edgar_recent_8k(cik)
            if rec:
                if rec["link"] in cache:
                    rec["summary"]=cache[rec["link"]]; rec["_cached"]=True   # 동일 공시 → 캐시(비용0)
                else:
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
def summarize_news(news, prices=None, fred=None, prev=None):
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
    hyoas_v=None; nfci_v=None
    if fred and fred.get("ok"):
        if fred.get("hyoas") and fred["hyoas"].get("value") is not None:
            hyoas_v=fred["hyoas"]["value"]; snap.append(f"HY스프레드 {hyoas_v:.2f}%")
        if fred.get("nfci") and fred["nfci"].get("value") is not None:
            nfci_v=fred["nfci"]["value"]; snap.append(f"NFCI {nfci_v:+.2f}")
        if fred.get("cape") is not None:
            snap.append(f"실러CAPE {fred['cape']:.0f}")
    snaptxt=" · ".join(snap) if snap else "(지표 없음)"

    # ── 변동 감지 캐싱: 큰 변동 없으면 이전 요약 재사용(API 절약) ──
    # 핵심 지표 + 트리거 뉴스 개수로 '상태 지문' 생성
    trig_n=sum(1 for it in news.get("credit",[]) if it.get("trig"))
    geo_n=sum(1 for k in ("macro","flow") for it in news.get(k,[]) if it.get("geo"))
    geo_titles=[it["title"] for k in ("macro","flow") for it in news.get(k,[]) if it.get("geo")][:3]
    def rnd(v,step):  # 구간화 — 작은 변동은 같은 값으로
        return None if v is None else round(v/step)*step
    fp={
        "vix": rnd(vix,2),            # VIX 2p 단위
        "tnx": rnd(tnx,0.1),          # 금리 0.1% 단위
        "dxy": rnd(dxy,0.5),          # 달러 0.5 단위
        "spy": rnd(spy_c,1.0),        # S&P 등락 1% 단위
        "hyoas": rnd(hyoas_v,0.2),    # HY 0.2% 단위
        "nfci": rnd(nfci_v,0.1),
        "trig": trig_n,
        "geo": geo_n,                 # 지정학 중대 뉴스 — 뜨면 새로 요약
        "capex": len(hf),             # AI capex·GPU/HBM 수급 뉴스 개수
        "news": len(hc)+len(hf)+len(hm),
    }
    prev_fp = (prev or {}).get("summary",{}).get("_fp") if prev else None
    prev_text = (prev or {}).get("summary",{}).get("text") if prev else None
    prev_by = (prev or {}).get("summary",{}).get("by") if prev else None
    # 지문이 같고 이전이 AI 요약이면 → 재사용(호출 skip)
    if prev_fp==fp and prev_text and prev_by=="claude":
        return {"text":prev_text,"by":"claude","_fp":fp,"_cached":True}

    if APIKEY and (hc or hf or hm or snap):
        geo_block = ("\n\n[⚠ 지정학·돌발 거시 이벤트]\n"+"\n".join("- "+t for t in geo_titles)) if geo_titles else ""
        s=claude(
            "당신은 거시·신용 시장 애널리스트다. 아래 데이터로 '지금 시장 상황'을 한국어로 요약하되, "
            "정확히 2문장으로 작성하라.\n"
            "1문장: 거시·자금 흐름 — 금리·지정학·변동성(VIX)·신용·사모대출 중 지금 가장 중요한 것을 중심으로 '무엇을 의미하는지'.\n"
            "2문장: AI 사이클 — capex·GPU/HBM 수급·밸류체인 관련해 특이 흐름이 있으면 한 줄, 없으면 '특이 신호 없음' 수준으로 짧게.\n"
            "각 문장 50자 내외로 압축. 나열 금지, 핵심만. 지정학 이벤트가 있으면 1문장에서 우선 언급. "
            "과장·투자권유 없이 사실 위주. 마크다운·제목·번호 없이 두 문장만 이어서.\n\n"
            "[핵심 지표]\n"+snaptxt+"\n\n[신용·사모대출 뉴스]\n"+("\n".join("- "+h for h in hc) or "- 특이사항 없음")+
            "\n\n[AI capex·GPU/HBM 수급 뉴스]\n"+("\n".join("- "+h for h in hf) or "- 특이사항 없음")+
            "\n\n[거시 뉴스]\n"+("\n".join("- "+h for h in hm) or "- 특이사항 없음")+geo_block, max_tokens=250)
        if s: return {"text":nomd(s),"by":"claude","_fp":fp}
    # 폴백: 키 없을 때도 사람이 읽기 좋게 풀어서
    trig=sum(1 for it in news.get("credit",[]) if it.get("trig"))
    parts=[]
    if geo_n>0:
        parts.append(f"지정학 돌발 이벤트 {geo_n}건 — 유가·변동성 영향 주시")
    if vix is not None and vix>=20:
        parts.append(f"VIX {vix:.0f}로 변동성 {'공포 구간' if vix>=28 else '경계 수준'}")
    if fred and fred.get("ok") and fred.get("hyoas") and fred["hyoas"].get("value") is not None:
        hy=fred["hyoas"]["value"]
        if hy>=5: parts.append(f"HY스프레드 {hy:.1f}%로 신용 {'경색' if hy>=7 else '확대'}")
    if trig>0: parts.append(f"신용 트리거 {trig}건")
    if fred and fred.get("cape") is not None and fred["cape"]>=40:
        parts.append(f"CAPE {fred['cape']:.0f} 고밸류")
    if parts:
        body=" · ".join(parts[:2])
    else:
        body="특이 신호 적음"+(f" (VIX {vix:.0f}·안정)" if vix is not None else "")
    return {"text":body+".","by":"heuristic","_fp":fp}

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

def fetch_gpu_price(prev):
    """H100 시세(중앙값) 수집 — computeprices.com 무료 JSON(/api/v1/gpu-prices). 실패시 graceful.
    누적: 시간당 1포인트씩 최근 48개 (추세 그래프용)."""
    hist=((prev or {}).get("gpu") or {}).get("hist",[]) if prev else []  # 이전 이력은 gpu.hist에서
    median=None
    # computeprices.com 공개 엔드포인트 (무료, 키 없음, 시간당 60회)
    for url in ("https://computeprices.com/api/v1/gpu-prices",
                "https://computeprices.com/api/v1/prices"):
        try:
            raw=get(url, timeout=15)
            j=json.loads(raw)
            rows=j.get("data") if isinstance(j,dict) else (j if isinstance(j,list) else [])
            # H100 80GB on-demand 가격들 모아 중앙값
            prices=[]
            for r in rows:
                if not isinstance(r,dict): continue
                gpu=str(r.get("gpu","")).upper()
                pt=str(r.get("pricing_type","")).lower()
                pr=r.get("price_per_hour_usd") or r.get("price")
                if "H100" in gpu and pr and (not pt or "on" in pt or pt=="on_demand"):
                    try: prices.append(float(pr))
                    except: pass
            if prices:
                prices.sort(); n=len(prices)
                median=round(prices[n//2] if n%2 else (prices[n//2-1]+prices[n//2])/2, 2)
                break
        except Exception:
            continue
    # 누적: 시간 단위 포인트 (실시간성). 같은 시(hour)면 덮어쓰고, 새 시간이면 추가. 최근 48개(약 2일).
    now=datetime.now(timezone.utc)
    hourkey=now.strftime("%Y-%m-%dT%H")    # 시간 단위 키
    if median is not None:
        if hist and hist[-1].get("d")==hourkey:
            hist[-1]={"d":hourkey,"v":median}
        else:
            hist.append({"d":hourkey,"v":median})
        hist=hist[-48:]
    return {"median":median,"hist":hist}

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

def _p_status(q):
    """클라이언트 pStatus 복제: -5%↓ 또는 20&50선 동시 아래=r, 한쪽=a"""
    if not q or not q.get("ok"): return None
    if q.get("chg") is not None and q["chg"]<=-5: return "r"
    pr=q.get("price"); b20=q.get("sma20") and pr<q["sma20"]; b50=q.get("sma50") and pr<q["sma50"]
    if b20 and b50: return "r"
    if b20 or b50: return "a"
    return "g"

def _group_status(p, syms):
    r=a=n=0
    for sym in syms:
        st=_p_status(p.get(sym))
        if st:
            n+=1
            if st=="r": r+=1
            elif st=="a": a+=1
    if not n: return None
    return "r" if r>n/2 else ("a" if (r+a)>n/2 else "g")

def _credit_status(p):
    """클라이언트 creditPanel 복제 (7종) — C게이트: 차주(CRWV)는 대주·부채 동반 약세 시에만 투표"""
    syms=["BIZD","ARCC","OBDC","CRWV","BKLN","SRLN","JBBB"]
    stats=[]
    for sym in syms:
        q=p.get(sym)
        st=_p_status(q)
        if st is None: continue
        weak5=q.get("chg5") is not None and q["chg5"]<=-1.5
        stats.append((sym,st,weak5))
    others=[t for t in stats if t[0]!="CRWV"]
    o_confirm=any(t[1]=="r" for t in others) or any(t[2] for t in others)
    use=stats if o_confirm else others   # 단독 차주 약세 = AI 베타 노이즈 → 제외
    n=len(use)
    if not n: return None
    r=sum(1 for t in use if t[1]=="r"); early=sum(1 for t in use if t[2])
    major=r>n/2; trend=early>=2
    if major and trend: return "r"
    if major or trend: return "a"
    if r>=1 or early>=1: return "a"
    return "g"

def _chain_status(p):
    """클라이언트 chainBasket 복제 (그룹 2단 과반)"""
    groups=[["MSFT","GOOGL","AMZN","META","ORCL"],
            ["NVDA","AVGO","AMD","TSM","ASML","MRVL"],
            ["MU","SNDK","STX","WDC","005930.KS","000660.KS"],
            ["ANET","ALAB","CRDO","VRT","CIEN","COHR"],
            ["VST","CEG","NRG","TLN","GEV","PWR"],
            ["FCX","SCCO","CPER","CCJ","URA","UEC"]]
    r=a=n=0
    for g in groups:
        st=_group_status(p,g)
        if st:
            n+=1
            if st=="r": r+=1
            elif st=="a": a+=1
    if not n: return None
    return "r" if r>n/2 else ("a" if (r+a)>n/2 else "g")

CHAIN_ALL=["MSFT","GOOGL","AMZN","META","ORCL","NVDA","AVGO","AMD","TSM","ASML","MRVL",
           "MU","SNDK","STX","WDC","005930.KS","000660.KS","ANET","ALAB","CRDO","VRT","CIEN","COHR",
           "VST","CEG","NRG","TLN","GEV","PWR","FCX","SCCO","CPER","CCJ","URA","UEC"]

def _breadth(p):
    """시장 폭: 추적 밸류체인 35종목 중 50일선 위 비율(%). (above_pct, n)"""
    above=n=0
    for sym in CHAIN_ALL:
        q=p.get(sym)
        if not q or not q.get("ok") or not q.get("sma50") or q.get("price") is None: continue
        n+=1
        if q["price"]>q["sma50"]: above+=1
    return (round(above/n*100) if n else None, n)

def _rising(arr, k=3):
    if not arr or len(arr)<k+1: return False
    t=arr[-(k+1):]
    for i in range(1,len(t)):
        if t[i] is None or t[i-1] is None or t[i]<=t[i-1]: return False
    return True

def _falling(arr, k=3):
    if not arr or len(arr)<k+1: return False
    t=arr[-(k+1):]
    for i in range(1,len(t)):
        if t[i] is None or t[i-1] is None or t[i]>=t[i-1]: return False
    return True

def calc_early(prices, fred, charts):
    """index.html v1.2와 '동일' 기준 — 알림·이력의 단일 진실원 동기화"""
    p=prices or {}; ch=charts or {}
    def g(sym):
        q=p.get(sym); return q.get("price") if q and q.get("ok") else None
    def gc(sym):
        q=p.get(sym); return q.get("chg") if q and q.get("ok") else None
    def gc5(sym):
        q=p.get(sym); return q.get("chg5") if q and q.get("ok") else None
    hits=[]; score=0.0; fast=slow=price_ax=False
    strong=0
    def add(lbl,w,ax):
        nonlocal score,fast,slow,price_ax,strong
        hits.append(lbl); score+=w
        if w>=1: strong+=1
        if ax=="fast": fast=True
        elif ax=="slow": slow=True
        elif ax=="price": price_ax=True
    # 사모대출 (선행 신용, 클라 sCredit)
    sCredit=_credit_status(p)
    if sCredit=="r": add("사모대출 주가 급약세",1,"slow")
    elif sCredit=="a": add("사모대출 주가 약세",0.5,"slow")
    # 강한 신호
    v=g("^VIX"); v3=g("^VIX3M")
    if v is not None and v3 and v/v3>=1: add("기간구조 역전",1,"fast")
    spy_c,qqq_c,es_c,nq_c=gc("SPY"),gc("QQQ"),gc("ES=F"),gc("NQ=F")
    cash_drop=(spy_c is not None and spy_c<=-2) or (qqq_c is not None and qqq_c<=-2)
    fut_drop=(es_c is not None and es_c<=-2) or (nq_c is not None and nq_c<=-2)
    if cash_drop or fut_drop:
        add("선물 급락(장외)" if (fut_drop and not cash_drop) else "시장 급락",1,"price")
    hy=(fred.get("hyoas") or {}).get("value") if fred and fred.get("ok") else None
    if hy is not None and hy>=5.5: add("HY스프레드 급등",1,"slow")
    vix_chg=gc("^VIX")
    if vix_chg is not None and vix_chg>=20: add("VIX 급등",1,"fast")
    # 보조 신호
    sk=g("^SKEW")
    if sk is not None and sk>=150: add("SKEW 급등",0.5,"fast")
    vv=g("^VVIX")
    if vv is not None and vv>=110: add("VVIX 급등",0.5,"fast")
    mv=g("^MOVE")
    if mv is not None and mv>=125: add("MOVE 급등",0.5,"fast")
    hyg5=gc5("HYG")
    if hyg5 is not None and hyg5<=-2: add("신용 급약화",0.5,"price")
    gld_c,dxy_c=gc("GLD"),gc("DX-Y.NYB")
    if gld_c is not None and gld_c>=2 and dxy_c is not None and dxy_c>=0.7: add("안전자산 쏠림",0.5,"price")
    nf=(fred.get("nfci") or {}).get("value") if fred and fred.get("ok") else None
    if nf is not None and nf>0.5: add("NFCI 긴축",0.5,"slow")
    # 선행 약세 (점수만, 축 미세팅 — 추세만으론 '위험' 불가)
    if not cash_drop and not fut_drop:
        spy5,qqq5=gc5("SPY"),gc5("QQQ")
        if (spy5 is not None and spy5<=-2) or (qqq5 is not None and qqq5<=-2):
            add("지수 추세 약세",0.5,None)
    sChain=_chain_status(p)
    if sChain in ("r","a"): add("AI 밸류체인 약세",0.5,None)
    if _rising(ch.get("hyoas")) and not (hy is not None and hy>=5.5): add("HY 상승추세",0.5,None)
    if _rising(ch.get("vix")) and not (v is not None and v>=26) and not (vix_chg is not None and vix_chg>=20):
        add("VIX 상승추세",0.5,None)
    spy_up=spy_c is not None and spy_c>=0
    hyg_weak=hyg5 is not None and hyg5<=-1
    if spy_up and hyg_weak: add("시장-신용 괴리",0.5,None)
    spyq=p.get("SPY")
    if spyq and spyq.get("ok") and spyq.get("high3m") and spyq.get("price"):
        near_high=spyq["price"]>=spyq["high3m"]*0.97
        b_pct,b_n=_breadth(p)
        if near_high and b_n>=20 and b_pct is not None and b_pct<45:
            add("폭 축소(쏠림 랠리)",0.5,None)   # 지수는 고점권인데 과반이 50일선 아래 = 좁아진 랠리(고전적 선행)
    # 조기징후 (점수 0, 2개+ → 주의)
    early_hits=[]
    if v is not None and v3 and 0.95<=v/v3<1: early_hits.append("기간구조 임박")
    if _falling(ch.get("bizd")): early_hits.append("신용프록시 약화")
    if v is not None and v<20 and sk is not None and sk>=145: early_hits.append("숨은 헤지")
    axisCount=(1 if fast else 0)+(1 if slow else 0)+(1 if price_ax else 0)
    guard_warn=(score>=2.5 and (strong>=1 or axisCount>=2)) or strong>=2
    risk = axisCount>=3 and score>=3 and strong>=2        # 위험 = 폭+깊이+질(강한2)
    if risk: st="r"
    elif guard_warn: st="r"                              # 경계
    elif score>=1: st="a"
    elif len(early_hits)>=2: st="a"
    else: st="g"
    return {"score":round(score,1),"axisCount":axisCount,"st":st,"hits":hits,"early":early_hits,"strong":strong,"risk":risk}

def update_history(prev, ew):
    """30일 일별 조기경보 이력 누적 (하루 1개, 최신값으로 갱신)"""
    hist = (prev or {}).get("history",[]) if prev else []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry={"d":today,"score":ew["score"],"st":ew["st"],"axis":ew["axisCount"],"hits":ew["hits"]}
    if hist and hist[-1].get("d")==today:
        hist[-1]=entry  # 오늘 것 갱신
    else:
        hist.append(entry)
    return hist[-30:]  # 최근 30일

def check_ipo_watch():
    """OpenAI/Anthropic 상장 자동 감지 — 야후 EQUITY 티커 등록 순간 포착. 실패=None(체크불가)."""
    out={"checked":datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for key,query in (("openai","OpenAI"),("anthropic","Anthropic")):
        try:
            j=json.loads(get(f"https://query1.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"))
            found=None
            for q in j.get("quotes",[]):
                nm=((q.get("shortname") or "")+" "+(q.get("longname") or "")).strip().lower()
                if q.get("quoteType")=="EQUITY" and nm.startswith(key):
                    found={"ticker":q.get("symbol"),"name":(q.get("shortname") or q.get("longname"))}; break
            out[key]={"found":bool(found),**(found or {})}
        except Exception:
            out[key]=None
    return out

def upcoming_events():
    """주요 거시 이벤트 — 발표 '시각'(미 동부)까지 반영해 24시간 이내면 D-DAY로 계산."""
    from zoneinfo import ZoneInfo
    ET=ZoneInfo("America/New_York")
    # (날짜, 이름, ET 발표시각). CPI 08:30 · FOMC 14:00 · 실적 16:00(장 마감 후)
    EVENTS=[
        # FOMC 성명 발표일(2일차) — 연준 공식 캘린더 검증(2026-06-10)
        ("2026-06-17","FOMC 금리결정","14:00"),
        ("2026-07-29","FOMC 금리결정","14:00"),
        ("2026-09-16","FOMC 금리결정","14:00"),
        ("2026-10-28","FOMC 금리결정","14:00"),
        ("2026-12-09","FOMC 금리결정","14:00"),
        # 미 CPI — BLS 공식 일정표 검증(2026-06-10)
        ("2026-06-10","미 CPI(5월)","08:30"),
        ("2026-07-14","미 CPI(6월)","08:30"),
        ("2026-08-12","미 CPI(7월)","08:30"),
        ("2026-09-11","미 CPI(8월)","08:30"),
        ("2026-10-14","미 CPI(9월)","08:30"),
        ("2026-11-10","미 CPI(10월)","08:30"),
        ("2026-12-10","미 CPI(11월)","08:30"),
        # 실적 — 회사 공시 전(통상 2~3주 전 확정)이라 예상치
        ("2026-07-23","하이퍼스케일러 실적 시작(예상)","16:00"),
        ("2026-08-27","NVDA 실적(예상)","16:00"),
    ]
    now=datetime.now(timezone.utc); today=now.date()
    out=[]
    for d,name,t in EVENTS:
        try:
            ed=datetime.strptime(d,"%Y-%m-%d").date()
            hh,mm=map(int,t.split(":"))
            rel=datetime(ed.year,ed.month,ed.day,hh,mm,tzinfo=ET).astimezone(timezone.utc)
            if (now-rel).total_seconds() > 12*3600:  # 발표 12시간 경과 → 제외
                continue
            out.append({"date":d,"name":name,"at":rel.isoformat(),"dday":max(0,(ed-today).days)})
        except Exception: pass
    out.sort(key=lambda x:x.get("at") or x["date"])
    return out[:6]


TG_TOKEN=os.environ.get("TELEGRAM_TOKEN","")
TG_CHAT=os.environ.get("TELEGRAM_CHAT_ID","")
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        import urllib.parse
        data=urllib.parse.urlencode({"chat_id":TG_CHAT,"text":text,"parse_mode":"HTML","disable_web_page_preview":"true"}).encode()
        req=urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",data=data)
        urllib.request.urlopen(req,timeout=10).read()
        return True
    except Exception as e:
        print("텔레그램 발송 실패:",str(e)[:100]); return False

def maybe_alert(prev, ew, summary, prices, fred):
    """위험 단계가 '상향 전환'될 때만 알림 (스팸 방지)."""
    # 조기경보 단계: g<a<r 순서 + 3축이면 최상
    order={"g":0,"a":1,"r":2}
    def tier(e):
        if not e: return 0
        if e.get("risk"): return 3                       # 위험 = 3축+점수3+강한2 (calc_early와 동일 기준)
        return order.get(e.get("st","g"),0)
    prev_ew=(prev or {}).get("early",{}) if prev else {}
    cur_t, prev_t = tier(ew), tier(prev_ew)
    label={0:"안정",1:"주의",2:"경계",3:"위험"}
    # 상향 전환(악화)일 때만 — 완화는 알림 안 함(스팸 방지)
    if cur_t>prev_t and cur_t>=1:
        hits=ew.get("hits",[])
        vix=(prices.get("^VIX") or {}).get("price")
        hy=(fred.get("hyoas") or {}).get("value") if fred and fred.get("ok") else None
        lines=[
            f"🚨 <b>폭락탐지기</b> — 단계 상향",
            f"<b>{label[prev_t]} → {label[cur_t]}</b>",
        ]
        if hits: lines.append("신호: "+" · ".join(hits[:4]))
        meta=[]
        if vix is not None: meta.append(f"VIX {vix:.0f}")
        if hy is not None: meta.append(f"HY {hy:.2f}%")
        if meta: lines.append(" / ".join(meta))
        if summary and summary.get("text"): lines.append("\n"+summary["text"][:120])
        lines.append("\n<a href='https://jiskim-boop.github.io/ai-monitor/'>대시보드 열기</a>")
        lines.append("<i>보조 경보입니다. 단독 매매 근거로 삼지 마세요.</i>")
        sent=tg_send("\n".join(lines))
        print("알림 발송:" , "성공" if sent else "실패/미설정", f"({label[prev_t]}→{label[cur_t]})")

def _gc_get(path, start, end):
    url = GC_SITE.rstrip("/") + path + "?start=" + start + "&end=" + end + "&limit=100"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + GC_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "ai-monitor"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def fetch_visit_stats(prev):
    """GoatCounter /stats/hits 로 누적 방문자 + 시간대(0~23시) 분포. 실패 시 직전 값 유지."""
    cv = (prev or {}).get("visitors")
    ch = (prev or {}).get("visit_hours")
    if not GC_TOKEN:
        print("[방문자] GOATCOUNTER_API_TOKEN 비어있음 → 시크릿(Repository/이름) 확인. 유지:", cv)
        return cv, ch
    start = "2020-01-01T00:00:00Z"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    try:
        j = _gc_get("/api/v0/stats/hits", start, end)
    except Exception as e:
        code = getattr(e, "code", ""); body = ""
        try:
            if hasattr(e, "read"): body = e.read().decode("utf-8", "replace")[:160]
        except Exception: pass
        print("[방문자] API 오류 /stats/hits:", code, str(e)[:100], body)
        return cv, ch
    # 총 방문자
    t = j.get("total")
    if isinstance(t, str): t = int(t.replace(",", "").strip() or 0)
    visitors = int(t) if isinstance(t, (int, float)) else cv
    # 시간대 합산 (경로별 stats[].hourly 24칸, 사이트 타임존 기준)
    hh = [0] * 24; got = False
    for hit in j.get("hits", []):
        for st in hit.get("stats", []):
            hr = st.get("hourly")
            if isinstance(hr, list) and len(hr) >= 24:
                for i in range(24):
                    try: hh[i] += int(hr[i] or 0)
                    except Exception: pass
                got = True
    hours = hh if got else ch
    print("[방문자] 수집:", visitors, "| 시간대합:", (sum(hh) if got else "응답에 hourly 없음"))
    return visitors, hours

def main():
    # 이전 data.json 로드 (요약 캐싱 + 이력 누적용)
    prev=None
    try:
        with open("data.json","r",encoding="utf-8") as f: prev=json.load(f)
    except Exception: prev=None
    news=fetch_news()
    prices=fetch_prices()
    fred=fetch_fred()
    # 피드 건강: 침묵 실패 방어 — 실패 종목·코어 생존 여부를 데이터에 동봉
    _failed=[k for k,v in prices.items() if not (isinstance(v,dict) and v.get("ok"))]
    _core_dead=[k for k in ("^VIX","SPY","QQQ","HYG") if k in _failed]
    feed={"fail":len(_failed),"failed":_failed[:12],"core_dead":_core_dead}
    if _core_dead or len(_failed)>=8:
        print("⚠ 피드 이상:", len(_failed), "실패", ("· 코어 사망: "+",".join(_core_dead)) if _core_dead else "")
    _pf=(prev or {}).get("feed",{}) if prev else {}
    _was_bad=bool(_pf.get("core_dead")) or _pf.get("fail",0)>=8
    _now_bad=bool(_core_dead) or len(_failed)>=8
    if _now_bad and not _was_bad:
        tg_send("🔌 <b>데이터 수집 이상</b>\n실패 "+str(len(_failed))+"종목"
                +(" · 코어 사망: "+", ".join(_core_dead) if _core_dead else "")
                +"\n대시보드 판정 신뢰 불가 — Actions 로그 확인 필요")
    ipo=check_ipo_watch()
    _pi=(prev or {}).get("ipo_watch",{}) if prev else {}
    for _k,_lb in (("openai","OpenAI"),("anthropic","Anthropic")):
        _c=(ipo.get(_k) or {}); _p2=(_pi.get(_k) or {}) if isinstance(_pi.get(_k),dict) else {}
        if _c.get("found") and not _p2.get("found"):
            tg_send(f"🚀 <b>{_lb} 상장 감지</b>\n티커: {_c.get('ticker')} ({_c.get('name')})\n야후 티커 등록 확인 — 검증 필요")
    charts=fetch_charts(fred)
    summary=summarize_news(news,prices,fred,prev)
    ew=calc_early(prices,fred,charts)
    history=update_history(prev,ew)
    gpu=fetch_gpu_price(prev)
    visitors, visit_hours = fetch_visit_stats(prev)
    data={"updated":datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "prices":prices,"news":news,"edgar":fetch_edgar(prev),
          "fred":fred,"charts":charts,"summary":summary,
          "early":ew,"history":history,"events":upcoming_events(),"gpu":gpu,"visitors":visitors,"visit_hours":visit_hours,"feed":feed,"breadth":dict(zip(("pct50","n"),_breadth(prices))),"ipo_watch":ipo}
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=1)
    # 위험 단계 상향 시 텔레그램 알림 (prev와 비교 — 저장 전 prev 사용)
    try: maybe_alert(prev, ew, summary, prices, fred)
    except Exception as e: print("알림 처리 오류:",str(e)[:100])
    cached=" (캐시재사용)" if summary.get("_cached") else ""
    print("data.json 저장:",data["updated"],"| summary:",data["summary"]["by"]+cached,"| 조기경보:",ew["st"],ew["score"],"| 이력:",len(history),"일")

if __name__=="__main__":
    main()
