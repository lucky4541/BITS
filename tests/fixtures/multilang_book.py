"""Two-page books in several languages / scripts for the language tests.

Each book: page 1 = chapter heading in the language's own label style +
a paragraph that breaks off mid-sentence; page 2 = the rest of that
sentence + a reference-list heading in the language + two references.
build(path, code) writes the PDF; zone(zm, pdf, code) tags one zone per
line as a user would (BITS profile tags, a plain "Heading 2" for the
reference heading - the tool has to recognise it from its words)."""
import os

import fitz

_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FREESERIF = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"

BOOKS = {
    # code: (font, chapter, paragraph start, paragraph end, references heading, ref 1, ref 2)
    "ru": (_DEJAVU, "Глава 1 История сердца", "Сердце является главным органом кровообращения, и его",
           "работа изучается очень давно.", "Список литературы",
           "1. Иванов И. И. Сердце. М.: Наука, 2001.", "2. Петров П. П. Кровь. М.: Медицина, 1999."),
    "de": ("helv", "Kapitel 1 Geschichte des Herzens", "Das Herz ist das wichtigste Organ des Kreislaufs, und seine",
           "Arbeit wird seit langem erforscht.", "Literaturverzeichnis",
           "1. Müller J. Das Herz. Berlin: Springer, 2001.", "2. Schulz K. Blut. Wien: Böhlau, 1999."),
    "el": (_DEJAVU, "Κεφάλαιο 1 Ιστορία της καρδιάς", "Η καρδιά είναι το κύριο όργανο της κυκλοφορίας και η",
           "λειτουργία της μελετάται από παλιά.", "Βιβλιογραφία",
           "1. Παπαδόπουλος Γ. Η καρδιά. Αθήνα, 2001.", "2. Νικολάου Κ. Το αίμα. Αθήνα, 1999."),
    "ar": (_DEJAVU, "الفصل 1 تاريخ القلب", "القلب هو العضو الرئيسي في الدورة الدموية وقد",
           "درس منذ زمن طويل جدا.", "المراجع",
           "1. أحمد م. القلب. القاهرة، 2001.", "2. علي ح. الدم. بيروت، 1999."),
    "he": (_DEJAVU, "פרק 1 תולדות הלב", "הלב הוא האיבר המרכזי של מחזור הדם ואת",
           "תפקודו חוקרים זמן רב.", "מקורות",
           "1. כהן י. הלב. ירושלים, 2001.", "2. לוי ד. הדם. תל אביב, 1999."),
    "hi": (_FREESERIF, "अध्याय 1 हृदय का इतिहास", "हृदय रक्त परिसंचरण का मुख्य अंग है और इसका",
           "अध्ययन बहुत समय से हो रहा है।", "संदर्भ",
           "1. शर्मा आर. हृदय. दिल्ली, 2001.", "2. वर्मा एस. रक्त. मुंबई, 1999."),
    "zh": ("china-s", "第1章 心脏的历史", "心脏是血液循环的主要器官，它的",
           "功能已经被研究了很久。", "参考文献",
           "1. 张三. 心脏. 北京: 科学出版社, 2001.", "2. 李四. 血液. 上海: 人民出版社, 1999."),
    "ja": ("japan", "第1章 心臓の歴史", "心臓は血液循環の主要な器官であり、その",
           "働きは長く研究されてきた。", "参考文献",
           "1. 山田太郎. 心臓. 東京: 医学書院, 2001.", "2. 鈴木花子. 血液. 大阪: 南江堂, 1999."),
    "ko": ("korea", "제1장 심장의 역사", "심장은 혈액 순환의 주요 기관이며 그",
           "기능은 오랫동안 연구되어 왔다.", "참고문헌",
           "1. 김철수. 심장. 서울: 의학사, 2001.", "2. 이영희. 혈액. 부산: 출판사, 1999."),
}

RTL = {"ar", "he"}

# per page: (line index into the book tuple, BITS tag, attrs)
_PAGE1 = [(1, "h1", {"part_type": "chapter"}), (2, "p", {})]
_PAGE2 = [(3, "p", {}), (4, "h2", {}), (5, "reference", {}), (6, "reference", {})]


def available(code):
    font = BOOKS[code][0]
    return not font.startswith("/") or os.path.isfile(font)


def _visual_rtl(text):
    """A right-to-left line as it is laid out on the page (left to right):
    words in reverse order, letters of each RTL word reversed, numbers and
    Latin words as they are - what a typesetter's PDF holds."""
    from core.lang import _bidi_class
    import re
    out = []
    for w in reversed(text.split(" ")):
        if any(_bidi_class(c) == "R" for c in w):
            out.append(w[::-1])
        else:
            # "1." / "2001." in RTL text: the full stop resolves to the right-to-left
            # direction and is drawn on the LEFT of the number
            m = re.match(r"^(\d(?:[\d.]*\d)?)([.,:;]*)$", w)
            out.append(m.group(2)[::-1] + m.group(1) if m else w)
    return " ".join(out)


def _insert(page, y, text, size, font, rtl=False):
    kw = {"fontsize": size}
    if font.startswith("/"):
        kw.update(fontname="F0", fontfile=font)
        f = fitz.Font(fontfile=font)
    else:
        kw["fontname"] = font
        f = fitz.Font(font)
    if rtl:
        text = _visual_rtl(text)
        x = page.rect.width - 40 - f.text_length(text, size)       # right-aligned
    else:
        x = 40
    page.insert_text((x, y), text, **kw)


def build(path, code):
    book = BOOKS[code]
    font = book[0]
    doc = fitz.open()
    for spec, size in ((_PAGE1, (16, 10)), (_PAGE2, (10, 12, 9, 9))):
        page = doc.new_page(width=480, height=600)
        y = 70
        for k, (idx, _tag, _a) in enumerate(spec):
            fs = size[min(k, len(size) - 1)]
            _insert(page, y, book[idx], fs, font, rtl=code in RTL)
            y += fs * 2.6
    doc.save(path)
    doc.close()
    return path


def zone(zm, pdf, code):
    for pno, spec in ((1, _PAGE1), (2, _PAGE2)):
        page = pdf.get_page(pno)
        lines = sorted((l["bbox"] for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
                        if "".join(s["text"] for s in l["spans"]).strip()), key=lambda r: r[1])
        for (idx, tag, attrs), bb in zip(spec, lines):
            zm.add_zone(pno, tag, [bb[0] - 1, bb[1] - 1, bb[2] + 1, bb[3] + 1], attributes=dict(attrs),
                        auto_parent=False)
    return zm
