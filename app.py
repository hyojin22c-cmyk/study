"""
연수 방명록 - 손글씨 서명 수집 앱
Streamlit + streamlit-drawable-canvas + Google Sheets + Google Drive

사용 흐름:
1. 관리자가 '연수 관리' 탭에서 연수 등록 → 연수별 링크/QR 생성
2. 참석자는 링크(?training_id=xxx)로 접속 → 이름/소속 입력 → 서명 → 제출
3. 서명 이미지는 Drive에 PNG로 저장, Sheets에는 메타데이터 + Drive 링크 기록
"""

import streamlit as st
from streamlit_drawable_canvas import st_canvas
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image
import io
import datetime
import uuid
import numpy as np

# ───────────────────────────────────────────────────────
# 설정
# ───────────────────────────────────────────────────────
SPREADSHEET_ID = st.secrets["spreadsheet_id"]  # secrets.toml에 저장
DRIVE_FOLDER_ID = st.secrets["drive_folder_id"]  # 서명 이미지 저장할 Drive 폴더 ID
ADMIN_PASSWORD = st.secrets["admin_password"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ───────────────────────────────────────────────────────
# 구글 API 클라이언트 (캐싱: 매번 재인증 방지 → 속도 핵심)
# ───────────────────────────────────────────────────────
@st.cache_resource
def get_gspread_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)


@st.cache_resource
def get_drive_service():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


@st.cache_resource
def get_sheets():
    """시트 핸들 캐싱. 단계별 에러 메시지 노출."""
    try:
        gc = get_gspread_client()
    except Exception as e:
        raise RuntimeError(
            f"❌ Service Account 인증 실패\n\n"
            f"원인: `secrets.toml`의 `[gcp_service_account]` 값이 잘못되었거나 비어있습니다.\n\n"
            f"상세: {e}"
        )

    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"❌ Spreadsheet를 찾을 수 없습니다\n\n"
            f"체크 포인트:\n"
            f"1. `spreadsheet_id`가 맞는지 확인 (현재: `{SPREADSHEET_ID}`)\n"
            f"2. 해당 Spreadsheet를 Service Account 이메일에 **편집자** 권한으로 공유했는지 확인\n"
            f"   → Service Account 이메일: `{st.secrets['gcp_service_account'].get('client_email', '(알 수 없음)')}`"
        )
    except gspread.exceptions.APIError as e:
        raise RuntimeError(
            f"❌ Google API 오류\n\n"
            f"가능한 원인:\n"
            f"- Google Cloud Console에서 **Sheets API** 또는 **Drive API**가 활성화 안 됨\n"
            f"- Service Account 권한 부족\n\n"
            f"상세: {e}"
        )
    except Exception as e:
        raise RuntimeError(f"❌ Spreadsheet 접근 실패: {e}")

    # 시트가 없으면 생성
    try:
        trainings = sh.worksheet("연수목록")
    except gspread.WorksheetNotFound:
        trainings = sh.add_worksheet("연수목록", rows=100, cols=5)
        trainings.append_row(["training_id", "연수명", "일시", "장소", "생성일"])

    try:
        records = sh.worksheet("서명기록")
    except gspread.WorksheetNotFound:
        records = sh.add_worksheet("서명기록", rows=1000, cols=7)
        records.append_row(
            ["training_id", "연수명", "이름", "소속", "제출시각", "서명파일", "서명URL"]
        )

    return trainings, records


# 연수 목록은 짧은 TTL로 캐싱 (관리자가 새로 등록해도 1분 내 반영)
@st.cache_data(ttl=60)
def load_trainings():
    trainings_ws, _ = get_sheets()
    return trainings_ws.get_all_records()


def render_diagnostics():
    """설정 진단 - 문제 발생 시 원인 파악용."""
    st.subheader("🔍 설정 진단")

    # 1. secrets 체크
    st.markdown("**1. secrets.toml 값 확인**")
    checks = {
        "spreadsheet_id": SPREADSHEET_ID,
        "drive_folder_id": DRIVE_FOLDER_ID,
        "admin_password": "(설정됨)" if ADMIN_PASSWORD else "(비어있음)",
        "Service Account 이메일": st.secrets["gcp_service_account"].get(
            "client_email", "(없음)"
        ),
    }
    for k, v in checks.items():
        st.text(f"  {k}: {v}")

    # 2. Sheets 접근 테스트
    st.markdown("**2. Spreadsheet 접근 테스트**")
    try:
        get_sheets.clear()  # 캐시 무효화
        trainings_ws, records_ws = get_sheets()
        st.success(f"✅ Spreadsheet 접근 성공: `{trainings_ws.spreadsheet.title}`")
        st.text(f"  - 연수목록 시트: {trainings_ws.row_count}행 x {trainings_ws.col_count}열")
        st.text(f"  - 서명기록 시트: {records_ws.row_count}행 x {records_ws.col_count}열")
    except Exception as e:
        st.error(str(e))
        return

    # 3. 연수 목록 조회 테스트
    st.markdown("**3. 연수 목록 조회 테스트**")
    try:
        load_trainings.clear()
        trainings = load_trainings()
        st.success(f"✅ 조회 성공: {len(trainings)}개 연수")
        if trainings:
            st.json(trainings[:3])
    except Exception as e:
        st.error(f"❌ 조회 실패: {e}")

    # 4. Drive 접근 테스트
    st.markdown("**4. Drive 폴더 접근 테스트**")
    try:
        service = get_drive_service()
        folder = service.files().get(fileId=DRIVE_FOLDER_ID, fields="id,name").execute()
        st.success(f"✅ Drive 폴더 접근 성공: `{folder['name']}`")
    except Exception as e:
        st.error(
            f"❌ Drive 폴더 접근 실패: {e}\n\n"
            f"체크: 해당 폴더를 Service Account 이메일에 편집자로 공유했나요?"
        )


