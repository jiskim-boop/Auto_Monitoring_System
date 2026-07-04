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
# 구글 뉴스 RSS는 봇 UA에 503(Service Unavailable)을 자주 반환 → 실제 브라우저 UA 사용
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
              "Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
              "Accept-Language": "en-US,en;q=0.9"}
APIKEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
GPU_KEY = os.environ.get("COMPUTEPRICES_KEY", "").strip()   # 선택: 무키 시간당60회→키 5000회
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
        # - 선물(24h): 직전 정산가 대비. 단 야후 일봉이 '현재 진행봉'을 closes[-1]에 넣을 때가 있어(그땐 closes[-1]≈현재가)
        #   → closes[-1]이 현재가와 사실상 같으면 진행봉으로 보고 closes[-2](직전 정산가)를, 다르면 closes[-1]을 기준.
        # - 장외(애프터/프리): 오늘 정규 종가(closes[-1]) 대비 (장외 변동만)
        # - 정규장 주식/지수: 전 거래일 종가(closes[-2]) 대비
        # 주의: meta.chartPreviousClose는 '6개월 차트 시작 직전'이라 어제 종가 아님 → 사용 금지
        extended = (post is not None) or (pre is not None)
        if is_fut and len(closes)>=2:
            prev = closes[-2] if abs(price-closes[-1]) < abs(closes[-1])*0.0003 else closes[-1]
        elif extended and len(closes)>=1:
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
                "reg":round(reg,2) if reg is not None else None,
                "pre":round(pre,2) if pre is not None else None,
                "post":round(post,2) if post is not None else None,
                "regClose":round(closes[-1],2) if closes else None,
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
   'Nvidia GPU lead time OR allocation','TSMC CoWoS capacity OR utilization',
   '"OpenAI" (revenue OR funding OR IPO OR lawsuit OR valuation OR compute OR shortfall)',
   '"Anthropic" (Claude OR revenue OR funding OR valuation OR IPO)',
   'SpaceX (Starship OR funding OR valuation OR IPO OR launch)',
   'xAI (Grok OR Musk OR funding OR valuation)'],
 "macro":['Fed rate decision OR FOMC','recession risk yield curve',
   'VIX market volatility selloff','credit spreads widening',
   'geopolitical risk markets','Middle East war oil OR conflict markets',
   'military strike OR invasion markets','sanctions tariff escalation markets'],
 "flow":['dollar index DXY move','oil price WTI OR crude',
   'copper price OR commodities','Japan yen carry trade','gold price safe haven'],
}
TRIGGER=re.compile(r"\b(gate|redemption|defaults?|downgrade[ds]?|markdowns?|non-accrual|cuts?|miss(es|ed)?|slowdowns?|write-?downs?|distress|halt|shortfalls?|lawsuits?|probes?|layoffs?|burn(ed|s)?|plunge[ds]?|warn(s|ed|ing)?|sued?)\b",re.I)
# 지정학·돌발 거시 중대 트리거 (강한 조합 — 오탐 최소화)
GEO_TRIGGER=re.compile(r"\b(invasion|invades?|airstrike|missile strike|nuclear|martial law|coup|oil embargo|strait of hormuz|declares? war|state of emergency|attack on)\b",re.I)
def clean(t): return html.unescape(re.sub("<[^>]+>"," ",t)).strip()
def nomd(s):
    s=re.sub(r'[#*`>_]+',' ',s)      # 마크다운 기호 제거
    return re.sub(r'\s+',' ',s).strip()
def _get_news(url, tries=3):
    """구글 뉴스 RSS: 브라우저 UA + 503/429 등 일시 오류 재시도(백오프)"""
    for i in range(tries):
        try:
            return get(url, headers=BROWSER_UA, timeout=20)
        except Exception as e:
            code=getattr(e,"code",None)
            if (code in (503,429,500,502,504) or code is None) and i<tries-1:
                time.sleep(1.2*(i+1)); continue   # 1.2s, 2.4s 백오프 후 재시도
            raise
def fetch_news_query(q):
    url="https://news.google.com/rss/search?q="+urllib.parse.quote(q+" when:14d")+"&hl=en-US&gl=US&ceid=US:en"
    items=[]
    try:
        xml=_get_news(url)
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
    "realrate":  "DFII10",        # 10Y 실질금리(TIPS) — AI 밸류에이션 동인
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

def fred_obs(series_id, limit=70):
    if not FRED_KEY: return None
    url=("https://api.stlouisfed.org/fred/series/observations?series_id="+series_id+
         "&api_key="+FRED_KEY+"&file_type=json&sort_order=desc&limit="+str(limit))
    try:
        raw=get(url)
        if raw is None: return None
        obs=json.loads(raw).get("observations",[])
        out=[(o["date"],float(o["value"])) for o in obs if o["value"] not in (".","")]
        return out or None
    except Exception:
        return None

