"""
학교 연수 방명록 서명 앱
- streamlit-drawable-canvas로 손글씨 서명
- Google Drive에 서명 PNG 저장, Google Sheets에 메타데이터 기록
"""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import numpy as np
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image
from streamlit_drawable_canvas import st_canvas

# ---------- 설정 ----------
SHEET_ID = st.secrets["sheet_id"]           # secrets.toml에서 로드
DRIVE_FOLDER_ID = st.secrets["drive_folder_id"]  # 서명 이미지 저장할 Drive 폴더
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
KST = ZoneInfo("Asia/Seoul")

st.set_page_config(page_title="연수 방명록", page_icon="✍️", layout="centered")

# ---------- 인증 (캐시) ----------
@st.cache_resource
def get_credentials():
    return Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )

@st.cache_resource
def get_gspread_client():
    return gspread.authorize(get_credentials())

@st.cache_resource
def get_drive_service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)

# ---------- 데이터 로드 ----------
@st.cache_data(ttl=60)  # 1분 캐시 — 연수 목록 바뀔 일 드물어서
def load_active_trainings():
    """연수목록 시트에서 활성화된 연수만 가져오기"""
    sh = get_gspread_client().open_by_key(SHEET_ID)
    ws = sh.worksheet("연수목록")
    rows = ws.get_all_records()
    return [r for r in rows if str(r.get("활성화", "")).strip() in ("Y", "y", "TRUE", "True", "1")]

# ---------- 저장 로직 ----------
def upload_signature_to_drive(png_bytes: bytes, filename: str) -> str:
    """PNG를 Drive에 업로드하고 공유 링크 반환"""
    drive = get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png", resumable=False)
    file = drive.files().create(
        body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    # 링크로 누구나 보기 가능하게 (학교 내부 링크 공유 정책에 따라 조정)
    drive.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"},
    ).execute()
    return file["webViewLink"]

def append_signature_record(training: str, name: str, affiliation: str, signature_url: str):
    """서명기록 시트에 한 행 추가"""
    sh = get_gspread_client().open_by_key(SHEET_ID)
    ws = sh.worksheet("서명기록")
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row(
        [timestamp, training, name, affiliation, signature_url],
        value_input_option="USER_ENTERED",
    )

def canvas_to_png_bytes(image_data: np.ndarray) -> bytes:
    """캔버스 RGBA 배열 → 흰 배경 PNG bytes"""
    img = Image.fromarray(image_data.astype("uint8"), mode="RGBA")
    # 투명 배경을 흰색으로 합성 (인쇄 증빙용)
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    bg.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def signature_is_empty(image_data: np.ndarray) -> bool:
    """서명이 비어있는지 (알파 채널 기준)"""
    return image_data[:, :, 3].sum() == 0

# ---------- UI ----------
st.title("✍️ 연수 방명록")
st.caption("참석하신 연수를 선택하고 서명해주세요.")

# 연수 선택
try:
    trainings = load_active_trainings()
except Exception as e:
    st.error(f"연수 목록을 불러올 수 없어요: {e}")
    st.stop()

if not trainings:
    st.warning("현재 활성화된 연수가 없습니다. 담당자에게 문의해주세요.")
    st.stop()

training_labels = [f"{t['연수명']} ({t.get('일시', '')})" for t in trainings]
idx = st.selectbox("연수 선택", range(len(trainings)), format_func=lambda i: training_labels[i])
selected_training = trainings[idx]["연수명"]

# 참석자 정보
col1, col2 = st.columns(2)
with col1:
    name = st.text_input("이름", max_chars=20)
with col2:
    affiliation = st.text_input("소속 (학교/부서)", value="삼괴고등학교", max_chars=30)

st.markdown("**서명**")
st.caption("아래 흰색 영역에 손글씨로 서명해주세요.")

# 캔버스 — update_streamlit=False가 속도의 핵심
canvas_result = st_canvas(
    fill_color="rgba(0, 0, 0, 0)",
    stroke_width=3,
    stroke_color="#000000",
    background_color="#FFFFFF",
    width=600,
    height=200,
    drawing_mode="freedraw",
    update_streamlit=False,   # ★ 그릴 때마다 rerun 방지 → 반응 속도 확보
    key="signature_canvas",
    display_toolbar=True,     # 지우개/되돌리기 버튼
)

# 제출
submit = st.button("제출", type="primary", use_container_width=True)

if submit:
    # 검증
    if not name.strip():
        st.error("이름을 입력해주세요.")
        st.stop()
    if canvas_result.image_data is None or signature_is_empty(canvas_result.image_data):
        st.error("서명을 입력해주세요.")
        st.stop()

    with st.spinner("저장 중..."):
        try:
            png_bytes = canvas_to_png_bytes(canvas_result.image_data)
            timestamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{selected_training}_{name}.png"
            url = upload_signature_to_drive(png_bytes, filename)
            append_signature_record(selected_training, name.strip(), affiliation.strip(), url)
        except Exception as e:
            st.error(f"저장 중 오류: {e}")
            st.stop()

    st.success(f"✅ {name} 선생님, 서명이 등록되었습니다.")
    st.balloons()
    # 다음 사람을 위해 자동 초기화 — 3초 후 rerun
    import time
    time.sleep(2)
    st.rerun()