# ───────────────────────────────────────────────────────
# 업로드 로직
# ───────────────────────────────────────────────────────
def upload_signature_to_drive(png_bytes: bytes, filename: str) -> tuple[str, str]:
    """서명 PNG를 Drive에 업로드. (파일ID, 보기URL) 반환."""
    service = get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png")
    file = service.files().create(
        body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return file["id"], file["webViewLink"]


def save_signature(training_id, training_name, name, affiliation, png_bytes):
    """서명 저장: Drive 업로드 → Sheets 기록. 총 API 호출 2번."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = name.replace("/", "_").replace(" ", "_")
    filename = f"{training_id}_{safe_name}_{uuid.uuid4().hex[:6]}.png"

    # 1. Drive 업로드
    file_id, view_url = upload_signature_to_drive(png_bytes, filename)

    # 2. Sheets 기록 (append_row: 전체 시트 로드 안 함 → 빠름)
    _, records_ws = get_sheets()
    records_ws.append_row(
        [training_id, training_name, name, affiliation, timestamp, filename, view_url],
        value_input_option="USER_ENTERED",
    )
    return view_url


def canvas_to_png_bytes(image_data: np.ndarray) -> bytes:
    """캔버스 RGBA 배열 → 흰 배경의 PNG bytes."""
    img = Image.fromarray(image_data.astype("uint8"), mode="RGBA")
    # 흰 배경 합성 (투명 → 흰색)
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    bg.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def is_canvas_empty(image_data: np.ndarray) -> bool:
    """캔버스에 아무것도 안 그렸는지 체크 (알파 채널 기준)."""
    if image_data is None:
        return True
    alpha = image_data[:, :, 3]
    return alpha.sum() == 0


# ───────────────────────────────────────────────────────
# UI: 참석자 서명 페이지
# ───────────────────────────────────────────────────────
def render_signing_page(training_id: str):
    try:
        trainings = load_trainings()
    except Exception as e:
        st.error("❌ 시스템 오류로 방명록을 불러올 수 없습니다.")
        st.caption("담당자에게 문의해주세요.")
        with st.expander("기술 정보 (담당자용)"):
            st.code(str(e))
        return

    training = next((t for t in trainings if str(t["training_id"]) == training_id), None)

    if not training:
        st.error("유효하지 않은 연수 링크입니다. 담당자에게 문의해주세요.")
        return

    st.title("📝 연수 방명록")
    st.markdown(
        f"### {training['연수명']}\n"
        f"**일시**: {training['일시']} | **장소**: {training['장소']}"
    )
    st.divider()

    # 입력 필드
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("이름 *", max_chars=20)
    with col2:
        affiliation = st.text_input("소속 (학과/담당) *", max_chars=30)

    st.markdown("**서명 *** (아래 영역에 손가락 또는 마우스로 서명해주세요)")

    # 캔버스 (클라이언트 사이드 렌더링 → 그리는 동안 서버 왕복 없음)
    canvas_result = st_canvas(
        fill_color="rgba(255, 255, 255, 0)",
        stroke_width=3,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=200,
        width=600,
        drawing_mode="freedraw",
        key="signature_canvas",
        display_toolbar=True,  # 지우개/되돌리기 기본 제공
    )

    st.caption("💡 팁: 좌측 상단 🗑 아이콘으로 전체 지우기, ↶ 아이콘으로 되돌리기 가능")

    # 제출 버튼
    submitted = st.button("✅ 서명 제출", type="primary", use_container_width=True)

    if submitted:
        # 검증
        if not name.strip():
            st.error("이름을 입력해주세요.")
            return
        if not affiliation.strip():
            st.error("소속을 입력해주세요.")
            return
        if canvas_result.image_data is None or is_canvas_empty(canvas_result.image_data):
            st.error("서명을 해주세요.")
            return

        # 저장 (API 호출은 여기서 딱 2번)
        with st.spinner("저장 중..."):
            try:
                png_bytes = canvas_to_png_bytes(canvas_result.image_data)
                save_signature(
                    training_id,
                    training["연수명"],
                    name.strip(),
                    affiliation.strip(),
                    png_bytes,
                )
                st.success("✅ 제출 완료! 참석해주셔서 감사합니다.")
                st.balloons()
                # 제출 후 캔버스 리셋을 위해 세션 플래그
                st.session_state["submitted"] = True
            except Exception as e:
                st.error(f"저장 중 오류가 발생했습니다: {e}")


# ───────────────────────────────────────────────────────
# UI: 관리자 페이지
# ───────────────────────────────────────────────────────
def render_admin_page():
    st.title("🔧 연수 방명록 관리")

    # 로그인
    if not st.session_state.get("admin_authed"):
        pw = st.text_input("관리자 비밀번호", type="password")
        if st.button("로그인"):
            if pw == ADMIN_PASSWORD:
                st.session_state["admin_authed"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        return

    tab1, tab2, tab3, tab4 = st.tabs(
        ["연수 등록", "연수 목록 & 링크", "서명 기록 조회", "🔍 진단"]
    )

    # 탭 1: 연수 등록
    with tab1:
        st.subheader("새 연수 등록")
        with st.form("new_training"):
            new_name = st.text_input("연수명")
            new_date = st.text_input("일시 (예: 2026-04-20 14:00)")
            new_place = st.text_input("장소")
            if st.form_submit_button("등록"):
                if new_name and new_date:
                    try:
                        tid = uuid.uuid4().hex[:8]
                        trainings_ws, _ = get_sheets()
                        trainings_ws.append_row(
                            [
                                tid,
                                new_name,
                                new_date,
                                new_place,
                                datetime.datetime.now().strftime("%Y-%m-%d"),
                            ]
                        )
                        st.cache_data.clear()
                        st.success(f"등록 완료! training_id: `{tid}`")
                    except Exception as e:
                        st.error(f"등록 실패: {e}")
                        st.info("'진단' 탭에서 설정을 확인해주세요.")
                else:
                    st.error("연수명과 일시는 필수입니다.")

    # 탭 2: 연수 목록 & 공유 링크
    with tab2:
        st.subheader("연수별 참석 링크")
        try:
            trainings = load_trainings()
        except Exception as e:
            st.error(str(e))
            st.info("👉 '진단' 탭에서 설정을 확인하세요.")
            trainings = []

        if not trainings:
            st.info("등록된 연수가 없습니다.")
        else:
            base_url = st.text_input(
                "배포된 앱 URL (끝에 / 없이)",
                value=st.session_state.get("base_url", "https://your-app.streamlit.app"),
                help="이 URL 뒤에 ?training_id=... 가 붙어서 참석자에게 공유됩니다.",
            )
            st.session_state["base_url"] = base_url

            for t in reversed(trainings):  # 최신순
                with st.expander(f"📌 {t['연수명']} ({t['일시']})"):
                    link = f"{base_url}/?training_id={t['training_id']}"
                    st.code(link, language=None)
                    st.caption(f"ID: `{t['training_id']}` | 장소: {t['장소']}")
                    # QR 코드 생성 (선택)
                    try:
                        import qrcode
                        img = qrcode.make(link)
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        st.image(buf.getvalue(), width=200, caption="QR 코드")
                    except ImportError:
                        st.caption("(qrcode 패키지 설치 시 QR 코드 자동 생성)")

    # 탭 3: 서명 기록 조회
    with tab3:
        st.subheader("서명 기록")
        try:
            _, records_ws = get_sheets()
            records = records_ws.get_all_records()
        except Exception as e:
            st.error(str(e))
            records = []

        if not records:
            st.info("아직 서명 기록이 없습니다.")
        else:
            try:
                trainings = load_trainings()
                training_names = ["전체"] + [t["연수명"] for t in trainings]
            except Exception:
                training_names = ["전체"]

            selected = st.selectbox("연수 선택", training_names)
            filtered = (
                records if selected == "전체"
                else [r for r in records if r["연수명"] == selected]
            )
            st.caption(f"총 {len(filtered)}건")
            st.dataframe(filtered, use_container_width=True)

    # 탭 4: 진단
    with tab4:
        render_diagnostics()
        if st.button("🔄 캐시 초기화 (문제 해결 후 클릭)"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.success("캐시 초기화 완료. 새로고침 해주세요.")


# ───────────────────────────────────────────────────────
# 라우팅
# ───────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="연수 방명록", page_icon="📝", layout="centered")

    params = st.query_params
    training_id = params.get("training_id")
    is_admin = params.get("admin") == "1"

    if is_admin:
        render_admin_page()
    elif training_id:
        render_signing_page(training_id)
    else:
        st.title("📝 연수 방명록")
        st.info(
            "접속 링크가 올바르지 않습니다.\n\n"
            "연수 담당자로부터 받은 링크(또는 QR 코드)로 접속해주세요."
        )
        st.caption("관리자는 URL 끝에 `?admin=1`을 붙여 접속하세요.")


if __name__ == "__main__":
    main()