def _asof(obs, target):
    for d,v in obs:
        if d<=target: return v
    return obs[-1][1] if obs else None

def fetch_netliq():
    # 순유동성[$bn] = WALCL(백만)/1000 - WTREGEN(백만)/1000 - RRPONTSYD(십억), 날짜 as-of 조인
    if not FRED_KEY: return None
    wal=fred_obs("WALCL",70); tga=fred_obs("WTREGEN",70); rrp=fred_obs("RRPONTSYD",70)
    if not (wal and tga and rrp): return None
    def nl(t):
        w=_asof(wal,t); x=_asof(tga,t); r=_asof(rrp,t)
        if w is None or x is None or r is None: return None
        return w/1000.0 - x/1000.0 - r   # WALCL·WTREGEN은 백만→십억, RRP는 이미 십억
    anchor=wal[0][0]
    import datetime
    d0=datetime.date.fromisoformat(anchor)
    d4=(d0-datetime.timedelta(days=28)).isoformat()
    now=nl(anchor); prev=nl(d4)
    if now is None: return None
    return {"value":round(now,1),"chg4w":round(now-prev,1) if prev is not None else None,"asof":anchor}

def ecb_obs(flow_key, n=24):
    # ECB Data Portal CSV → (기간, 값) 최신순. 값 단위는 시리즈 원단위(M2 잔액은 €백만).
    url=("https://data-api.ecb.europa.eu/service/data/"+flow_key+
         "?format=csvdata&lastNObservations="+str(n))
    try:
        raw=get(url)
        if not raw: return None
        lines=raw.strip().split("\n")
        if len(lines)<2: return None
        hdr=[h.strip() for h in lines[0].split(",")]
        ti=hdr.index("TIME_PERIOD"); vi=hdr.index("OBS_VALUE")
        out=[]
        for ln in lines[1:]:
            c=ln.split(",")
            if len(c)<=max(ti,vi): continue
            try: out.append((c[ti].strip(), float(c[vi])))
            except Exception: continue
        out.sort(reverse=True)
        return out or None
    except Exception:
        return None

def _norm_month(p):
    p=p.replace("M","-")                      # 2026M04 → 2026-04
    return (p+"-28") if len(p)==7 else p       # 월간을 월말 날짜로 (문자열 _asof 호환)

def fetch_g2_m2():
    # 글로벌 M2(선진권 실측) = 미국 M2(FRED WM2NS, $bn) + 유로존 M2(ECB BSI M20, €백만→$).
    # 미국 주간·유로존 월간 혼합, 시차 ~1개월. 환율 DEXUSEU(USD/EUR).
    # 중국·일본은 최신 무료 API가 없어(IMF/FRED 시차·동결) 제외 — 설명/주석 참조.
    if not FRED_KEY: return None
    us = fred_obs("WM2NS", 70)                                  # [(date,$bn)] 주간 최신순
    eu = ecb_obs("BSI/M.U2.Y.V.M20.X.1.U2.2300.Z01.E", 24)     # [(YYYY-MM,€백만)] 월간 최신순
    fx = fred_obs("DEXUSEU", 220)                               # [(date,USD/EUR)] 일간
    if not (us and eu and fx): return None
    import datetime
    eu=[(_norm_month(p), v) for p,v in eu]
    anchor=us[0][0]
    d0=datetime.date.fromisoformat(anchor)
    def back(n): return (d0 - datetime.timedelta(days=n)).isoformat()
    def total(t):                                              # 해당 시점 미국+유로존 합 ($T)
        u=_asof(us,t); e=_asof(eu,t); x=_asof(fx,t)
        if u is None or e is None or x is None or x==0: return None
        return (u + (e/1000.0)*x) / 1000.0                     # e/1000=€bn, ×x=$bn, +u, /1000=$T
    now=total(anchor)
    if now is None: return None
    p13=total(back(91))
    un,uo=_asof(us,anchor),_asof(us,back(91))
    en,eo=_asof(eu,anchor),_asof(eu,back(91))
    xn=_asof(fx,anchor)
    series=[]
    for k in range(12,-1,-1):
        tv=total(back(k*7)); series.append(round(tv,2) if tv is not None else None)
    return {"value": round(now,2), "series": series,
            "chg13w": round((now/p13-1)*100,2) if p13 else None,
            "us_chg13w": round((un/uo-1)*100,2) if (un and uo) else None,
            "eu_chg13w": round((en/eo-1)*100,2) if (en and eo) else None,
            "us_t": round(un/1000.0,2) if un else None,
            "eu_t": round((en/1000.0)*xn/1000.0,2) if (en and xn) else None,
            "asof": anchor, "eu_asof": eu[0][0]}

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
    out["netliq"]=fetch_netliq()
    out["gm2"]=fetch_g2_m2()
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

