"""받은 파일에서 글자 뽑기: PDF·HWP·HWPX·DOCX·PPTX·XLSX·글자 파일. 저장하지 않고 메모리에서만."""

import io
import struct
import zipfile

import pytest

from app.collectors.documents import PARA_TEXT, UnreadableDocument, _hwp_section, extract_text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def zipped(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_docx_paragraphs():
    xml = f'<w:document {W}><w:body><w:p><w:r><w:t>1장 </w:t></w:r><w:r><w:t>연습문제</w:t></w:r></w:p><w:p><w:r><w:t>3번까지</w:t></w:r></w:p></w:body></w:document>'
    assert extract_text("과제.docx", zipped({"word/document.xml": xml})) == "1장 연습문제\n3번까지"


def test_pptx_slides_in_order():
    slide = lambda text: f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>'  # noqa: E731
    data = zipped({"ppt/slides/slide10.xml": slide("열 번째"), "ppt/slides/slide2.xml": slide("두 번째")})
    assert extract_text("ch05.pptx", data) == "[슬라이드 1] 두 번째\n\n[슬라이드 2] 열 번째"


def test_xlsx_cells_with_shared_strings():
    shared = '<sst xmlns="s"><si><t>이름</t></si><si><t>점수</t></si></sst>'
    sheet = '<worksheet xmlns="s"><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row><row><c t="inlineStr"><is><t>홍길동</t></is></c><c><v>95</v></c></row></sheetData></worksheet>'
    data = zipped({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})
    assert extract_text("성적.xlsx", data) == "[시트 1]\n이름 | 점수\n홍길동 | 95"


def test_hwpx_sections():
    section = '<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p><hp:run><hp:t>한글 문서입니다</hp:t></hp:run></hp:p></hs:sec>'
    assert extract_text("안내.hwpx", zipped({"Contents/section0.xml": section})) == "한글 문서입니다"


def test_hwp_body_records_skip_controls():
    """HWP 본문 문단: 8칸짜리 조종 문자(표·그림 자리)는 건너뛰고 글자만 남긴다."""
    text = "과제".encode("utf-16-le") + struct.pack("<8H", 11, 0, 0, 0, 0, 0, 0, 11) + "제출".encode("utf-16-le")
    text += struct.pack("<H", 13)
    record = struct.pack("<I", PARA_TEXT | (len(text) << 20)) + text
    other = struct.pack("<I", 66 | (4 << 20)) + b"\x00" * 4  # 글자가 아닌 레코드
    assert _hwp_section(other + record) == "과제제출"


def test_plain_text_in_either_encoding():
    assert extract_text("메모.txt", "안녕하세요".encode("cp949")) == "안녕하세요"
    assert extract_text("메모.txt", "안녕하세요".encode("utf-8")) == "안녕하세요"


def test_pdf_text():
    stream = b"BT /F1 12 Tf 20 100 Td (Hello PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    body += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    assert extract_text("자료.pdf", body) == "[1쪽] Hello PDF"


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("사진.jpg", b"\xff\xd8", "형식은 글자를 읽지 못합니다"),
        ("깨진.docx", b"not a zip", "파일을 열지 못했습니다"),
        ("빈.txt", b"   ", "글자가 없는 파일"),
    ],
)
def test_unreadable_files_say_why(name, data, message):
    with pytest.raises(UnreadableDocument, match=message):
        extract_text(name, data)


def test_long_text_is_cut():
    assert extract_text("긴.txt", ("가" * 10000).encode("utf-8"), limit=100).endswith("…")
