# 연수 방명록 서명 앱

Streamlit + streamlit-drawable-canvas + Google Sheets/Drive 기반.

## 왜 빠른가
- 캔버스 렌더링은 **클라이언트 사이드** (브라우저 Canvas API)
- `update_streamlit=False`로 그리는 동안 서버 rerun 안 일어남
- 서버 호출은 **제출 버튼 1번**만 (Drive 업로드 + Sheets append)
- GAS는 매 스트로크마다 왕복 → 느림. 이 앱은 구조적으로 다름.

## 시트 구조

### 연수목록 (시트명)
| 연수명 | 일시 | 활성화 |
|---|---|---|
| 2026 1학기 1차 교직원 연수 | 2026-04-20 15:00 | Y |
| 수업나눔 협의회 | 2026-04-22 14:00 | Y |
| 지난 연수 | 2026-03-10 | N |

- 활성화 컬럼: `Y` / `N` (N이면 목록에 안 보임)

### 서명기록 (시트명) — 헤더만 한 번 만들어두면 자동으로 append됨
| 타임스탬프 | 연수명 | 이름 | 소속 | 서명URL |
|---|---|---|---|---|

## 초기 설정

### 1. Google Cloud 설정
이미 `flash-proxy-490806-v9` 서비스 계정을 쓰고 계시니 그대로 활용.

- **Drive API 활성화**: GCP 콘솔 → API 및 서비스 → Drive API 사용 설정
- **Sheets API**는 이미 켜져있을 거예요

### 2. 서명 이미지 저장용 Drive 폴더 생성
1. Google Drive에서 폴더 만들기 (예: "연수방명록_서명이미지")
2. 해당 폴더를 서비스 계정 이메일(`...@flash-proxy-490806-v9.iam.gserviceaccount.com`)에 **편집자 권한**으로 공유
3. 폴더 URL에서 ID 복사: `https://drive.google.com/drive/folders/{이_부분이_ID}`

### 3. Google Sheets 생성 & 공유
1. 새 스프레드시트 만들고 "연수목록", "서명기록" 시트 생성 (헤더 입력)
2. 서비스 계정 이메일에 **편집자 권한**으로 공유
3. 스프레드시트 URL에서 ID 복사

### 4. secrets.toml 작성
`secrets.toml.example`을 `.streamlit/secrets.toml`로 복사 후 값 채우기.

### 5. 설치 & 실행
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud 배포
1. GitHub에 푸시 (secrets.toml은 제외 — `.gitignore`)
2. share.streamlit.io에서 앱 생성
3. Settings → Secrets에 `secrets.toml` 내용 붙여넣기
4. 배포된 URL을 연수장 QR로 만들어서 안내

## 현장 운영 팁
- 서명 후 2초 뒤 자동 초기화되어 다음 사람이 바로 서명 가능
- 태블릿 1~2대 비치 + 본인 휴대폰 둘 다 가능 (모바일 브라우저 지원)
- 감사 대비: 서명기록 시트에서 URL 클릭 → Drive에서 원본 PNG 확인
- 결재용 PDF 필요시, 시트 필터링 후 별도 스크립트로 일괄 PDF 생성 가능 (다음 단계)
