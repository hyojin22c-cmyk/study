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

from pdf_roster import generate_attendance_pdf

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
        # 헤더 순서 검증 (append_row는 위치 기준이므로 헤더 순서가 중요)
        header = records.row_values(1)
        expected = ["training_id", "연수명", "부서", "이름", "제출시각", "서명파일", "서명URL", "서명FileID"]
        if header and header[:len(expected)] != expected[:len(header)]:
            # 심각한 불일치: 부서/이름 순서 뒤바뀜 등
            if "부서" in header and "이름" in header:
                dept_idx = header.index("부서")
                name_idx = header.index("이름")
                if dept_idx != 2 or name_idx != 3:
                    raise RuntimeError(
                        f"❌ '서명기록' 시트 헤더 순서가 잘못되었습니다.\n\n"
                        f"현재: {header}\n"
                        f"필요: {expected}\n\n"
                        f"Spreadsheet에서 헤더를 수정하고 기존 데이터를 정리해주세요."
                    )
    except gspread.WorksheetNotFound:
        records = sh.add_worksheet("서명기록", rows=1000, cols=8)
        records.append_row(
            ["training_id", "연수명", "부서", "이름", "제출시각", "서명파일", "서명URL", "서명FileID"]
        )

    # 교직원명부 (신규)
    try:
        teachers = sh.worksheet("교직원명부")
    except gspread.WorksheetNotFound:
        teachers = sh.add_worksheet("교직원명부", rows=200, cols=3)
        teachers.append_row(["연번", "부서", "이름"])
        teachers.append_row(["1", "교무부", "(예시) 김교무"])
        teachers.append_row(["2", "연구부", "(예시) 이연구"])

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


@st.cache_data(ttl=60)
def load_teachers_ordered() -> list[tuple[str, str]]:
    """교직원 명부를 연번 순서대로 [(부서, 이름), ...] 반환. 결재 명부 PDF용."""
    _, _, teachers_ws = get_sheets()
    rows = teachers_ws.get_all_records()
    ordered = []
    for r in rows:
        dept = str(r.get("부서", "")).strip()
        name = str(r.get("이름", "")).strip()
        if dept and name:
            # 연번이 있으면 int 변환해서 정렬, 없으면 등록 순서 유지
            try:
                num = int(r.get("연번", 0))
            except (ValueError, TypeError):
                num = 0
            ordered.append((num, dept, name))

    # 연번이 있으면 그 순서로, 없으면 등록 순서
    if any(n > 0 for n, _, _ in ordered):
        ordered.sort(key=lambda x: x[0] if x[0] > 0 else 9999)
    return [(d, n) for _, d, n in ordered]


@st.cache_data(ttl=30)
def load_signed_names_for_training(training_id: str) -> set:
    """해당 연수에 이미 서명한 (부서, 이름) 집합. 구 버전 '소속' 컬럼도 호환."""
    _, records_ws, _ = get_sheets()
    all_records = records_ws.get_all_records()
    result = set()
    for r in all_records:
        if str(r.get("training_id", "")) != str(training_id):
            continue
        # '부서' 또는 '소속' 둘 다 지원 (레거시 데이터 호환)
        dept = str(r.get("부서") or r.get("소속") or "").strip()
        name = str(r.get("이름", "")).strip()
        if dept and name:
            result.add((dept, name))
    return result


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
        supportsAllDrives=True,
    ).execute()
    return file["id"], file["webViewLink"]


def download_signature_from_drive(file_id: str) -> bytes:
    """Drive 파일 ID로 PNG bytes를 다운로드."""
    from googleapiclient.http import MediaIoBaseDownload
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def delete_signature_from_drive(file_id: str) -> None:
    """Drive에서 서명 파일 삭제 (실패해도 예외 raise만, 호출부에서 처리)."""
    service = get_drive_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()


def delete_signature_record(row_number: int, file_id: str = "") -> None:
    """
    서명 기록 삭제 (Sheets 행 + Drive 파일).
    row_number: 1-indexed (헤더 포함). 실제 데이터 첫 행은 2.
    """
    # Drive 파일 먼저 삭제 (있으면)
    if file_id:
        try:
            delete_signature_from_drive(file_id)
        except Exception:
            pass  # Drive 파일이 이미 없어도 Sheets 행은 지움

    # Sheets 행 삭제
    _, records_ws, _ = get_sheets()
    records_ws.delete_rows(row_number)