# ── 구조 취약성 패널 (표시 전용 · 판정 미반영) ───────────────────────────
def fred_series_dated(series_id, limit=1000):
    """FRED 원시 시계열 (날짜 포함, asc 정렬). 커브 재정상화 경과월·GDP 최신값용"""
    if not FRED_KEY: return None
    url=("https://api.stlouisfed.org/fred/series/observations?series_id="+series_id+
         "&api_key="+FRED_KEY+"&file_type=json&sort_order=desc&limit="+str(limit))
    try:
        raw=get(url)
        if raw is None: return None
        obs=json.loads(raw).get("observations",[])
        out=[(o["date"],float(o["value"])) for o in obs if o["value"] not in (".","")]
        return out[::-1] or None
    except Exception:
        return None

FINRA_HTML_HEADERS={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"}

def _parse_margin(html):
    """FINRA 표 텍스트에서 최신월 debit balance 추출 (내림차순 표라 첫 매치=최신)"""
    txt=re.sub(r"<[^>]+>"," ",html); txt=re.sub(r"\s+"," ",txt)
    m=re.search(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\-\.]{0,2}(?:20)?\d{2})\s*\$?\s*([\d,]{7,11})", txt)
    if m:
        val=int(m.group(2).replace(",",""))
        if 200_000<=val<=5_000_000: return val, m.group(1).strip()   # $200B~$5T sanity ($M 단위)
    return None,None

def fetch_finra_margin():
    """FINRA 마진부채 최신월 ($M). ①FINRA 직접 ②웨이백 스냅샷 폴백. 실패 시 (None,None,None) → 호출부 prev 유지"""
    wb="https://web.archive.org/web/"+datetime.now(timezone.utc).strftime("%Y%m%d")+"/https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"
    tries=[("finra","https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"),
           ("archive",wb)]
    for tag,u in tries:
        try:
            html=get(u, headers=FINRA_HTML_HEADERS, timeout=30)
            v,a=_parse_margin(html or "")
            print(f"[fragility] margin {tag}: len={len(html or '')} → {v} ({a})")
            if v: return v,a,tag
        except Exception as e:
            print(f"[fragility] margin {tag} 실패: {type(e).__name__} {str(e)[:80]}")
    return None,None,None

def fetch_ebp(prev):
    """연준 초과 채권 프리미엄(EBP) — GZ(2012) 스프레드 분해의 잔차 = 위험선호.
    월간 CSV(1973~ 전체 이력 매월 재배포, 4영업일 10시 이후 갱신, 전 이력 수정 가능).
    일 1회만 fetch(예의), 실패 시 이전값 유지. z·역대 정점은 전체 이력에서 산출(3년창 문제 없음)."""
    pe=(prev or {}).get("ebp") or {}
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if pe.get("fetched")==today and pe.get("val") is not None:
        return pe
    url="https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv"
    try:
        raw=get(url, headers=FINRA_HTML_HEADERS, timeout=30)
        rows=[r.strip() for r in (raw or "").replace("\r","").split("\n") if r.strip()]
        if len(rows)<24: raise ValueError("rows<24")
        hdr=[c.strip().strip('"').lower() for c in rows[0].split(",")]
        i_e=next((i for i,c in enumerate(hdr) if "ebp" in c), 1)
        i_p=next((i for i,c in enumerate(hdr) if "prob" in c), 2)
        data=[]
        for r in rows[1:]:
            cs=[c.strip().strip('"') for c in r.split(",")]
            if len(cs)<=i_e: continue
            try: e=float(cs[i_e])
            except Exception: continue
            p=None
            if len(cs)>i_p:
                try: p=float(cs[i_p])
                except Exception: p=None
            if abs(e)<10: data.append((cs[0],e,p))
        if len(data)<24: raise ValueError("data<24")
        vals=[e for _,e,_ in data]
        m=sum(vals)/len(vals); sd=(sum((v-m)**2 for v in vals)/len(vals))**0.5 or 1e-9
        cur_d,cur_e,cur_p=data[-1]
        if cur_p is not None and cur_p<=1.5: cur_p*=100.0   # 0~1 스케일이면 %로
        pk=max(data,key=lambda x:x[1])
        out={"val":round(cur_e,2),"z":round((cur_e-m)/sd,2),
             "prob":(round(cur_p,1) if cur_p is not None else None),
             "asof":cur_d,"mean":round(m,2),"sd":round(sd,2),"hist_n":len(data),
             "peak":round(pk[1],2),"peak_d":pk[0],
             "prev_val":(round(data[-2][1],2) if len(data)>=2 else None),
             "fetched":today,"src":"fed"}
        print("[ebp]",out["asof"],out["val"],"z",out["z"],"prob",out["prob"],"n",out["hist_n"])
        return out
    except Exception as e:
        print("[ebp] 실패:",type(e).__name__,str(e)[:80])
        if pe.get("val") is not None:
            pe["src"]="prev"; return pe
        return {"val":None,"src":None}

