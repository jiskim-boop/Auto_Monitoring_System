# 시장 사이클 모니터 (v5)

GitHub Pages + Actions 자동 대시보드. 매시간 갱신. 3구역 반응형.
- 거시·시스템: VIX·신용스프레드·달러·금리차·광범위시장
- AI 밸류체인(자본흐름): 반도체·메모리·네트워킹·전력·원자재·하이퍼스케일러 (총 38종목 트래킹)
- 신용(최우선)·자금흐름·기타: 사모대출 패널·EDGAR·신용뉴스·금리·금·BTC·capex뉴스·규제·추세·IPO

## 위험 규칙
- 종목: 200일선 아래 OR 당일 −5%↓ = 위험
- 바스켓: 구성종목 과반이 위험이면 ON
- 신용 트리거 뉴스/신규 공시 → 상단 빨간 배너 + AI 판정 격상
- 판정 분리: 시장 시스템 위험 vs AI 사이클 위험

## 갱신: 매시간 (:17분)
## AI 요약: Settings→Secrets→Actions, Name `ANTHROPIC_API_KEY`
## 종목 수정: fetch_data.py의 PRICE_SYMBOLS, index.html의 basket() 호출
