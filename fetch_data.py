#!/usr/bin/env python3
"""
AI 사이클 모니터 — 데이터 수집기 (v2)
시세(야후) + 뉴스(Google News RSS) + EDGAR 8-K + 한 문장 요약 -> data.json
의존성 없음(표준 라이브러리). Python 3.9+
- EDGAR: SEC 규정 User-Agent(이메일 포함) + browse-edgar Atom 폴백
- 요약: ANTHROPIC_API_KEY 있으면 Claude로 한 문장 요약, 없으면 기계 요약
"""
import json, os, urllib.request, urllib.parse, re, time, html, gzip
from datetime import datetime, timezone

UA = {"User-Agent": "ai-cycle-monitor/1.0 (personal research)"}
# SEC 전용: 반드시 연락처(이메일 형식) 포함. 본인 이메일로 바꿔도 됨.
SEC_UA = {"User-Agent": "ai-cycle-monitor jiskim.boop@gmail.com",
          "Accept-Encoding": "gzip, deflate"}

def get(url, headers=UA, timeout=25):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

# ---- 시세
PRICE_SYMBOLS = ["CRWV","VST","CEG","NRG","TLN","NVDA","AVGO","SMH","%5ETNX"]
def fetch_quote(sym):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
    try:
        j=json.loads(get(url)); res=j["chart"]["result"][0]
        price=res["meta"]["regularMarketPrice"]
        closes=[c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        sma=lambda n: round(sum(closes[-n:])/min(n,len(closes)),2) if closes else None
        return {"price":round(price,2),"sma50":sma(50),"sma200":sma(200),
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
}
TRIGGER=re.compile(r"\b(gate|redemption|default|downgrade|markdown|non-accrual|cut|miss|slowdown|write-?down|distress|halt)\b",re.I)
def clean(t): return html.unescape(re.sub("<[^>]+>","",t)).strip()
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
        out[cat]=items[:12]
    return out

# ---- EDGAR 8-K
EDGAR_CIKS={"OBDC (Blue Owl Capital)":"0001655888","ARCC (Ares Capital)":"0001287750",
 "BXSL (Blackstone Secured Lending)":"0001736035","FSK (FS KKR)":"0001422183",
 "GBDC (Golub Capital)":"0001476765"}
def edgar_from_submissions(cik):
    j=json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json",headers=SEC_UA))
    r=j["filings"]["recent"]
    for i,form in enumerate(r["form"]):
        if form.startswith("8-K"):
            accn=r["accessionNumber"][i].replace("-","")
            link=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{r['primaryDocument'][i]}"
            return {"form":form,"date":r["filingDate"][i],"link":link}
    return None
def edgar_from_atom(cik):
    url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K&dateb=&owner=include&count=1&output=atom"
    xml=get(url,headers=SEC_UA)
    m=re.search(r"<entry>(.*?)</entry>",xml,re.S)
    if not m: return None
    e=m.group(1)
    date=clean((re.search(r"<filing-date>(.*?)</filing-date>",e,re.S) or re.search(r"<updated>(.*?)</updated>",e,re.S) or [None,""])[1])[:10]
    href=(re.search(r'<filing-href>(.*?)</filing-href>',e,re.S) or re.search(r'<link[^>]*href="(.*?)"',e,re.S))
    return {"form":"8-K","date":date,"link":clean(href.group(1)) if href else ""}
def fetch_edgar():
    out=[]
    for name,cik in EDGAR_CIKS.items():
        rec=None
        try: rec=edgar_from_submissions(cik)
        except Exception:
            try: rec=edgar_from_atom(cik)
            except Exception as e:
                out.append({"name":name,"form":"ERR","date":str(e)[:60],"link":""}); time.sleep(0.5); continue
        if rec: out.append({"name":name,**rec})
        time.sleep(0.5)
    out.sort(key=lambda x:x.get("date",""),reverse=True)
    return out

# ---- 한 문장 요약
def summarize(news):
    heads_c=[it["title"] for it in news.get("credit",[]) if not it["title"].startswith("[")][:10]
    heads_f=[it["title"] for it in news.get("fundamental",[]) if not it["title"].startswith("[")][:6]
    key=os.environ.get("ANTHROPIC_API_KEY","").strip()
    if key and (heads_c or heads_f):
        try:
            prompt=("다음은 AI 인프라 신용·사모대출 환매(CREDIT)와 하이퍼스케일러 capex(FUNDAMENTAL) "
                "관련 최신 영문 뉴스 헤드라인이다. 현재 상황을 한국어 한 문장(80자 내외)으로, 과장 없이 "
                "사실 위주로 요약하라. 신용 스트레스 정도와 capex 흐름을 함께 담아라.\n\n[CREDIT]\n"
                +"\n".join("- "+h for h in heads_c)+"\n\n[FUNDAMENTAL]\n"+"\n".join("- "+h for h in heads_f))
            body=json.dumps({"model":"claude-haiku-4-5-20251001","max_tokens":200,
                "messages":[{"role":"user","content":prompt}]}).encode()
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=body,
                headers={"x-api-key":key,"anthropic-version":"2023-06-01","content-type":"application/json"})
            with urllib.request.urlopen(req,timeout=40) as r:
                j=json.loads(r.read().decode())
            txt="".join(b.get("text","") for b in j.get("content",[])).strip()
            if txt: return {"text":txt,"by":"claude"}
        except Exception:
            pass
    trig_c=sum(1 for it in news.get("credit",[]) if it.get("trig"))
    n_c=len([it for it in news.get("credit",[]) if not it["title"].startswith("[")])
    cred=("신용 경보 신호 다수" if trig_c>=3 else "신용 경계 신호 일부" if trig_c>=1 else "신용 특이신호 적음")
    return {"text":f"최근 신용 뉴스 {n_c}건 중 트리거 {trig_c}건 — {cred}. (자동 한 문장 요약은 API 키 설정 시)","by":"heuristic"}

def main():
    news=fetch_news()
    data={"updated":datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "prices":fetch_prices(),"news":news,"edgar":fetch_edgar(),"summary":summarize(news)}
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=1)
    print("data.json 저장:",data["updated"],"| summary by",data["summary"]["by"])

if __name__=="__main__":
    main()