def build_fragility(prev):
    """취약성 패널 원자료: 마진부채/GDP + 커브(T10Y2Y) 상태. CAPE·HY·RSP/SPY는 charts에서 클라이언트가 직접 읽음"""
    fr={}
    mv,masof,msrc=fetch_finra_margin()
    pf=(prev or {}).get("fragility") or {}
    if mv is None:   # 스크랩 실패 → 월간 지표라 이전값 유지가 합리적
        fr["margin_mn"]=pf.get("margin_mn"); fr["margin_asof"]=pf.get("margin_asof"); fr["margin_src"]="prev" if pf.get("margin_mn") else None
    else:
        fr["margin_mn"]=mv; fr["margin_asof"]=masof; fr["margin_src"]=msrc
    gdp=fred_series_dated("GDP", limit=8)          # 명목 GDP ($B, SAAR) 최신 분기
    gdp_bn=gdp[-1][1] if gdp else None
    fr["margin_gdp_pct"]=round(fr["margin_mn"]/1000.0/gdp_bn*100,2) if (fr.get("margin_mn") and gdp_bn) else None
    cur=fred_series_dated("T10Y2Y", limit=1000)    # 일간 ~1000영업일 ≈ 4년 — '24.9 재정상화 포함
    if cur:
        last=cur[-1][1]
        fr["curve_bp"]=round(last*100); fr["curve_inverted"]=last<0
        neg=[d for d,v in cur if v<0]
        if fr["curve_inverted"]: fr["curve_uninv_months"]=0.0
        elif neg:
            dt=datetime.strptime(neg[-1],"%Y-%m-%d").date()
            fr["curve_uninv_months"]=round((datetime.now(timezone.utc).date()-dt).days/30.44,1)
        else:
            fr["curve_uninv_months"]=None   # 조회창 내 역전 없음(=충분히 오래 정상)
    else:
        fr["curve_bp"]=None; fr["curve_inverted"]=None; fr["curve_uninv_months"]=None
    print("[fragility] margin:",fr.get("margin_mn"),fr.get("margin_asof"),fr.get("margin_src"),
          "| margin/GDP:",fr.get("margin_gdp_pct"),"% | curve:",fr.get("curve_bp"),"bp · 재정상화",fr.get("curve_uninv_months"),"개월")
    return fr

def fetch_gpu_price(prev):
    """H100 시세(중앙값) 수집 — computeprices.com 무료 JSON(/api/v1/gpu-prices). 실패시 graceful.
    누적: 시간당 1포인트씩 최근 48개 (추세 그래프용)."""
    hist=((prev or {}).get("gpu") or {}).get("hist",[]) if prev else []  # 이전 이력은 gpu.hist에서
    median=None; stale=False
    hdr=dict(UA); 
    if GPU_KEY: hdr["X-API-Key"]=GPU_KEY   # 키 있으면 한도 상향(없으면 무키 60회/시)
    # computeprices.com 공개 엔드포인트 — 재시도 2회
    done=False
    for url in ("https://computeprices.com/api/v1/gpu-prices",
                "https://computeprices.com/api/v1/prices"):
        for attempt in range(2):
            try:
                raw=get(url, headers=hdr, timeout=15)
                j=json.loads(raw)
                rows=j.get("data") if isinstance(j,dict) else (j if isinstance(j,list) else [])
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
                    done=True; break
            except Exception:
                time.sleep(1); continue
        if done: break
    # 실패 시 마지막 성공값 유지(빈칸 방지) — 일시적 장애·한도초과 흡수
    if median is None and hist:
        last=[h.get("v") for h in hist if h.get("v") is not None]
        if last: median=last[-1]; stale=True
    # 누적: 시간 단위 포인트 (실시간성). 같은 시(hour)면 덮어쓰고, 새 시간이면 추가. 최근 48개(약 2일).
    now=datetime.now(timezone.utc)
    hourkey=now.strftime("%Y-%m-%dT%H")    # 시간 단위 키
    if median is not None:
        if hist and hist[-1].get("d")==hourkey:
            hist[-1]={"d":hourkey,"v":median}
        else:
            hist.append({"d":hourkey,"v":median})
        hist=hist[-48:]
    return {"median":median,"hist":hist,"stale":stale}

