"""
연수 방명록 - 손글씨 서명 수집 앱 (v2)
Streamlit + streamlit-drawable-canvas + Google Sheets + Google Drive

참석자 흐름:
  사이트 접속 → 연수 선택 → 부서 선택 → 이름 선택 → 서명 → 제출

관리자 흐름:
  ?admin=1 → 연수 등록/관리, 교직원 명부 관리, 서명 기록 조회
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
SPREADSHEET_ID = st.secrets["spreadsheet_id"]
DRIVE_FOLDER_ID = st.secrets["drive_folder_id"]
ADMIN_PASSWORD = st.secrets["admin_password"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ───────────────────────────────────────────────────────
# 구글 API 클라이언트 (캐싱)
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
    """시트 핸들 캐싱. 필요한 탭이 없으면 자동 생성."""
    try:
        gc = get_gspread_client()
    except Exception as e:
        raise RuntimeError(f"❌ Service Account 인증 실패: {e}")

    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"❌ Spreadsheet를 찾을 수 없습니다 (ID: `{SPREADSHEET_ID}`)\n"
            f"Service Account에 편집자 공유 필요: "
            f"`{st.secrets['gcp_service_account'].get('client_email', '(알 수 없음)')}`"
        )
    except Exception as e:
        raise RuntimeError(f"❌ Spreadsheet 접근 실패: {e}")

    # 연수목록
    try:
        trainings = sh.worksheet("연수목록")
    except gspread.WorksheetNotFound:
        trainings = sh.add_worksheet("연수목록", rows=100, cols=6)
        trainings.append_row(
            ["training_id", "연수명", "일시", "장소", "상태", "생성일"]
        )

    # 서명기록
    try:
        records = sh.worksheet("서명기록")
    except gspread.WorksheetNotFound:
        records = sh.add_worksheet("서명기록", rows=1000, cols=7)
        records.append_row(
            ["training_id", "연수명", "부서", "이름", "제출시각", "서명파일", "서명URL"]
        )

    # 교직원명부 (신규)
    try:
        teachers = sh.worksheet("교직원명부")
    except gspread.WorksheetNotFound:
        teachers = sh.add_worksheet("교직원명부", rows=200, cols=2)
        teachers.append_row(["부서", "이름"])
        teachers.append_row(["교무부", "(예시) 김교무"])
        teachers.append_row(["연구부", "(예시) 이연구"])

    return trainings, records, teachers


# TTL 캐시
@st.cache_data(ttl=60)
def load_trainings():
    trainings_ws, _, _ = get_sheets()
    return trainings_ws.get_all_records()


@st.cache_data(ttl=60)
def load_teachers():
    """교직원 명부를 {부서: [이름들]} dict로 반환."""
    _, _, teachers_ws = get_sheets()
    rows = teachers_ws.get_all_records()
    by_dept: dict[str, list[str]] = {}
    for r in rows:
        dept = str(r.get("부서", "")).strip()
        name = str(r.get("이름", "")).strip()
        if dept and name:
            by_dept.setdefault(dept, []).append(name)
    return by_dept


@st.cache_data(ttl=30)
def load_signed_names_for_training(training_id: str) -> set:
    """해당 연수에 이미 서명한 (부서, 이름) 집합."""
    _, records_ws, _ = get_sheets()
    all_records = records_ws.get_all_records()
    return {
        (str(r["부서"]), str(r["이름"]))
        for r in all_records
        if str(r["training_id"]) == str(training_id)
    }


# ───────────────────────────────────────────────────────
# 업로드 로직
# ───────────────────────────────────────────────────────
def upload_signature_to_drive(png_bytes: bytes, filename: str) -> tuple[str, str]:
    service = get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png")
    file = service.files().create(
        body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return file["id"], file["webViewLink"]


def save_signature(training_id, training_name, dept, name, png_bytes):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = name.replace("/", "_").replace(" ", "_")
    filename = f"{training_id}_{dept}_{safe_name}_{uuid.uuid4().hex[:6]}.png"

    file_id, view_url = upload_signature_to_drive(png_bytes, filename)

    _, records_ws, _ = get_sheets()
    records_ws.append_row(
        [training_id, training_name, dept, name, timestamp, filename, view_url],
        value_input_option="USER_ENTERED",
    )
    return view_url


def canvas_to_png_bytes(image_data: np.ndarray) -> bytes:
    img = Image.fromarray(image_data.astype("uint8"), mode="RGBA")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    bg.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def is_canvas_empty(image_data: np.ndarray) -> bool:
    if image_data is None:
        return True
    return image_data[:, :, 3].sum() == 0


# ───────────────────────────────────────────────────────
# 참석자 페이지
# ───────────────────────────────────────────────────────
def render_signing_flow():
    st.title("📝 연수 방명록")

    try:
        trainings = load_trainings()
    except Exception as e:
        st.error(f"시스템 오류: {e}")
        return

    # 진행중인 연수만 노출
    active = [t for t in trainings if str(t.get("상태", "")).strip() != "종료"]
    if not active:
        st.info("현재 진행 중인 연수가 없습니다.")
        return

    # 1단계: 연수 선택
    training_labels = [f"{t['연수명']}  ({t['일시']})" for t in active]
    idx = st.selectbox(
        "**1️⃣ 참석한 연수를 선택하세요**",
        options=range(len(active)),
        format_func=lambda i: training_labels[i],
        index=None,
        placeholder="연수를 선택하세요...",
    )

    if idx is None:
        st.caption("👆 위에서 연수를 선택하면 다음 단계가 나타납니다.")
        return

    training = active[idx]
    training_id = str(training["training_id"])

    st.success(
        f"**선택된 연수**: {training['연수명']}  \n"
        f"📅 {training['일시']} · 📍 {training['장소']}"
    )

    # 2단계: 부서
    try:
        teachers_by_dept = load_teachers()
    except Exception as e:
        st.error(f"교직원 명부 로드 실패: {e}")
        return

    if not teachers_by_dept:
        st.warning("교직원 명부가 비어있습니다. 관리자에게 문의해주세요.")
        return

    departments = sorted(teachers_by_dept.keys())
    dept = st.selectbox(
        "**2️⃣ 소속 부서**",
        options=departments,
        index=None,
        placeholder="부서를 선택하세요...",
    )

    if not dept:
        return

    # 3단계: 이름 (이미 서명한 사람 제외)
    signed = load_signed_names_for_training(training_id)
    all_names = sorted(teachers_by_dept[dept])
    available_names = [n for n in all_names if (dept, n) not in signed]
    already_signed = [n for n in all_names if (dept, n) in signed]

    if not available_names:
        st.info(f"✅ {dept} 소속은 모두 서명을 완료하셨습니다.")
        if already_signed:
            with st.expander("서명 완료 명단"):
                st.write(", ".join(already_signed))
        return

    name = st.selectbox(
        "**3️⃣ 이름**",
        options=available_names,
        index=None,
        placeholder="이름을 선택하세요...",
    )

    if already_signed:
        st.caption(f"✓ 이미 서명 완료: {', '.join(already_signed)}")

    if not name:
        return

    # 4단계: 서명
    st.markdown("**4️⃣ 서명** (아래 영역에 서명해주세요)")

    canvas_result = st_canvas(
        fill_color="rgba(255, 255, 255, 0)",
        stroke_width=3,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=200,
        width=600,
        drawing_mode="freedraw",
        key=f"canvas_{training_id}_{dept}_{name}",
        display_toolbar=True,
    )

    st.caption("💡 좌측 상단 🗑 전체 지우기 · ↶ 되돌리기")

    if st.button("✅ 서명 제출", type="primary", use_container_width=True):
        if canvas_result.image_data is None or is_canvas_empty(canvas_result.image_data):
            st.error("서명을 해주세요.")
            return

        with st.spinner("저장 중..."):
            try:
                png_bytes = canvas_to_png_bytes(canvas_result.image_data)
                save_signature(training_id, training["연수명"], dept, name, png_bytes)
                load_signed_names_for_training.clear()
                st.success(f"✅ {dept} {name}님, 제출 완료! 참석해주셔서 감사합니다.")
                st.balloons()
            except Exception as e:
                st.error(f"저장 중 오류: {e}")


# ───────────────────────────────────────────────────────
# 관리자 페이지
# ───────────────────────────────────────────────────────
def render_admin_page():
    st.title("🔧 연수 방명록 관리")

    if not st.session_state.get("admin_authed"):
        pw = st.text_input("관리자 비밀번호", type="password")
        if st.button("로그인"):
            if pw == ADMIN_PASSWORD:
                st.session_state["admin_authed"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        return

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📋 연수 관리", "👥 교직원 명부", "📊 서명 기록", "🔗 공유 링크", "🔍 진단"]
    )

    # 탭 1: 연수 관리
    with tab1:
        st.subheader("새 연수 등록")
        with st.form("new_training", clear_on_submit=True):
            new_name = st.text_input("연수명")
            c1, c2 = st.columns(2)
            new_date = c1.text_input("일시 (예: 2026-04-20 14:00)")
            new_place = c2.text_input("장소")
            if st.form_submit_button("등록"):
                if new_name and new_date:
                    try:
                        tid = uuid.uuid4().hex[:8]
                        trainings_ws, _, _ = get_sheets()
                        trainings_ws.append_row(
                            [
                                tid, new_name, new_date, new_place, "진행중",
                                datetime.datetime.now().strftime("%Y-%m-%d"),
                            ]
                        )
                        st.cache_data.clear()
                        st.success(f"등록 완료! ID: `{tid}`")
                    except Exception as e:
                        st.error(f"등록 실패: {e}")
                else:
                    st.error("연수명과 일시는 필수입니다.")

        st.divider()
        st.subheader("등록된 연수")
        st.caption("'종료' 상태의 연수는 참석자 페이지에 표시되지 않습니다.")
        try:
            trainings = load_trainings()
        except Exception as e:
            st.error(str(e))
            trainings = []

        if trainings:
            for t in reversed(trainings):
                status = str(t.get("상태", "진행중"))
                emoji = "🟢" if status == "진행중" else "⚫"
                with st.expander(f"{emoji} {t['연수명']} ({t['일시']}) - {status}"):
                    st.text(f"ID: {t['training_id']}")
                    st.text(f"장소: {t['장소']}")
                    st.text(f"생성일: {t.get('생성일', '-')}")

                    new_status = "종료" if status == "진행중" else "진행중"
                    if st.button(
                        f"상태를 '{new_status}'(으)로 변경",
                        key=f"toggle_{t['training_id']}",
                    ):
                        try:
                            trainings_ws, _, _ = get_sheets()
                            cell = trainings_ws.find(str(t["training_id"]))
                            if cell:
                                trainings_ws.update_cell(cell.row, 5, new_status)
                                st.cache_data.clear()
                                st.success("변경 완료")
                                st.rerun()
                        except Exception as e:
                            st.error(f"변경 실패: {e}")

    # 탭 2: 교직원 명부
    with tab2:
        st.subheader("교직원 명부")
        st.info(
            "💡 대량 입력은 Spreadsheet의 **'교직원명부'** 탭을 직접 편집하는 게 훨씬 빠릅니다.\n\n"
            "구조: `부서 | 이름` (첫 행은 헤더)"
        )
        try:
            teachers_by_dept = load_teachers()
        except Exception as e:
            st.error(str(e))
            teachers_by_dept = {}

        if teachers_by_dept:
            total = sum(len(names) for names in teachers_by_dept.values())
            st.caption(f"총 {len(teachers_by_dept)}개 부서, {total}명 등록")
            for dept in sorted(teachers_by_dept.keys()):
                with st.expander(f"📁 {dept} ({len(teachers_by_dept[dept])}명)"):
                    st.write(", ".join(sorted(teachers_by_dept[dept])))
        else:
            st.warning("교직원 명부가 비어있습니다.")

        st.divider()
        st.subheader("➕ 교직원 1명 추가")
        with st.form("add_teacher", clear_on_submit=True):
            c1, c2 = st.columns(2)
            add_dept = c1.text_input("부서")
            add_name = c2.text_input("이름")
            if st.form_submit_button("추가"):
                if add_dept and add_name:
                    try:
                        _, _, teachers_ws = get_sheets()
                        teachers_ws.append_row([add_dept.strip(), add_name.strip()])
                        st.cache_data.clear()
                        st.success(f"{add_dept} {add_name} 추가됨")
                    except Exception as e:
                        st.error(f"추가 실패: {e}")

    # 탭 3: 서명 기록
    with tab3:
        st.subheader("서명 기록")
        try:
            _, records_ws, _ = get_sheets()
            records = records_ws.get_all_records()
            trainings = load_trainings()
        except Exception as e:
            st.error(str(e))
            records, trainings = [], []

        if not records:
            st.info("아직 서명 기록이 없습니다.")
        else:
            training_names = ["전체"] + [t["연수명"] for t in trainings]
            selected = st.selectbox("연수 선택", training_names)

            filtered = (
                records if selected == "전체"
                else [r for r in records if r["연수명"] == selected]
            )
            st.caption(f"총 {len(filtered)}건")
            st.dataframe(filtered, use_container_width=True, hide_index=True)

            # 미서명자 (선택한 연수 기준)
            if selected != "전체":
                try:
                    teachers_by_dept = load_teachers()
                    all_teachers = {
                        (d, n) for d, names in teachers_by_dept.items() for n in names
                    }
                    signed = {(r["부서"], r["이름"]) for r in filtered}
                    missing = sorted(all_teachers - signed)
                    if missing:
                        with st.expander(f"❗ 미서명자 ({len(missing)}명)"):
                            by_d: dict[str, list[str]] = {}
                            for d, n in missing:
                                by_d.setdefault(d, []).append(n)
                            for d in sorted(by_d.keys()):
                                st.text(f"  [{d}] {', '.join(by_d[d])}")
                except Exception:
                    pass

    # 탭 4: 공유 링크
    with tab4:
        st.subheader("참석자 공유 링크")
        st.caption(
            "모든 참석자는 같은 링크로 접속해서 직접 연수를 선택합니다."
        )
        base_url = st.text_input(
            "배포된 앱 URL",
            value=st.session_state.get("base_url", "https://your-app.streamlit.app"),
        )
        st.session_state["base_url"] = base_url
        st.code(base_url, language=None)

        try:
            import qrcode
            img = qrcode.make(base_url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=250, caption="QR 코드")
            st.download_button(
                "QR 코드 다운로드",
                buf.getvalue(),
                file_name="연수방명록_QR.png",
                mime="image/png",
            )
        except ImportError:
            st.caption("(qrcode 패키지 설치 시 QR 자동 생성)")

    # 탭 5: 진단
    with tab5:
        render_diagnostics()
        if st.button("🔄 캐시 초기화"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.success("초기화 완료. 새로고침 해주세요.")


def render_diagnostics():
    st.subheader("🔍 설정 진단")
    checks = {
        "spreadsheet_id": SPREADSHEET_ID,
        "drive_folder_id": DRIVE_FOLDER_ID,
        "Service Account": st.secrets["gcp_service_account"].get("client_email", "(없음)"),
    }
    for k, v in checks.items():
        st.text(f"  {k}: {v}")

    try:
        get_sheets.clear()
        trainings_ws, records_ws, teachers_ws = get_sheets()
        st.success(f"✅ Spreadsheet: `{trainings_ws.spreadsheet.title}`")
        st.text(f"  연수목록: {trainings_ws.row_count}행")
        st.text(f"  서명기록: {records_ws.row_count}행")
        st.text(f"  교직원명부: {teachers_ws.row_count}행")
    except Exception as e:
        st.error(str(e))
        return

    try:
        service = get_drive_service()
        folder = service.files().get(fileId=DRIVE_FOLDER_ID, fields="id,name").execute()
        st.success(f"✅ Drive 폴더: `{folder['name']}`")
    except Exception as e:
        st.error(f"❌ Drive 폴더 접근 실패: {e}")


# ───────────────────────────────────────────────────────
# 라우팅
# ───────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="연수 방명록", page_icon="📝", layout="centered")

    if st.query_params.get("admin") == "1":
        render_admin_page()
    else:
        render_signing_flow()


if __name__ == "__main__":
    main()