def extract_file_id_from_url(url: str) -> str | None:
    """Drive URL에서 파일 ID 추출."""
    # https://drive.google.com/file/d/{ID}/view?usp=drivesdk
    import re
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def save_signature(training_id, training_name, dept, name, png_bytes):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = name.replace("/", "_").replace(" ", "_")
    filename = f"{training_id}_{dept}_{safe_name}_{uuid.uuid4().hex[:6]}.png"

    file_id, view_url = upload_signature_to_drive(png_bytes, filename)

    _, records_ws, _ = get_sheets()
    records_ws.append_row(
        [training_id, training_name, dept, name, timestamp, filename, view_url, file_id],
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

    # 방금 서명 제출한 경우: 확인 화면 표시
    if "just_submitted" in st.session_state:
        info = st.session_state["just_submitted"]
        st.success(
            f"✅ **{info['dept']} {info['name']}**님, 서명이 정상적으로 제출되었습니다!"
        )
        st.markdown("**제출된 서명 확인:**")
        st.image(info["png"], width=500)
        st.caption(
            "📌 서명이 흐리거나 잘못되었다면 연수 담당자에게 재서명을 요청해주세요."
        )

        c1, c2 = st.columns(2)
        if c1.button("🔄 다른 분 서명하기", use_container_width=True, type="primary"):
            del st.session_state["just_submitted"]
            st.rerun()
        if c2.button("🚪 종료", use_container_width=True):
            del st.session_state["just_submitted"]
            st.rerun()
        return

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

    # 1단계: 연수 선택 (1개면 자동 선택)
    if len(active) == 1:
        training = active[0]
        training_id = str(training["training_id"])
        st.success(
            f"📝 **{training['연수명']}**  \n"
            f"📅 {training['일시']} · 📍 {training['장소']}"
        )
    else:
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

    # 2단계: 이름 검색 (부서 선택 없이 바로 이름으로)
    try:
        teachers_by_dept = load_teachers()
    except Exception as e:
        st.error(f"교직원 명부 로드 실패: {e}")
        return

    if not teachers_by_dept:
        st.warning("교직원 명부가 비어있습니다. 관리자에게 문의해주세요.")
        return

    # 전체 교직원을 "이름 [부서]" 형태의 문자열 리스트로
    # 이미 서명한 사람은 체크마크로 표시하되 목록에 포함 (본인이 서명했는지 확인 가능하도록)
    signed = load_signed_names_for_training(training_id)
    all_teachers = []
    for d in sorted(teachers_by_dept.keys()):
        for n in sorted(teachers_by_dept[d]):
            all_teachers.append((d, n))

    def make_label(dept, name, is_signed):
        prefix = "✅ " if is_signed else ""
        return f"{prefix}{name} [{dept}]"

    # 모든 교직원을 라벨 → (부서, 이름, 서명여부) 로 매핑
    all_labels = []
    label_to_info = {}
    for d, n in all_teachers:
        is_signed = (d, n) in signed
        label = make_label(d, n, is_signed)
        all_labels.append(label)
        label_to_info[label] = (d, n, is_signed)

    already_signed_count = len(signed)

    # 미서명자 먼저, 서명완료자 뒤로 정렬
    all_labels.sort(key=lambda lbl: (label_to_info[lbl][2], lbl))

    selected_label = st.selectbox(
        "**2️⃣ 본인 이름을 선택하세요** (이름 일부를 입력하면 바로 찾을 수 있어요)",
        options=all_labels,
        index=None,
        placeholder="이름 두 글자 입력하면 바로 나와요...",
    )

    # 진행 상황 피드백
    if already_signed_count > 0:
        st.caption(
            f"✓ 현재까지 {already_signed_count}명 서명 완료 "
            f"(전체 {len(all_teachers)}명) · ✅ 표시는 서명 완료자"
        )

    if not selected_label:
        return

    dept, name, is_signed = label_to_info[selected_label]

    # 이미 서명한 사람이면 완료 안내 후 종료
    if is_signed:
        st.success(
            f"✅ **{dept} {name}**님은 이 연수에 이미 서명을 완료하셨습니다.\n\n"
            f"추가 서명은 필요하지 않습니다. 감사합니다!"
        )
        return

    # 3단계: 서명
    st.markdown(f"**3️⃣ 서명** — [{dept}] {name}  아래 영역에 서명해주세요")

    canvas_result = st_canvas(
        fill_color="rgba(255, 255, 255, 0)",
        stroke_width=4,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=280,
        width=700,
        drawing_mode="freedraw",
        key=f"canvas_{training_id}_{dept}_{name}",
        display_toolbar=True,
    )

    st.caption(
        "💡 좌측 상단 🗑 전체 지우기 · ↶ 되돌리기 · "
        "모바일에서는 손가락으로, PC에서는 마우스/트랙패드로 서명해주세요."
    )

    if st.button("✅ 서명 제출", type="primary", use_container_width=True):
        if canvas_result.image_data is None or is_canvas_empty(canvas_result.image_data):
            st.error("서명을 해주세요.")
            return

        with st.spinner("저장 중..."):
            try:
                png_bytes = canvas_to_png_bytes(canvas_result.image_data)
                save_signature(training_id, training["연수명"], dept, name, png_bytes)
                load_signed_names_for_training.clear()
                # 세션에 방금 제출한 서명 정보 저장 (확인용)
                st.session_state["just_submitted"] = {
                    "dept": dept,
                    "name": name,
                    "png": png_bytes,
                    "training_id": training_id,
                }
                st.balloons()
                st.rerun()
            except Exception as e:
                st.error(f"저장 중 오류: {e}")


def render_attendee_footer():
    """참석자 페이지 하단의 담백한 관리자 링크."""
    st.divider()
    st.caption(
        "🔧 연수 담당자이신가요? → [관리자 페이지로 이동](?admin=1)"
    )


# ───────────────────────────────────────────────────────
# 관리자 페이지
# ───────────────────────────────────────────────────────
def render_admin_page():
    # 헤더: 제목 + 참석자 페이지 링크
    c1, c2 = st.columns([3, 1])
    with c1:
        st.title("🔧 연수 방명록 관리")
    with c2:
        st.write("")  # 세로 정렬용 여백
        st.link_button("📝 참석자 페이지", url="?", use_container_width=True)

    if not st.session_state.get("admin_authed"):
        pw = st.text_input("관리자 비밀번호", type="password")
        if st.button("로그인"):
            if pw == ADMIN_PASSWORD:
                st.session_state["admin_authed"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        return

    # 로그아웃 버튼 (탭 위)
    if st.button("🚪 로그아웃", key="admin_logout"):
        st.session_state["admin_authed"] = False
        st.rerun()

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        ["📋 연수 관리", "👥 교직원 명부", "📊 서명 기록", "📄 결재 명부", "🔗 공유 링크", "🔍 진단"]
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
            c1, c2, c3 = st.columns([1, 2, 2])
            add_num = c1.text_input("연번", placeholder="자동")
            add_dept = c2.text_input("부서")
            add_name = c3.text_input("이름")
            if st.form_submit_button("추가"):
                if add_dept and add_name:
                    try:
                        _, _, teachers_ws = get_sheets()
                        # 연번 비어있으면 현재 최대값 + 1
                        if not add_num.strip():
                            existing = teachers_ws.get_all_records()
                            max_num = max(
                                [int(r.get("연번", 0)) for r in existing if str(r.get("연번", "")).isdigit()],
                                default=0,
                            )
                            add_num = str(max_num + 1)
                        teachers_ws.append_row([add_num, add_dept.strip(), add_name.strip()])
                        st.cache_data.clear()
                        st.success(f"{add_num}. {add_dept} {add_name} 추가됨")
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
            selected = st.selectbox("연수 선택", training_names, key="records_training_select")

            # 원본 행 번호를 유지하면서 필터
            # get_all_records는 헤더 제외한 리스트를 반환 → 실제 시트 행번호는 index+2
            indexed_records = [(i + 2, r) for i, r in enumerate(records)]
            if selected != "전체":
                filtered_indexed = [
                    (row_num, r) for row_num, r in indexed_records
                    if r.get("연수명") == selected
                ]
            else:
                filtered_indexed = indexed_records

            st.caption(f"총 {len(filtered_indexed)}건")

            # 삭제 확인 상태 관리
            pending_delete_key = "pending_delete_row"

            # 개별 레코드 표시 + 삭제 버튼
            with st.expander("📋 개별 기록 보기 (삭제 가능)", expanded=False):
                for row_num, r in filtered_indexed:
                    dept = str(r.get("부서") or r.get("소속") or "-")
                    name = str(r.get("이름") or "-")
                    time = str(r.get("제출시각") or "-")
                    training = str(r.get("연수명") or "-")
                    file_id = str(r.get("서명FileID") or "")

                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.text(f"[{training}] {dept} {name} · {time}")
                    with c2:
                        # 삭제 확인 단계: pending이 이 행을 가리키면 "정말 삭제" 버튼
                        if st.session_state.get(pending_delete_key) == row_num:
                            if st.button("⚠️ 확정", key=f"confirm_del_{row_num}", type="primary"):
                                try:
                                    delete_signature_record(row_num, file_id)
                                    st.session_state.pop(pending_delete_key, None)
                                    st.cache_data.clear()
                                    load_signed_names_for_training.clear()
                                    st.success(f"{dept} {name} 서명 삭제됨")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"삭제 실패: {e}")
                        else:
                            if st.button("🗑", key=f"del_{row_num}", help="서명 삭제"):
                                st.session_state[pending_delete_key] = row_num
                                st.rerun()

                # 삭제 취소
                if pending_delete_key in st.session_state:
                    st.warning(
                        "⚠️ 위에서 '확정'을 다시 누르면 서명 기록과 이미지가 **영구 삭제**됩니다."
                    )
                    if st.button("취소", key="cancel_delete"):
                        st.session_state.pop(pending_delete_key, None)
                        st.rerun()

            # 전체 dataframe (복사/확인용)
            with st.expander("📊 표로 보기"):
                st.dataframe(
                    [r for _, r in filtered_indexed],
                    use_container_width=True,
                    hide_index=True,
                )

            # 미서명자 (선택한 연수 기준)
            if selected != "전체":
                try:
                    teachers_by_dept = load_teachers()
                    all_teachers = {
                        (d, n) for d, names in teachers_by_dept.items() for n in names
                    }
                    signed = {
                        (str(r.get("부서") or r.get("소속") or ""), str(r.get("이름", "")))
                        for _, r in filtered_indexed
                    }
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

    # 탭 4: 결재 명부 다운로드
    with tab4:
        st.subheader("📄 결재용 연수 확인 명부 생성")
        st.caption(
            "선택한 연수의 교직원 명부 + 서명 이미지를 합쳐서 A4 1장 PDF로 생성합니다. "
            "한글에서 열어 편집하거나 바로 인쇄해서 결재 올릴 수 있습니다."
        )

        try:
            trainings = load_trainings()
        except Exception as e:
            st.error(str(e))
            trainings = []

        if not trainings:
            st.info("먼저 연수를 등록해주세요.")
        else:
            training_options = [f"{t['연수명']} ({t['일시']})" for t in trainings]
            idx = st.selectbox(
                "연수 선택",
                options=range(len(trainings)),
                format_func=lambda i: training_options[i],
                index=None,
                placeholder="연수를 선택하세요...",
                key="roster_training_select",
            )

            if idx is not None:
                selected_training = trainings[idx]
                training_id = str(selected_training["training_id"])

                # 해당 연수의 서명 기록 조회
                try:
                    _, records_ws, _ = get_sheets()
                    all_records = records_ws.get_all_records()
                    training_records = [
                        r for r in all_records if str(r["training_id"]) == training_id
                    ]
                except Exception as e:
                    st.error(f"서명 기록 조회 실패: {e}")
                    training_records = []

                # 교직원 명부 (순서대로)
                try:
                    teachers = load_teachers_ordered()
                except Exception as e:
                    st.error(f"교직원 명부 조회 실패: {e}")
                    teachers = []

                if not teachers:
                    st.warning("교직원 명부가 비어있습니다.")
                else:
                    signed_count = len(training_records)
                    total_count = len(teachers)

                    c1, c2, c3 = st.columns(3)
                    c1.metric("전체 교직원", f"{total_count}명")
                    c2.metric("서명 완료", f"{signed_count}명")
                    c3.metric(
                        "미서명",
                        f"{total_count - signed_count}명",
                        delta=None,
                    )

                    # 생성 버튼 (일반 + 마감까지 원클릭)
                    current_status = str(selected_training.get("상태", "진행중"))
                    is_ongoing = current_status != "종료"

                    btn_col1, btn_col2 = st.columns(2)
                    gen_clicked = btn_col1.button(
                        "📥 결재 명부 PDF 생성",
                        type="primary",
                        use_container_width=True,
                    )
                    close_and_gen_clicked = btn_col2.button(
                        "🔒 연수 마감 + PDF 생성",
                        use_container_width=True,
                        disabled=not is_ongoing,
                        help=(
                            "연수를 '종료' 상태로 바꾸고 PDF를 생성합니다. "
                            "이후 참석자는 더 이상 서명할 수 없습니다."
                            if is_ongoing
                            else "이미 종료된 연수입니다."
                        ),
                    )

                    # 마감 버튼 눌렀으면 먼저 상태 변경
                    if close_and_gen_clicked:
                        try:
                            trainings_ws, _, _ = get_sheets()
                            cell = trainings_ws.find(str(selected_training["training_id"]))
                            if cell:
                                trainings_ws.update_cell(cell.row, 5, "종료")
                                st.cache_data.clear()
                                st.success("🔒 연수를 '종료' 상태로 변경했습니다.")
                        except Exception as e:
                            st.error(f"연수 마감 실패 (PDF는 계속 생성 시도): {e}")

                    if gen_clicked or close_and_gen_clicked:
                        with st.spinner("서명 이미지를 불러와 PDF를 생성하는 중..."):
                            try:
                                # 서명 이미지 다운로드: {(부서, 이름): png_bytes}
                                signatures = {}
                                failed = []
                                progress = st.progress(0)
                                for i, r in enumerate(training_records):
                                    dept = str(r.get("부서") or r.get("소속") or "").strip()
                                    name = str(r.get("이름", "")).strip()

                                    # file_id 우선, 없으면 URL에서 추출 (레거시 호환)
                                    file_id = str(r.get("서명FileID") or "").strip()
                                    if not file_id:
                                        file_id = extract_file_id_from_url(
                                            str(r.get("서명URL", ""))
                                        ) or ""

                                    if not dept or not name:
                                        failed.append(f"빈 이름/부서 행 (row {i+2})")
                                    elif not file_id:
                                        failed.append(f"{dept} {name}: 파일 ID 없음")
                                    else:
                                        try:
                                            png = download_signature_from_drive(file_id)
                                            signatures[(dept, name)] = png
                                        except Exception as ex:
                                            failed.append(f"{dept} {name}: 다운로드 실패 - {ex}")
                                    progress.progress((i + 1) / max(len(training_records), 1))
                                progress.empty()

                                if failed:
                                    with st.expander(f"⚠️ 서명 이미지 {len(failed)}건 처리 실패"):
                                        for msg in failed:
                                            st.text(msg)

                                # PDF 생성
                                pdf_bytes = generate_attendance_pdf(
                                    training_name=str(selected_training["연수명"]),
                                    training_date=str(selected_training["일시"]),
                                    teachers=teachers,
                                    signatures=signatures,
                                    school_year=datetime.datetime.now().strftime("%Y학년도"),
                                )

                                # 타입 검증
                                if not isinstance(pdf_bytes, (bytes, bytearray)):
                                    raise TypeError(
                                        f"PDF 생성 결과가 bytes가 아닙니다: {type(pdf_bytes)}"
                                    )
                                if len(pdf_bytes) == 0:
                                    raise ValueError("PDF 생성 결과가 비어있습니다.")

                                # 매칭 검증: 서명은 있는데 명부에 없는 경우 체크
                                teacher_set = set(teachers)
                                unmatched = [
                                    (d, n) for (d, n) in signatures.keys()
                                    if (d, n) not in teacher_set
                                ]
                                if unmatched:
                                    with st.expander(
                                        f"⚠️ 서명은 있지만 교직원 명부와 매칭 안 됨 ({len(unmatched)}명)"
                                    ):
                                        st.caption(
                                            "이 사람들의 서명은 PDF에 **표시되지 않습니다**. "
                                            "교직원 명부의 부서/이름과 정확히 일치해야 PDF에 반영됩니다."
                                        )
                                        for d, n in unmatched:
                                            st.text(f"  서명기록: [{d}] {n}")

                                # 다운로드 버튼 (세션 저장)
                                st.session_state["roster_pdf"] = pdf_bytes
                                st.session_state["roster_filename"] = (
                                    f"연수확인명부_{selected_training['연수명']}.pdf"
                                )
                                matched = len(signatures) - len(unmatched)
                                st.success(
                                    f"✅ PDF 생성 완료! "
                                    f"PDF에 포함된 서명: **{matched}명** "
                                    f"(다운로드한 서명: {len(signatures)}명, 교직원: {total_count}명)"
                                )
                            except Exception as e:
                                st.error(f"PDF 생성 실패: {e}")
                                import traceback
                                with st.expander("상세 에러"):
                                    st.code(traceback.format_exc())

                    # 생성된 PDF 다운로드
                    pdf_data = st.session_state.get("roster_pdf")
                    if isinstance(pdf_data, (bytes, bytearray)) and len(pdf_data) > 0:
                        st.download_button(
                            "⬇️ PDF 다운로드",
                            data=bytes(pdf_data),
                            file_name=st.session_state.get(
                                "roster_filename", "연수확인명부.pdf"
                            ),
                            mime="application/pdf",
                            use_container_width=True,
                        )
                        st.caption(
                            "💡 한글 프로그램에서 [파일 → 불러오기 → PDF] 로 열어 편집 가능합니다."
                        )

    # 탭 5: 공유 링크
    with tab5:
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

    # 탭 6: 진단
    with tab6:
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
        folder = service.files().get(
            fileId=DRIVE_FOLDER_ID, fields="id,name", supportsAllDrives=True
        ).execute()
        st.success(f"✅ Drive 폴더: `{folder['name']}`")
    except Exception as e:
        st.error(f"❌ Drive 폴더 접근 실패: {e}")

    # ── 교직원 명부 상세 진단 ──
    st.divider()
    st.subheader("👥 교직원 명부 상세")

    try:
        load_teachers.clear()
        load_teachers_ordered.clear()
        _, _, teachers_ws = get_sheets()
        raw_rows = teachers_ws.get_all_records()
        by_dept = load_teachers()
        ordered = load_teachers_ordered()

        st.text(f"시트 원본 행 수: {len(raw_rows)}")
        st.text(f"파싱된 교직원 수 (부서별 합계): {sum(len(v) for v in by_dept.values())}")
        st.text(f"정렬된 명부 길이: {len(ordered)}")

        # 특정 이름 검색
        search_name = st.text_input(
            "🔎 특정 이름 검색 (명부에서 찾기)",
            placeholder="예: 공명현, 최효진",
        )
        if search_name:
            search_name = search_name.strip()
            st.write("**시트 원본에서 매칭되는 행:**")
            matches = []
            for i, r in enumerate(raw_rows, start=2):  # 2부터 (헤더 제외)
                name_val = str(r.get("이름", ""))
                if search_name in name_val:
                    matches.append((i, r, repr(name_val)))
            if matches:
                for row_num, r, name_repr in matches:
                    st.code(
                        f"행 {row_num}: 연번={r.get('연번')!r}, "
                        f"부서={r.get('부서')!r}, "
                        f"이름={name_repr}"
                    )
            else:
                st.error(f"'{search_name}'을 포함하는 이름이 시트 원본에 없습니다.")

            st.write("**파싱 결과에서 매칭:**")
            found = [(d, n) for d, names in by_dept.items() for n in names if search_name in n]
            if found:
                for d, n in found:
                    st.text(f"  [{d}] {n!r}")
            else:
                st.error(f"'{search_name}'이 파싱 결과에 없습니다.")

        # 전체 이름 펼쳐보기
        with st.expander("📋 파싱된 전체 명부 보기 (공백/특수문자 확인)"):
            for d in sorted(by_dept.keys()):
                st.text(f"[{d}] ({len(by_dept[d])}명)")
                for n in sorted(by_dept[d]):
                    st.text(f"    {n!r}")  # repr로 보여서 숨은 공백 확인 가능

    except Exception as e:
        st.error(f"교직원 명부 진단 실패: {e}")
        import traceback
        st.code(traceback.format_exc())


# ───────────────────────────────────────────────────────
# 라우팅
# ───────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="연수 방명록", page_icon="📝", layout="centered")

    if st.query_params.get("admin") == "1":
        render_admin_page()
    else:
        render_signing_flow()
        render_attendee_footer()


if __name__ == "__main__":
    main()