def fetch_charts(fred):
    ch={}
    # 야후 기반
    vix=fetch_series_yahoo("%5EVIX")
    vix3m=fetch_series_yahoo("%5EVIX3M")
    ch["vix"]=vix
    ch["tnx"]=fetch_series_yahoo("%5ETNX")
    ch["dxy"]=fetch_series_yahoo("DX-Y.NYB")
    ch["bizd"]=fetch_series_yahoo("BIZD")  # 사모대출 신용 프록시
    ch["move"]=fetch_series_yahoo("%5EMOVE")  # 채권 변동성(신용 확정 보조)
    ch["jpy"]=fetch_series_yahoo("JPY=X")
    ch["uso"]=fetch_series_yahoo("USO")
    ch["cper"]=fetch_series_yahoo("CPER")
    ch["gld"]=fetch_series_yahoo("GLD")
    ch["btc"]=fetch_series_yahoo("BTC-USD")
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
    # 신용 품질 분산 (CCC−BB, %p) — 부실 집중도 선행: CCC가 먼저·크게 벌어짐. 확대=조기 신용경색 신호(HY보다 이름)
    ccc=fred_series("BAMLH0A3HYC"); bb=fred_series("BAMLH0A1HYBB")
    if ccc and bb and len(ccc)==len(bb):
        ch["ccc_bb"]=[round(a-b,2) if (a is not None and b is not None) else None for a,b in zip(ccc,bb)]
    else:
        ch["ccc_bb"]=None
    # 시장 폭 (RSP/SPY 동일가중/시총가중 비율) — 하락=소수 대형주가 지수 떠받침(폭 narrowing). '00·'07 정점 공통 선행
    rsp=fetch_series_yahoo("RSP"); spy=fetch_series_yahoo("SPY")
    if rsp and spy and len(rsp)==len(spy):
        ch["rsp_spy"]=[round(a/b,4) if b else None for a,b in zip(rsp,spy)]
    else:
        ch["rsp_spy"]=None
    # 금융주 상대강도 (XLF/SPY) — '07 금융섹터가 지수 정점 수개월 전 이탈·시스템 붕괴 주도, '23 지역은행이 SVB 수주 선행. 신용경색의 주식측 카나리아
    xlf=fetch_series_yahoo("XLF")
    if xlf and spy and len(xlf)==len(spy):
        ch["xlf_spy"]=[round(a/b,4) if b else None for a,b in zip(xlf,spy)]
    else:
        ch["xlf_spy"]=None
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

def ew_inputs(prices,fred,charts):
    """R3 출석부: calc_early가 실제 소비하는 입력의 가용성 — 결측=해당 신호 미평가(점수 하한 편향).
    ⚠ calc_early에 신호 추가/삭제 시 이 목록도 함께 갱신(패치 시점 기계 추출로 동기화됨)."""
    P=prices or {}; F=fred or {}; C=charts or {}
    _p=lambda k: bool(P.get(k) and P[k].get("ok") and P[k].get("price") is not None)
    _f=lambda k: bool((F.get(k) or {}).get("value") is not None)
    _c=lambda k: bool(C.get(k))
    items=[("DX-Y.NYB",_p("DX-Y.NYB")),("ES=F",_p("ES=F")),("GLD",_p("GLD")),("HYG",_p("HYG")),("NQ=F",_p("NQ=F")),("QQQ",_p("QQQ")),("SPY",_p("SPY")),("MOVE",_p("^MOVE")),("SKEW",_p("^SKEW")),("VIX",_p("^VIX")),("VIX3M",_p("^VIX3M")),("VVIX",_p("^VVIX")),("HYOAS",_f("hyoas")),("NFCI",_f("nfci")),("차트:bizd",_c("bizd")),("차트:ccc_bb",_c("ccc_bb")),("차트:hyoas",_c("hyoas")),("차트:rsp_spy",_c("rsp_spy")),("차트:vix",_c("vix")),("차트:xlf_spy",_c("xlf_spy"))]
    missing=[n for n,okv in items if not okv]
    return {"total":len(items),"ok":len(items)-len(missing),"missing":missing}

