# AI 사이클 모니터 (v3)

GitHub Pages + Actions 자동 대시보드. 4열 반응형.
- 1열 EDGAR 8-K(본문 AI 요약, NEW 배지) / 2열 신용 종합패널(BIZD·ARCC·OBDC·HYG 실시간+위험도)+뉴스
- 3열 하이퍼스케일러 펀더멘털 뉴스 / 4열 금리·전력·포지셔닝 바스켓·규제·추세·IPO

## 업데이트
1. `fetch_data.py`, `index.html` 교체 후 push
2. AI 요약(EDGAR 본문 + 뉴스 한 문장): Settings → Secrets and variables → Actions →
   New repository secret → **Name 칸**에 `ANTHROPIC_API_KEY`, **Secret 칸**에 키
3. Actions → Run workflow

키 없으면: EDGAR는 Item 코드 분류로, 뉴스 요약은 기계 요약으로 자동 대체.

## 비용
Haiku 모델, 하루 2회 × (EDGAR 5건 + 뉴스 1) ≈ 12콜/일. 월 1달러 미만.

## 수정
`fetch_data.py`의 NEWS_QUERIES / EDGAR_CIKS / PRICE_SYMBOLS 편집.
