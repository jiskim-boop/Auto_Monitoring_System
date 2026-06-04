# AI 사이클 모니터

GitHub Pages + Actions로 도는 자동 대시보드.

## 설치 (5단계)
1. 이 폴더의 파일들을 기존 GitHub 저장소(또는 새 저장소)에 업로드
   - `index.html`, `fetch_data.py`, `data.json`, `.github/workflows/update.yml`
2. 저장소 → **Settings → Pages** → Source를 `main` 브랜치 / `/ (root)`로 설정 → 저장
3. 저장소 → **Settings → Actions → General** → "Workflow permissions"를 **Read and write**로 설정 (data.json 커밋 권한)
4. 저장소 → **Actions** 탭 → "update-dashboard-data" → **Run workflow** 수동 1회 실행 → data.json 생성 확인
5. `https://<아이디>.github.io/<저장소명>/` 접속 → 폰 홈 화면에 추가

이후 평일 하루 2회(장 시작·마감 무렵) 자동 갱신됩니다.

## 갱신
- 시세·뉴스·EDGAR: 자동
- 위험선 가격(이동평균)·수동 축(규제·추세지속): "갱신해줘" 요청 시 또는 분기 실적 때

## 키워드 수정
`fetch_data.py`의 `NEWS_QUERIES` / `EDGAR_CIKS` 편집.