def schema_gate(prev_exists, prev, ew, history, klr):
    """R3 현관 검문(구조 보호): 깨진 실행이 좋은 데이터를 덮어쓰지 않게 저장 전 차단.
    소스 결측(야후/FRED 다운)은 기존 feed 헬스 라인 담당 — 여기선 구조만 본다.
    차단 4항: ① 판정 부재 ② 기존 파일 파싱실패(이력·원장 소실 위험) ③ 이력 소실 ④ KLR 원장 축소(append-only 위반)"""
    miss=[]
    if not (ew or {}).get("st"): miss.append("early.st 부재")
    if prev_exists and prev is None: miss.append("이전 data.json 파싱 실패")
    if not history: miss.append("history 소실")
    _pk=len((((prev or {}).get("klr") or {}).get("entries")) or [])
    _ck=len(((klr or {}).get("entries")) or [])
    if _ck<_pk: miss.append(f"KLR 원장 축소({_pk}→{_ck})")
    return miss

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
    strong=0; lead_score=0.0   # 선행약세(축 없음) 별도 누적 — 캡 대상(JS와 동일)
    def add(lbl,w,ax):
        nonlocal score,fast,slow,price_ax,strong,lead_score
        hits.append(lbl)
        if w>=1: strong+=1
        if ax=="fast": fast=True; score+=w
        elif ax=="slow": slow=True; score+=w
        elif ax=="price": price_ax=True; score+=w
        else: lead_score+=w   # 선행약세 → 별도 누적
    # 사모대출 (선행 신용, 클라 sCredit)
    sCredit=_credit_status(p)
    if sCredit=="r": add("사모대출 주가 급약세",1,"slow")
    elif sCredit=="a": add("사모대출 주가 약세",0.5,"slow")
    # 강한 신호
    v=g("^VIX"); v3=g("^VIX3M")
    if v is not None and v3 and v/v3>=1.02 and v>=20: add("기간구조 역전",1,"fast")  # 마진+VIX레벨
    spy_c,qqq_c,es_c,nq_c=gc("SPY"),gc("QQQ"),gc("ES=F"),gc("NQ=F")
    cash_drop=(spy_c is not None and spy_c<=-2) or (qqq_c is not None and qqq_c<=-2)
    fut_drop=(es_c is not None and es_c<=-2) or (nq_c is not None and nq_c<=-2)
    if cash_drop or fut_drop:
        add("선물 급락(장외)" if (fut_drop and not cash_drop) else "시장 급락",1,"price")
    hy=(fred.get("hyoas") or {}).get("value") if fred and fred.get("ok") else None
    if hy is not None and hy>=5.0: add("HY스프레드 급등",1,"slow")  # F2: 위험선 5.0
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
    if _rising(ch.get("hyoas")) and not (hy is not None and hy>=5.0): add("HY 상승추세",0.5,None)  # F2
    if _rising(ch.get("vix")) and not (v is not None and v>=26) and not (vix_chg is not None and vix_chg>=20):
        add("VIX 상승추세",0.5,None)
    if _rising(ch.get("ccc_bb")): add("CCC-BB 확대(품질분산)",0.5,None)   # JS earlyWarning과 동일 미러
    if _falling(ch.get("rsp_spy")): add("시장 폭 악화(RSP/SPY)",0.5,None)  # JS earlyWarning과 동일 미러
    _s5,_q5=gc5("SPY"),gc5("QQQ")
    _idxW5=(_s5 is not None and _s5<=-2) or (_q5 is not None and _q5<=-2)
    if _falling(ch.get("xlf_spy")) and not _idxW5: add("금융 상대약세(XLF/SPY)",0.5,None)  # JS 미러 · '07형 다이버전스(지수 정상+금융만 약세)
    def _trough_up(arr,cap):   # 저점이탈: 90일 저점 대비 +30% 이탈 & 레벨 조용 — '07.6 형태 (JS troughUp 미러)
        vv=[x for x in (arr or []) if x is not None]
        if len(vv)<5: return False
        lo=min(vv)
        return lo>0 and vv[-1]>=lo*1.3 and vv[-1]<cap
    if _trough_up(ch.get("hyoas"),5.0): add("HY 저점이탈(+30%)",0.5,None)
    if _trough_up(ch.get("ccc_bb"),12): add("CCC-BB 저점이탈(+30%)",0.5,None)
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
    if v is not None and v3 and v/v3>=0.95 and not (v/v3>=1.02 and v>=20): early_hits.append("기간구조 임박")
    if _falling(ch.get("bizd")): early_hits.append("신용프록시 약화")
    if v is not None and v<20 and sk is not None and sk>=145: early_hits.append("숨은 헤지")
    # 레짐 지속성: 지수가 3개월 고점 대비 -4%+ 낙폭이면 조정 진행 중 — 급성 스파이크 풀려도 유지 (JS와 동일)
    def _dd(o):
        if o and o.get("ok") and o.get("high3m") and o.get("price") is not None:
            return (o["price"]/o["high3m"]-1)*100
        return None
    _dds=[x for x in (_dd(p.get("SPY")),_dd(p.get("QQQ"))) if x is not None]
    regime_dd=min(_dds) if _dds else None
    regime_active = regime_dd is not None and regime_dd<=-4
    if regime_active: add("조정 지속(고점 %.1f%%)"%regime_dd,0.5,None)  # 점수 0.5 → 주의 기여, 축 미세팅이라 위험엔 X
    # 다이버전스 엔진: 지수 고점권인데 내부(폭·금융·HY) 2+ 미확인 — JS 미러 (보조 0.5·slow축)
    _divN=(1 if _falling(ch.get("rsp_spy")) else 0)+(1 if _falling(ch.get("xlf_spy")) else 0)+(1 if _rising(ch.get("hyoas")) else 0)
    if (not regime_active) and _divN>=2: add("다이버전스(지수↑·내부↓)",0.5,"slow")
    score += min(lead_score,1.0)   # P1: 선행약세 합산 최대 1.0 (상관된 추세 신호 과대계상 방지, JS와 동일)
    axisCount=(1 if fast else 0)+(1 if slow else 0)+(1 if price_ax else 0)
    guard_warn=(score>=2.5 and axisCount>=2 and strong>=1) or strong>=2  # E1(절충 v2.23)
    risk = axisCount>=3 and score>=3 and strong>=3        # E2(v2.23): 위험 = 폭+깊이+질(강한3)
    if risk: st="r"
    elif guard_warn: st="r"                              # 경계
    elif score>=1: st="a"
    elif len(early_hits)>=2: st="a"
    else: st="g"
    if regime_active and st=="g": st="a"   # 레짐 지속 floor: 조정 진행 중이면 최소 주의 (JS와 동일)
    return {"score":round(score,1),"axisCount":axisCount,"st":st,"hits":hits,"early":early_hits,"strong":strong,"risk":risk}

