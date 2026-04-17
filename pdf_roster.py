"""
연수 확인 명부 PDF 생성 모듈

업로드된 양식(2026학년도 교직원 연수 확인 명부) 구조를 A4 1장에 재현:
- 제목 + 2단 표 (연번|부서|이름|확인)
- 서명 이미지를 '확인' 칸에 삽입
- 서명 없으면 빈 칸
"""

import io
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image


# ───────────────────────────────────────────────────────
# 폰트 등록 (한글)
# ───────────────────────────────────────────────────────
_FONTS_REGISTERED = False


def _register_korean_fonts():
    """나눔고딕 폰트를 등록. repo에 fonts/ 폴더가 있다고 가정."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return

    # 우선순위: repo 내 fonts/ → 시스템 폰트
    candidates_regular = [
        "fonts/NanumGothic.ttf",
        "fonts/NanumGothic-Regular.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    ]
    candidates_bold = [
        "fonts/NanumGothicBold.ttf",
        "fonts/NanumGothic-Bold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ]

    regular_path = next((p for p in candidates_regular if os.path.exists(p)), None)
    bold_path = next((p for p in candidates_bold if os.path.exists(p)), None)

    if not regular_path:
        raise FileNotFoundError(
            "한글 폰트(NanumGothic.ttf)를 찾을 수 없습니다. "
            "repo의 fonts/ 폴더에 NanumGothic.ttf를 추가해주세요."
        )

    pdfmetrics.registerFont(TTFont("KFont", regular_path))
    if bold_path:
        pdfmetrics.registerFont(TTFont("KFont-Bold", bold_path))
    else:
        pdfmetrics.registerFont(TTFont("KFont-Bold", regular_path))

    _FONTS_REGISTERED = True


# ───────────────────────────────────────────────────────
# PNG 이미지 bytes를 캔버스에 그리기 (비율 유지)
# ───────────────────────────────────────────────────────
def _draw_signature(c, png_bytes, x, y, w, h):
    """지정된 영역(x, y, w, h) 안에 서명 이미지를 비율 유지해서 그림."""
    from reportlab.lib.utils import ImageReader

    img = Image.open(io.BytesIO(png_bytes))
    iw, ih = img.size
    scale = min(w / iw, h / ih) * 0.9  # 90% 크기로 (약간 여백)
    draw_w, draw_h = iw * scale, ih * scale
    draw_x = x + (w - draw_w) / 2
    draw_y = y + (h - draw_h) / 2

    img_reader = ImageReader(io.BytesIO(png_bytes))
    c.drawImage(img_reader, draw_x, draw_y, draw_w, draw_h, mask="auto")


# ───────────────────────────────────────────────────────
# 메인 생성 함수
# ───────────────────────────────────────────────────────
def generate_attendance_pdf(
    training_name: str,
    training_date: str,
    teachers: list[tuple[str, str]],  # [(부서, 이름), ...]
    signatures: dict[tuple[str, str], bytes],  # {(부서, 이름): png_bytes}
    school_year: str = "2026학년도",
) -> bytes:
    """
    연수 확인 명부 PDF 생성.

    Args:
        training_name: 연수명
        training_date: 연수일자 (예: "2026. 4. 20.")
        teachers: [(부서, 이름), ...] 순서대로 표시됨 (업로드된 PDF의 연번 순서)
        signatures: 서명한 사람 {(부서, 이름): PNG bytes}

    Returns:
        PDF bytes
    """
    _register_korean_fonts()

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    # ── 여백 설정 ──
    margin_left = 15 * mm
    margin_right = 15 * mm
    margin_top = 15 * mm
    margin_bottom = 15 * mm

    content_w = page_w - margin_left - margin_right  # 사용 가능 폭

    # ── 제목 ──
    title = f"{school_year} 교직원 연수 확인 명부"
    c.setFont("KFont-Bold", 16)
    title_y = page_h - margin_top - 8 * mm
    c.drawCentredString(page_w / 2, title_y, title)

    # ── 2단 표 레이아웃 ──
    # 각 단: 연번(8mm) | 부서(22mm) | 이름(18mm) | 확인(32mm) = 80mm
    # 단 사이 간격: 10mm
    # 총: 80 + 10 + 80 = 170mm (content_w는 180mm이므로 양쪽 여유 5mm)
    col_widths = [8 * mm, 22 * mm, 18 * mm, 32 * mm]
    column_w = sum(col_widths)  # 80mm
    gap = 10 * mm
    total_w = column_w * 2 + gap
    left_col_x = (page_w - total_w) / 2  # 가운데 정렬
    right_col_x = left_col_x + column_w + gap

    # ── 표 위치 ──
    table_top_y = title_y - 10 * mm
    header_h = 7 * mm
    row_h = 8 * mm  # 서명 들어갈 수 있는 높이

    # ── 인원 분할 (좌/우 단) ──
    total = len(teachers)
    half = (total + 1) // 2  # 홀수면 좌단에 한 명 더
    left_half = teachers[:half]
    right_half = teachers[half:]

    # 표 전체 높이 계산 (좌/우 중 많은 쪽 기준)
    max_rows = max(len(left_half), len(right_half))
    table_h = header_h + row_h * max_rows

    # ── 표 그리기 함수 ──
    def draw_column(col_x, start_num, rows):
        # 헤더
        c.setFont("KFont-Bold", 10)
        c.setLineWidth(0.5)

        # 헤더 배경 (옅은 회색)
        c.setFillColorRGB(0.92, 0.92, 0.92)
        c.rect(col_x, table_top_y - header_h, column_w, header_h, fill=1, stroke=1)
        c.setFillColorRGB(0, 0, 0)

        # 헤더 텍스트
        headers = ["연번", "부서", "이름", "확인"]
        x_cursor = col_x
        for header, cw in zip(headers, col_widths):
            c.drawCentredString(x_cursor + cw / 2, table_top_y - header_h + 2 * mm, header)
            x_cursor += cw

        # 헤더 세로선
        x_cursor = col_x
        for cw in col_widths[:-1]:
            x_cursor += cw
            c.line(x_cursor, table_top_y, x_cursor, table_top_y - header_h)

        # 데이터 행
        c.setFont("KFont", 9)
        prev_dept = None
        for i, (dept, name) in enumerate(rows):
            row_top = table_top_y - header_h - row_h * i
            row_bottom = row_top - row_h

            # 셀 테두리
            c.rect(col_x, row_bottom, column_w, row_h, fill=0, stroke=1)
            # 세로선
            x_cursor = col_x
            for cw in col_widths[:-1]:
                x_cursor += cw
                c.line(x_cursor, row_top, x_cursor, row_bottom)

            # 연번
            num = start_num + i
            c.drawCentredString(col_x + col_widths[0] / 2, row_bottom + 2.5 * mm, str(num))

            # 부서 (같은 부서 연속이면 첫 행만)
            if dept != prev_dept:
                c.drawCentredString(
                    col_x + col_widths[0] + col_widths[1] / 2,
                    row_bottom + 2.5 * mm,
                    dept,
                )
            prev_dept = dept

            # 이름
            c.drawCentredString(
                col_x + col_widths[0] + col_widths[1] + col_widths[2] / 2,
                row_bottom + 2.5 * mm,
                name,
            )

            # 서명 (있으면)
            sig_png = signatures.get((dept, name))
            if sig_png:
                sig_x = col_x + col_widths[0] + col_widths[1] + col_widths[2]
                sig_y = row_bottom
                sig_w = col_widths[3]
                sig_h = row_h
                _draw_signature(c, sig_png, sig_x, sig_y, sig_w, sig_h)

        # 빈 행 (좌우 높이 맞추기)
        for i in range(len(rows), max_rows):
            row_top = table_top_y - header_h - row_h * i
            row_bottom = row_top - row_h
            c.rect(col_x, row_bottom, column_w, row_h, fill=0, stroke=1)
            x_cursor = col_x
            for cw in col_widths[:-1]:
                x_cursor += cw
                c.line(x_cursor, row_top, x_cursor, row_bottom)

    # 좌단
    draw_column(left_col_x, 1, left_half)
    # 우단
    draw_column(right_col_x, half + 1, right_half)

    # ── 하단 정보 ──
    footer_y = table_top_y - table_h - 12 * mm
    c.setFont("KFont", 10)
    c.drawString(left_col_x, footer_y, f"연수명: {training_name}")
    c.drawString(left_col_x, footer_y - 6 * mm, f"연수일자: {training_date}")

    # 서명 통계 (선택)
    signed_count = len(signatures)
    c.setFont("KFont", 9)
    c.drawRightString(
        page_w - margin_right,
        footer_y,
        f"참석: {signed_count}명 / 전체: {total}명",
    )

    c.showPage()
    c.save()
    return buf.getvalue()


# ───────────────────────────────────────────────────────
# 테스트
# ───────────────────────────────────────────────────────
if __name__ == "__main__":
    # 더미 데이터로 테스트
    teachers = [
        ("교장", "공명현"),
        ("교감", "김지숙"),
        ("교무기획", "고정은"),
        ("교무기획", "양경은"),
        ("교무기획", "박소영"),
        ("교무기획", "황다운"),
        ("교육연구", "이제우"),
        ("교육연구", "추지영"),
        ("교육과정", "고인기"),
        ("교육과정", "최효진"),
    ]

    # 더미 서명 이미지 (흰 배경에 검은 선)
    from PIL import Image, ImageDraw

    def make_dummy_sig(text):
        img = Image.new("RGB", (400, 100), "white")
        d = ImageDraw.Draw(img)
        d.line([(50, 50), (350, 60)], fill="black", width=3)
        d.text((150, 30), text, fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    signatures = {
        ("교장", "공명현"): make_dummy_sig("sign1"),
        ("교무기획", "고정은"): make_dummy_sig("sign2"),
        ("교육과정", "최효진"): make_dummy_sig("sign3"),
    }

    pdf_bytes = generate_attendance_pdf(
        training_name="AI 활용 교수학습 역량 강화 연수",
        training_date="2026. 4. 20.",
        teachers=teachers,
        signatures=signatures,
    )

    with open("/tmp/test_roster.pdf", "wb") as f:
        f.write(pdf_bytes)
    print(f"PDF 생성 완료: /tmp/test_roster.pdf ({len(pdf_bytes)} bytes)")
