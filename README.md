# AI 사이클 모니터

GitHub Pages + Actions 자동 대시보드. 시세 자동 색칠 + 신용/펀더멘털 뉴스 + EDGAR 8-K + 한 문장 요약.

## 업데이트(이미 배포한 경우)
1. `fetch_data.py`, `index.html`, `.github/workflows/update.yml` 교체 후 push
2. (선택) AI 한 문장 요약: 저장소 Settings → Secrets and variables → Actions →
   New repository secret → 이름 `ANTHROPIC_API_KEY`, 값 = console.anthropic.com 발급 키
3. Actions → Run workflow 재실행

## 자동/수동
- 자동(하루 2회): 시세 색칠, 신용·펀더멘털 뉴스, EDGAR 8-K, (키 있으면)한 문장 요약
- 수동(요청 시): 위험선 이동평균값, 정성 축(규제·추세지속)

## EDGAR User-Agent
SEC는 연락처 없는 요청을 차단합니다. `fetch_data.py`의 `SEC_UA` 이메일은
형식만 맞으면 되며, 본인 이메일로 바꿔도 됩니다.

## 키워드 수정
`fetch_data.py`의 `NEWS_QUERIES` / `EDGAR_CIKS` 편집.