def update_history(prev, ew):
    """30일 일별 조기경보 이력 누적 (하루 1개, 최신값으로 갱신)"""
    hist = (prev or {}).get("history",[]) if prev else []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry={"d":today,"score":ew["score"],"st":ew["st"],"axis":ew["axisCount"],"risk":bool(ew.get("risk")),"hits":ew["hits"]}
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
        lines.append("\n<a href='https://jisk.net/'>대시보드 열기</a>")
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

def update_klr(prev, ew, prices, fred):
    """KLR 원장(사전등록 자동 집행) — 사람 손 개입 없이:
    ① '경계'+ 최초 진입일의 SPY 종가·HY OAS를 박제 ② 매 사이클 60거래일(민감도 40/90) 창에서
    SPX −10% 또는 HY +150bp 적중을 종가 기준 자동 채점 ③ 재점등 30거래일 쿨다운 관리.
    전향(prospective) 전용 — 과거 소급 없음. 시장데이터 결측 시 원장 동결(전진·진입 모두 정지)."""
    k=(prev or {}).get("klr") or {"entries":[],"since_last_td":None,"last_mkt":None,
       "proto":"경계+ 진입 → 60거래일 · SPX −10% ∨ HY +150bp = 적중 · 쿨다운 30거래일 · 민감도 40/90 병기 · n<5 유보"}
    spy=(prices or {}).get("SPY") or {}
    px=spy.get("regClose") or spy.get("price"); mt=spy.get("mt")
    hyo=((fred or {}).get("hyoas") or {}); hy=hyo.get("value")
    if not (spy.get("ok") and px and mt):
        return k                                   # 원장 동결
    mkt_date=datetime.fromtimestamp(mt,tz=timezone.utc).strftime("%Y-%m-%d")
    new_day=(k.get("last_mkt")!=mkt_date)
    if new_day:
        k["last_mkt"]=mkt_date
        if k.get("since_last_td") is not None: k["since_last_td"]+=1
    for e in k["entries"]:
        if e.get("status") in ("추적중","60미적중·90추적"):
            if new_day and mkt_date>e["entry_date"]: e["td"]=e.get("td",0)+1
            pct=round((px/e["spx0"]-1)*100,2); e["spx_now_pct"]=pct
            e["min_spx_pct"]=min(e.get("min_spx_pct",0.0),pct)
            if hy is not None and e.get("hy0") is not None:
                bp=round((hy-e["hy0"])*100,1); e["hy_now_bp"]=bp
                e["max_hy_bp"]=max(e.get("max_hy_bp",0.0),bp)
            hit_spx=e["min_spx_pct"]<=-10; hit_hy=e.get("max_hy_bp",0.0)>=150
            if (hit_spx or hit_hy) and not e.get("hit_date"):
                e["hit_date"]=mkt_date; e["hit_td"]=e["td"]
                e["hit_reason"]="SPX -10%" if hit_spx else "HY +150bp"
                e["hit40"]=e["td"]<=40; e["hit60"]=e["td"]<=60
                e["status"]="적중" if e["td"]<=60 else "90일내 적중(60기준 미적중)"
            elif e["td"]>=90:
                e["status"]="미적중(종결)"
            elif e["td"]>60 and e["status"]=="추적중":
                e["status"]="60미적중·90추적"
    tier=3 if ew.get("risk") else (2 if ew.get("st")=="r" else (1 if ew.get("st")=="a" else 0))
    open_exists=any(e.get("status") in ("추적중","60미적중·90추적") for e in k["entries"])
    cd_ok=(k.get("since_last_td") is None) or (k["since_last_td"]>=30)
    dup=any(e.get("entry_date")==mkt_date for e in k["entries"])
    if tier>=2 and not open_exists and cd_ok and not dup:
        k["entries"].append({"entry_date":mkt_date,"tier":("위험" if tier==3 else "경계"),
            "spx0":px,"inst":"SPY종가","hy0":hy,"hy0_date":hyo.get("date"),
            "td":0,"min_spx_pct":0.0,"max_hy_bp":0.0,"status":"추적중"})
        k["since_last_td"]=0
    adj=[e for e in k["entries"] if e.get("hit60") is True or e.get("td",0)>60 and not e.get("hit60")]
    hit60=sum(1 for e in adj if e.get("hit60"))
    k["stats"]={"n":len(adj),"hit60":hit60,"miss60":len(adj)-hit60,
                "open":sum(1 for e in k["entries"] if e.get("status")=="추적중"),
                "hold":("n<5 결론 유보" if len(adj)<5 else "")}
    return k

def main():
    # 이전 data.json 로드 (요약 캐싱 + 이력 누적용)
    prev=None
    prev_exists=os.path.exists("data.json")
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
    fragility=build_fragility(prev)
    ebp=fetch_ebp(prev)
    summary=summarize_news(news,prices,fred,prev)
    ew=calc_early(prices,fred,charts)
    ew["inputs"]=ew_inputs(prices,fred,charts)   # R3 출석부
    history=update_history(prev,ew)
    klr=update_klr(prev,ew,prices,fred)
    # KLR 원장 이벤트 알림 — 사전등록 집행 통지 (드문 이벤트만)
    try:
        _pk=((prev or {}).get("klr") or {}).get("entries",[]); _ck=klr.get("entries",[])
        if len(_ck)>len(_pk):
            _e=_ck[-1]; tg_send(f"📋 <b>KLR 원장 진입</b>\n{_e['entry_date']} {_e['tier']} · 기준 SPY {_e['spx0']} · HY {_e['hy0']}%\n60거래일 자동 채점 시작 (SPX −10% ∨ HY +150bp)")
        _ps={e.get("entry_date"):e.get("status") for e in _pk}
        for _e in _ck:
            if "적중"==(_e.get("status") or "")[:2] and (_ps.get(_e.get("entry_date")) or "")[:2]!="적중":
                tg_send(f"🎯 <b>KLR 적중</b> {_e['entry_date']} 진입건 — {_e.get('hit_reason')} ({_e.get('hit_td')}거래일차)")
    except Exception: pass
    gpu=fetch_gpu_price(prev)
    visitors, visit_hours = fetch_visit_stats(prev)
    data={"updated":datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "prices":prices,"news":news,"edgar":fetch_edgar(prev),
          "fred":fred,"charts":charts,"summary":summary,
          "early":ew,"history":history,"events":upcoming_events(),"gpu":gpu,"visitors":visitors,"visit_hours":visit_hours,"feed":feed,"breadth":dict(zip(("pct50","n"),_breadth(prices))),"ipo_watch":ipo,"fragility":fragility,"ebp":ebp,"klr":klr}
    _gate=schema_gate(prev_exists,prev,ew,history,klr)   # R3 현관 검문
    if _gate:
        print("⛔ 스키마 게이트 차단:",_gate)
        try: tg_send("⛔ <b>스키마 게이트</b>\n"+" · ".join(_gate)+"\ndata.json 미갱신 — 이전 상태 보존, Actions 로그 확인 필요")
        except Exception: pass
        return
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=1)
    # 월간 아카이브: 이달 스냅샷 없으면 1회 저장 — FRED 3년창 제한 우회 + 자체 히트율/거짓경보율 데이터 축적 (워크플로가 archive/ 커밋)
    try:
        os.makedirs("archive",exist_ok=True)
        _arc=os.path.join("archive",datetime.now(timezone.utc).strftime("%Y-%m")+".json")
        if not os.path.exists(_arc):
            import shutil; shutil.copyfile("data.json",_arc)
            print("[archive] 월간 스냅샷 저장:",_arc)
    except Exception as e:
        print("[archive] 실패:",str(e)[:80])
    # 위험 단계 상향 시 텔레그램 알림 (prev와 비교 — 저장 전 prev 사용)
    try: maybe_alert(prev, ew, summary, prices, fred)
    except Exception as e: print("알림 처리 오류:",str(e)[:100])
    cached=" (캐시재사용)" if summary.get("_cached") else ""
    print("data.json 저장:",data["updated"],"| summary:",data["summary"]["by"]+cached,"| 조기경보:",ew["st"],ew["score"],"| 이력:",len(history),"일")

if __name__=="__main__":
    main()
