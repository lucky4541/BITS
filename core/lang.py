"""Language and script support for every part of the BITS tool.

One place for everything that depends on the language of the book:

  * structural vocabulary in ~40 languages - chapter / part / section /
    appendix / figure / table labels and the headings of references,
    notes, index, contents, preface, introduction, acknowledgments,
    glossary, abstract, keywords ... (KEYWORDS)
  * label patterns for every numbering style: "Chapter 3", "Глава 3",
    "Рис. 2.1", "3. fejezet" / "2. ábra" (number first), "第3章",
    "第三章", "图1-1", "제3장", Arabic-Indic / Devanagari / full-width
    digits, Roman numerals (label_regex)
  * script rules: which scripts have letter case, which are written
    without spaces between words (Chinese, Japanese, Thai ...), sentence
    end punctuation of every script, word tokens for the text-loss check
    (words / joiner / continues)
  * language detection for xml:lang (detect_language) and the OCR
    language for it (ocr_language)

Matching is case-insensitive (Unicode casefold) and ignores leading
numbering ("3.", "IV") and trailing punctuation of headings."""
import re
import unicodedata
from collections import Counter

# --------------------------------------------------------------- vocabulary
# category -> language -> terms (lower case; "." in a term is literal)
KEYWORDS = {
    "chapter": {
        "en": ["chapter", "chap."], "es": ["capítulo", "cap."], "pt": ["capítulo", "cap."], "fr": ["chapitre"],
        "it": ["capitolo", "cap."], "de": ["kapitel", "kap."], "nl": ["hoofdstuk"], "sv": ["kapitel"],
        "da": ["kapitel"], "no": ["kapittel"], "fi": ["luku"], "pl": ["rozdział"], "cs": ["kapitola"],
        "sk": ["kapitola"], "sl": ["poglavje"], "hr": ["poglavlje"], "sr": ["poglavlje", "поглавље"],
        "ro": ["capitolul", "capitol"], "hu": ["fejezet"], "tr": ["bölüm"], "el": ["κεφάλαιο"],
        "ru": ["глава"], "uk": ["розділ", "глава"], "bg": ["глава"], "ar": ["الفصل", "فصل"], "fa": ["فصل"],
        "he": ["פרק"], "hi": ["अध्याय"], "bn": ["অধ্যায়"], "id": ["bab"], "ms": ["bab"], "vi": ["chương"],
        "th": ["บทที่", "บท"], "ca": ["capítol"], "et": ["peatükk"], "lv": ["nodaļa"], "lt": ["skyrius"],
        "zh": ["章"], "ja": ["章"], "ko": ["장"],
    },
    "part": {
        "en": ["part"], "es": ["parte"], "pt": ["parte"], "fr": ["partie"], "it": ["parte"], "de": ["teil"],
        "nl": ["deel"], "sv": ["del"], "da": ["del"], "no": ["del"], "fi": ["osa"], "pl": ["część"],
        "cs": ["část"], "sk": ["časť"], "ro": ["partea"], "hu": ["rész"], "tr": ["kısım"], "el": ["μέρος"],
        "ru": ["часть"], "uk": ["частина"], "bg": ["част"], "ar": ["الجزء", "الباب"], "fa": ["بخش"],
        "he": ["חלק"], "hi": ["भाग"], "id": ["bagian"], "vi": ["phần"], "ca": ["part"],
        "zh": ["部", "编", "編", "篇"], "ja": ["部", "編"], "ko": ["부", "편"],
    },
    "section": {
        "en": ["section", "sect."], "es": ["sección"], "pt": ["seção", "secção"], "fr": ["section"],
        "it": ["sezione"], "de": ["abschnitt"], "nl": ["sectie", "afdeling"], "pl": ["sekcja"],
        "ru": ["раздел"], "uk": ["розділ"], "el": ["ενότητα"], "ar": ["القسم"], "he": ["סעיף"],
        "tr": ["kesim"], "zh": ["节", "節"], "ja": ["節"], "ko": ["절"], "ca": ["secció"],
    },
    "appendix": {
        "en": ["appendix", "app."], "es": ["apéndice", "anexo"], "pt": ["apêndice", "anexo"],
        "fr": ["annexe", "appendice"], "it": ["appendice", "allegato"], "de": ["anhang"],
        "nl": ["bijlage", "appendix"], "sv": ["bilaga"], "da": ["bilag"], "no": ["vedlegg"], "fi": ["liite"],
        "pl": ["załącznik", "dodatek"], "cs": ["příloha"], "sk": ["príloha"], "ro": ["anexa"],
        "hu": ["függelék"], "tr": ["ek"], "el": ["παράρτημα"], "ru": ["приложение"], "uk": ["додаток"],
        "bg": ["приложение"], "ar": ["ملحق"], "fa": ["پیوست"], "he": ["נספח"], "hi": ["परिशिष्ट"],
        "id": ["lampiran"], "vi": ["phụ lục"], "zh": ["附录", "附錄"], "ja": ["付録"], "ko": ["부록"],
        "ca": ["apèndix", "annex"],
    },
    "figure": {
        "en": ["figure", "fig.", "fig"], "es": ["figura", "fig."], "pt": ["figura", "fig."],
        "fr": ["figure", "fig."], "it": ["figura", "fig."], "de": ["abbildung", "abb."],
        "nl": ["figuur", "afbeelding", "fig."], "sv": ["figur", "bild"], "da": ["figur"], "no": ["figur"],
        "fi": ["kuva", "kuvio"], "pl": ["rysunek", "rys."], "cs": ["obrázek", "obr."], "sk": ["obrázok", "obr."],
        "sl": ["slika"], "hr": ["slika"], "sr": ["slika", "слика"], "ro": ["figura"], "hu": ["ábra"],
        "tr": ["şekil"], "el": ["εικόνα", "σχήμα", "σχ."], "ru": ["рисунок", "рис."], "uk": ["рисунок", "рис."],
        "bg": ["фигура", "фиг."], "ar": ["شكل", "الشكل"], "fa": ["شکل"], "he": ["איור"], "hi": ["चित्र"],
        "bn": ["চিত্র"], "id": ["gambar"], "ms": ["rajah"], "vi": ["hình"], "th": ["รูปที่", "ภาพที่"],
        "ca": ["figura"], "zh": ["图", "圖"], "ja": ["図"], "ko": ["그림"],
    },
    "table": {
        "en": ["table", "tab."], "es": ["tabla", "cuadro"], "pt": ["tabela", "quadro"], "fr": ["tableau"],
        "it": ["tabella"], "de": ["tabelle", "tab."], "nl": ["tabel"], "sv": ["tabell"], "da": ["tabel"],
        "no": ["tabell"], "fi": ["taulukko"], "pl": ["tabela", "tab."], "cs": ["tabulka"], "sk": ["tabuľka"],
        "sl": ["tabela"], "hr": ["tablica"], "sr": ["tabela", "табела"], "ro": ["tabelul", "tabel"],
        "hu": ["táblázat"], "tr": ["tablo", "çizelge"], "el": ["πίνακας"], "ru": ["таблица", "табл."],
        "uk": ["таблиця", "табл."], "bg": ["таблица"], "ar": ["جدول", "الجدول"], "fa": ["جدول"],
        "he": ["טבלה"], "hi": ["तालिका"], "bn": ["সারণি"], "id": ["tabel"], "ms": ["jadual"], "vi": ["bảng"],
        "th": ["ตารางที่"], "ca": ["taula"], "zh": ["表"], "ja": ["表"], "ko": ["표"],
    },
    "references": {
        "en": ["references", "reference list", "bibliography", "select bibliography", "selected bibliography",
               "works cited", "works consulted", "literature cited", "sources", "further reading",
               "suggested reading", "suggested readings", "recommended reading", "key references",
               "sources and further reading", "bibliographical notes", "reference"],
        "es": ["referencias", "referencias bibliográficas", "referencias clave", "bibliografía",
               "lecturas recomendadas", "lecturas sugeridas", "fuentes", "lectura recomendada"],
        "pt": ["referências", "referências bibliográficas", "bibliografia", "leituras recomendadas", "fontes"],
        "fr": ["références", "références bibliographiques", "bibliographie", "sources", "pour en savoir plus"],
        "it": ["bibliografia", "riferimenti", "riferimenti bibliografici", "letture consigliate", "fonti"],
        "de": ["literatur", "literaturverzeichnis", "literaturhinweise", "quellen", "quellenverzeichnis",
               "bibliographie", "bibliografie", "weiterführende literatur"],
        "nl": ["literatuur", "literatuurlijst", "referenties", "bibliografie", "bronnen"],
        "sv": ["referenser", "litteratur", "källor", "litteraturförteckning"],
        "da": ["referencer", "litteratur", "kilder", "litteraturliste"],
        "no": ["referanser", "litteratur", "kilder", "litteraturliste"],
        "fi": ["lähteet", "kirjallisuus", "lähdeluettelo"], "pl": ["bibliografia", "piśmiennictwo", "literatura"],
        "cs": ["literatura", "použitá literatura", "reference", "seznam literatury"],
        "sk": ["literatúra", "použitá literatúra"], "ro": ["bibliografie", "referințe"],
        "hu": ["irodalom", "irodalomjegyzék", "hivatkozások", "felhasznált irodalom"],
        "tr": ["kaynaklar", "kaynakça"], "el": ["βιβλιογραφία", "αναφορές"],
        "ru": ["литература", "список литературы", "библиография", "источники"],
        "uk": ["література", "список літератури", "джерела"], "bg": ["литература", "библиография"],
        "ar": ["المراجع", "المصادر", "قائمة المراجع"], "fa": ["منابع", "مراجع", "کتابنامه"],
        "he": ["ביבליוגרפיה", "מקורות", "רשימת מקורות"], "hi": ["संदर्भ", "सन्दर्भ", "संदर्भ ग्रंथ सूची"],
        "id": ["daftar pustaka", "referensi"], "ms": ["rujukan", "bibliografi"], "vi": ["tài liệu tham khảo"],
        "th": ["บรรณานุกรม", "เอกสารอ้างอิง"], "ca": ["referències", "bibliografia"],
        "zh": ["参考文献", "參考文獻", "参考书目"], "ja": ["参考文献", "引用文献"], "ko": ["참고문헌", "참고 문헌"],
    },
    "notes": {
        "en": ["notes", "endnotes", "end notes", "chapter notes", "footnotes"], "es": ["notas"], "pt": ["notas"],
        "fr": ["notes"], "it": ["note"], "de": ["anmerkungen", "endnoten", "fußnoten"],
        "nl": ["noten", "aantekeningen"], "sv": ["noter"], "da": ["noter"], "no": ["noter"], "fi": ["viitteet"],
        "pl": ["przypisy"], "cs": ["poznámky"], "ro": ["note"], "hu": ["jegyzetek"], "tr": ["notlar"],
        "el": ["σημειώσεις"], "ru": ["примечания"], "uk": ["примітки"], "bg": ["бележки"],
        "ar": ["الهوامش", "ملاحظات"], "fa": ["یادداشت‌ها", "یادداشتها"], "he": ["הערות"], "hi": ["टिप्पणियाँ"],
        "id": ["catatan"], "vi": ["chú thích"], "ca": ["notes"], "zh": ["注释", "註釋", "注"], "ja": ["注", "注記"],
        "ko": ["주", "미주"],
    },
    "index": {
        "en": ["index", "subject index", "name index", "general index"],
        "es": ["índice analítico", "índice alfabético", "índice de materias", "índice temático"],
        "pt": ["índice remissivo"], "fr": ["index"], "it": ["indice analitico", "indice dei nomi"],
        "de": ["register", "index", "sachregister", "stichwortverzeichnis", "personenregister"],
        "nl": ["register", "index", "trefwoordenregister"], "sv": ["register", "sakregister"],
        "pl": ["indeks", "skorowidz"], "cs": ["rejstřík"], "ru": ["указатель", "предметный указатель"],
        "uk": ["покажчик", "предметний покажчик"], "el": ["ευρετήριο"], "ar": ["الفهرس", "فهرس"],
        "he": ["מפתח"], "tr": ["dizin"], "hu": ["tárgymutató", "mutató"], "zh": ["索引"], "ja": ["索引"],
        "ko": ["색인", "찾아보기"],
    },
    "contents": {
        "en": ["contents", "table of contents"], "es": ["índice", "contenido", "índice general"],
        "pt": ["sumário", "índice"], "fr": ["table des matières", "sommaire"], "it": ["indice", "sommario"],
        "de": ["inhalt", "inhaltsverzeichnis"], "nl": ["inhoud", "inhoudsopgave"], "sv": ["innehåll"],
        "da": ["indhold"], "no": ["innhold"], "fi": ["sisällys", "sisältö"], "pl": ["spis treści"],
        "cs": ["obsah"], "sk": ["obsah"], "ro": ["cuprins"], "hu": ["tartalom", "tartalomjegyzék"],
        "tr": ["içindekiler"], "el": ["περιεχόμενα"], "ru": ["содержание", "оглавление"], "uk": ["зміст"],
        "bg": ["съдържание"], "ar": ["المحتويات"], "fa": ["فهرست مطالب"], "he": ["תוכן העניינים"],
        "hi": ["विषय सूची"], "id": ["daftar isi"], "vi": ["mục lục"], "zh": ["目录", "目錄"], "ja": ["目次"],
        "ko": ["목차", "차례"],
    },
    "preface": {
        "en": ["preface"], "es": ["prefacio", "prólogo"], "pt": ["prefácio"], "fr": ["préface"],
        "it": ["prefazione"], "de": ["vorwort"], "nl": ["voorwoord"], "sv": ["förord"], "da": ["forord"],
        "no": ["forord"], "fi": ["esipuhe"], "pl": ["przedmowa"], "cs": ["předmluva"], "ro": ["prefață"],
        "hu": ["előszó"], "tr": ["önsöz"], "el": ["πρόλογος"], "ru": ["предисловие"], "uk": ["передмова"],
        "ar": ["تمهيد"], "he": ["הקדמה"], "zh": ["前言", "序言", "序"], "ja": ["まえがき", "序文"],
        "ko": ["머리말", "서문"],
    },
    "foreword": {
        "en": ["foreword"], "es": ["prólogo"], "pt": ["apresentação"], "fr": ["avant-propos"],
        "it": ["premessa"], "de": ["geleitwort"], "nl": ["woord vooraf"], "zh": ["序"], "ja": ["序"],
    },
    "introduction": {
        "en": ["introduction"], "es": ["introducción"], "pt": ["introdução"], "fr": ["introduction"],
        "it": ["introduzione"], "de": ["einleitung", "einführung"], "nl": ["inleiding"], "sv": ["inledning"],
        "da": ["indledning"], "no": ["innledning"], "fi": ["johdanto"], "pl": ["wstęp", "wprowadzenie"],
        "cs": ["úvod"], "sk": ["úvod"], "ro": ["introducere"], "hu": ["bevezetés"], "tr": ["giriş"],
        "el": ["εισαγωγή"], "ru": ["введение"], "uk": ["вступ"], "bg": ["въведение"], "ar": ["مقدمة"],
        "fa": ["مقدمه"], "he": ["מבוא"], "hi": ["परिचय", "भूमिका"], "id": ["pendahuluan"], "vi": ["giới thiệu"],
        "zh": ["引言", "导论", "導論", "绪论", "緒論"], "ja": ["はじめに", "序論"], "ko": ["서론"],
    },
    "ack": {
        "en": ["acknowledgments", "acknowledgements", "acknowledgment", "acknowledgement"],
        "es": ["agradecimientos"], "pt": ["agradecimentos"], "fr": ["remerciements"], "it": ["ringraziamenti"],
        "de": ["danksagung", "dank"], "nl": ["dankwoord"], "sv": ["tack"], "pl": ["podziękowania"],
        "cs": ["poděkování"], "ro": ["mulțumiri"], "hu": ["köszönetnyilvánítás"], "tr": ["teşekkür"],
        "el": ["ευχαριστίες"], "ru": ["благодарности"], "uk": ["подяки"], "ar": ["شكر وتقدير"],
        "he": ["תודות"], "zh": ["致谢", "致謝", "鸣谢"], "ja": ["謝辞"], "ko": ["감사의 글"],
    },
    "glossary": {
        "en": ["glossary"], "es": ["glosario"], "pt": ["glossário"], "fr": ["glossaire"], "it": ["glossario"],
        "de": ["glossar"], "nl": ["woordenlijst"], "pl": ["słowniczek"], "ru": ["глоссарий", "словарь терминов"],
        "tr": ["sözlük"], "ar": ["مسرد"], "he": ["מילון מונחים"], "zh": ["术语表", "術語表", "词汇表"],
        "ja": ["用語集"], "ko": ["용어집", "용어 해설"],
    },
    "dedication": {
        "en": ["dedication"], "es": ["dedicatoria"], "pt": ["dedicatória"], "fr": ["dédicace"],
        "it": ["dedica"], "de": ["widmung"],
    },
    "abstract": {
        "en": ["abstract", "summary"], "es": ["resumen"], "pt": ["resumo"], "fr": ["résumé"],
        "it": ["riassunto", "sommario"], "de": ["zusammenfassung", "kurzfassung"], "nl": ["samenvatting"],
        "pl": ["streszczenie"], "ru": ["аннотация", "резюме"], "uk": ["анотація"], "tr": ["özet"],
        "ar": ["ملخص"], "he": ["תקציר"], "zh": ["摘要"], "ja": ["要旨", "抄録", "要約"], "ko": ["초록", "요약"],
    },
    "keywords": {
        "en": ["keywords", "key words"], "es": ["palabras clave"], "pt": ["palavras-chave"],
        "fr": ["mots-clés", "mots clés"], "it": ["parole chiave"], "de": ["schlüsselwörter", "schlagwörter"],
        "nl": ["trefwoorden"], "pl": ["słowa kluczowe"], "ru": ["ключевые слова"], "uk": ["ключові слова"],
        "tr": ["anahtar kelimeler"], "ar": ["الكلمات المفتاحية"], "zh": ["关键词", "關鍵詞"], "ja": ["キーワード"],
        "ko": ["주제어", "핵심어"],
    },
}

# words joining author names ("A and B"), lower case, matched as whole words
NAME_JOINERS = ["and", "y", "e", "et", "und", "ed", "en", "och", "og", "ja", "i", "a", "és", "ve", "și",
                "и", "і", "та", "και", "dan", "và", "و"]
CJK_NAME_SEPARATORS = "、，,；;和与與及"
ET_AL = ["et al.", "et al", "y cols.", "y col.", "y otros", "et coll.", "et collab.", "u. a.", "u.a.", "e coll.",
         "e col.", "e cols.", "en anderen", "m.fl.", "i in.", "a kol.", "и др.", "та ін.", "και συν.",
         "وآخرون", "等", "ほか", "他", "외"]
BYLINE_PREFIXES = ["by", "edited by", "por", "par", "von", "di", "da", "door", "av", "przez", "автор"]
SEE_ALSO = ["see also", "véase también", "ver também", "voir aussi", "vedi anche", "siehe auch", "zie ook",
            "zob. też", "см. также", "参见", "參見"]
SEE = ["see", "véase", "ver", "voir", "vedi", "siehe", "zie", "zob.", "см.", "见", "見"]
BLANK_PAGE = ["intentionally left blank", "deliberately left blank", "left blank intentionally",
              "página en blanco", "dejado en blanco", "laissée blanche", "page blanche",
              "absichtlich leer", "leerseite", "intenzionalmente bianca", "lasciata bianca",
              "em branco", "bewust leeg", "celowo pozostawiona pusta", "намеренно оставлена пустой"]

# ------------------------------------------------------------------ scripts
SENTENCE_END = ".!?…。！？‼⁇⁈⁉؟۔।॥։።፧"
CLOSERS = "\"'’”»›)]}」』）】》〉〕'"
OPENERS = "\"'‘“«‹([{「『（【《〈〔¿¡"

_SCRIPT_RANGES = [
    (0x0370, 0x03FF, "Greek"), (0x1F00, 0x1FFF, "Greek"),
    (0x0400, 0x052F, "Cyrillic"), (0x2DE0, 0x2DFF, "Cyrillic"), (0xA640, 0xA69F, "Cyrillic"),
    (0x0530, 0x058F, "Armenian"), (0x0590, 0x05FF, "Hebrew"), (0xFB1D, 0xFB4F, "Hebrew"),
    (0x0600, 0x06FF, "Arabic"), (0x0750, 0x077F, "Arabic"), (0x08A0, 0x08FF, "Arabic"),
    (0xFB50, 0xFDFF, "Arabic"), (0xFE70, 0xFEFF, "Arabic"),
    (0x0900, 0x097F, "Devanagari"), (0x0980, 0x09FF, "Bengali"), (0x0A00, 0x0A7F, "Gurmukhi"),
    (0x0A80, 0x0AFF, "Gujarati"), (0x0B00, 0x0B7F, "Oriya"), (0x0B80, 0x0BFF, "Tamil"),
    (0x0C00, 0x0C7F, "Telugu"), (0x0C80, 0x0CFF, "Kannada"), (0x0D00, 0x0D7F, "Malayalam"),
    (0x0D80, 0x0DFF, "Sinhala"), (0x0E00, 0x0E7F, "Thai"), (0x0E80, 0x0EFF, "Lao"),
    (0x0F00, 0x0FFF, "Tibetan"), (0x1000, 0x109F, "Myanmar"), (0x10A0, 0x10FF, "Georgian"),
    (0x1200, 0x139F, "Ethiopic"), (0x1780, 0x17FF, "Khmer"),
    (0x3040, 0x309F, "Hiragana"), (0x30A0, 0x30FF, "Katakana"), (0x31F0, 0x31FF, "Katakana"),
    (0xFF66, 0xFF9F, "Katakana"),
    (0x1100, 0x11FF, "Hangul"), (0x3130, 0x318F, "Hangul"), (0xAC00, 0xD7AF, "Hangul"),
    (0x2E80, 0x2FDF, "Han"), (0x3400, 0x4DBF, "Han"), (0x4E00, 0x9FFF, "Han"), (0xF900, 0xFAFF, "Han"),
    (0x20000, 0x2FA1F, "Han"),
]
# scripts written without spaces between words: lines join with no space,
# and the text-loss check compares them character by character
NO_SPACE_SCRIPTS = {"Han", "Hiragana", "Katakana", "Thai", "Lao", "Khmer", "Myanmar", "Tibetan"}
RTL_SCRIPTS = {"Hebrew", "Arabic"}


def script_of(ch: str) -> str:
    """Script of one character ("Latin", "Cyrillic", "Han", ...); "" for
    digits, punctuation, spaces and symbols."""
    if not ch or not (ch.isalpha() or unicodedata.category(ch) in ("Mn", "Mc", "Lo")):
        return ""
    cp = ord(ch)
    if cp < 0x0250 or 0x1E00 <= cp <= 0x1EFF or 0x2C60 <= cp <= 0x2C7F or 0xA720 <= cp <= 0xA7FF \
            or 0xFF21 <= cp <= 0xFF5A:
        return "Latin" if ch.isalpha() else ""
    for lo, hi, name in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return name
    return "Other"


def dominant_script(text: str) -> str:
    c = Counter(s for s in map(script_of, text or "") if s)
    return c.most_common(1)[0][0] if c else ""


def is_no_space(ch: str) -> bool:
    return script_of(ch) in NO_SPACE_SCRIPTS


def is_rtl(text: str) -> bool:
    return dominant_script(text) in RTL_SCRIPTS


def _first_letter(text: str) -> str:
    for ch in text or "":
        if ch.isalpha():
            return ch
        if ch.isspace() or ch in OPENERS or unicodedata.category(ch).startswith("P"):
            continue
        return ""
    return ""


def _last_char(text: str) -> str:
    t = (text or "").rstrip()
    while t and t[-1] in CLOSERS:
        t = t[:-1].rstrip()
    return t[-1] if t else ""


def ends_sentence(text: str) -> bool:
    return _last_char(text) in SENTENCE_END


def starts_lowercase(text: str) -> bool:
    """First letter is a lower-case letter of a cased script (Latin incl.
    accents, Greek, Cyrillic, Armenian ...)."""
    ch = _first_letter(text)
    return bool(ch) and ch.islower()


def starts_caseless(text: str) -> bool:
    """First letter belongs to a script without letter case (Han, kana,
    Hangul, Arabic, Hebrew, Indic, Thai ...)."""
    ch = _first_letter(text)
    return bool(ch) and ch.lower() == ch.upper() and not ch.islower() and not ch.isupper()


# languages that capitalise every noun: a continued sentence may go on with a capital
NOUN_CAPITALISING = {"de", "lb"}


def continues(prev: str, nxt: str, lang: str = None) -> bool:
    """Does `nxt` continue the sentence `prev` broke off (column / page
    break)? Cased scripts: prev does not end a sentence and nxt starts
    lower case. Caseless scripts: prev does not end a sentence and stops
    on a letter (not a heading-like number), nxt starts with a letter."""
    if not prev or not nxt or ends_sentence(prev):
        return False
    if starts_lowercase(nxt):
        return True
    if (lang or "").split("-")[0].lower() in NOUN_CAPITALISING and _first_letter(nxt).isupper():
        last = _last_char(prev)
        return bool(last) and (last.islower() or last == ",")
    if starts_caseless(nxt):
        last = _last_char(prev)
        return bool(last) and (last.isalpha() or unicodedata.category(last).startswith("M")
                               or last in ",、，;；:：-‐")
    return False


def joiner(prev: str, nxt: str) -> str:
    """What goes between two joined lines: nothing between two characters
    of a script written without spaces (Chinese, Japanese, Thai ...),
    otherwise one space."""
    a, b = _last_char(prev), (nxt or "").lstrip()[:1]
    if a and b and (is_no_space(a) or a in "。、，；：！？「」『』（）") and (is_no_space(b) or b in "「『（"):
        return ""
    return " "


_TOKEN_SPLIT = re.compile(r"\s+")


def words(text: str) -> list:
    """Word tokens for text comparison: whitespace-separated words, with
    every character of a no-space script (Han, kana, Thai ...) its own
    token - "第3章 東京の歴史" -> ["第", "3", "章", "東", "京", "の", "歴", "史"]."""
    out = []
    for tok in _TOKEN_SPLIT.split(text or ""):
        if not tok:
            continue
        if not any(is_no_space(ch) for ch in tok):
            out.append(tok)
            continue
        buf = ""
        for ch in tok:
            if is_no_space(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
    return out


# ---------------------------------------------------- right-to-left lines
def _bidi_class(ch: str) -> str:
    """"R" strong right-to-left, "L" strong left-to-right, "N" digits
    (European numbers - laid out left-to-right inside RTL text), "M"
    combining mark, "W" neutral (space, punctuation)."""
    b = unicodedata.bidirectional(ch)
    if b in ("R", "AL"):
        return "R"
    if b == "L":
        return "L"
    if b in ("EN", "AN"):
        return "N" if b == "EN" else "R"
    if b == "NSM":
        return "M"
    return "W"


def logical_from_visual(clusters, char_of=lambda m: m):
    """clusters: [(x_center, [chars...]), ...] of ONE right-to-left line, in
    any order. Returns the chars in logical (reading) order: right to left,
    with runs of left-to-right text and European digits ("HTML", "2001",
    "3.5") kept left to right - the inverse of the Unicode bidi layout for
    an RTL paragraph, computed from the glyph positions on the page."""
    vis = sorted(clusters, key=lambda c: c[0])          # left -> right on the page
    seq = list(reversed(vis))                           # right -> left
    first = [char_of(c[1][0]) or " " for c in seq]
    cls = [_bidi_class(f[0]) for f in first]
    out, i = [], 0
    while i < len(seq):
        if cls[i] in ("L", "N"):
            j = i
            # extend over L/N and neutrals that sit between L/N characters
            while j + 1 < len(seq) and (cls[j + 1] in ("L", "N") or
                                        (cls[j + 1] == "W" and first[j + 1] != " " and j + 2 < len(seq)
                                         and cls[j + 2] in ("L", "N"))):
                j += 1
            out.extend(reversed(seq[i:j + 1]))
            i = j + 1
        else:
            out.append(seq[i])
            i += 1
    return [ch for _x, chars in out for ch in chars]


def fix_rtl_rawdict(raw: dict) -> dict:
    """PyMuPDF "rawdict" with every right-to-left line's characters (and
    spans) rebuilt in logical order from their positions on the page -
    MuPDF's own order is wrong when an Arabic / Hebrew / Persian line holds
    European digits or Latin words ("القلب1 الفصل" for "الفصل 1 تاريخ
    القلب"). Left-to-right lines are not touched."""
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = [(span, ch) for span in line.get("spans", []) for ch in span.get("chars", [])]
            if not chars:
                continue
            strong = Counter(_bidi_class(ch["c"]) for _s, ch in chars if ch.get("c"))
            if strong["R"] <= strong["L"] or not strong["R"]:
                continue
            # a base character with the combining marks that follow it
            clusters = []
            for span, ch in chars:
                c = ch.get("c", "")
                if clusters and c and _bidi_class(c) == "M":
                    clusters[-1][1].append((span, ch))
                    continue
                bb = ch.get("bbox") or (0, 0, 0, 0)
                clusters.append([(bb[0] + bb[2]) / 2.0, [(span, ch)]])
            ordered = logical_from_visual([(x, list(members)) for x, members in clusters],
                                          char_of=lambda m: m[1].get("c", ""))
            # regroup into spans of the same style, in the new order
            new_spans = []
            for span, ch in ordered:
                if new_spans and new_spans[-1][0] is span:
                    new_spans[-1][1].append(ch)
                else:
                    new_spans.append((span, [ch]))
            rebuilt = []
            for span, chs in new_spans:
                ns = dict(span)
                ns["chars"] = chs
                xs = [c["bbox"] for c in chs if c.get("bbox")]
                if xs:
                    ns["bbox"] = (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs),
                                  max(b[3] for b in xs))
                rebuilt.append(ns)
            line["spans"] = rebuilt
            line["dir"] = line.get("dir", (1, 0))
            line["rtl"] = True
    return raw


_PRESENTATION_RANGES = ((0xFB00, 0xFB06), (0xFB13, 0xFB17), (0xFB1D, 0xFB4F), (0xFB50, 0xFDFF),
                        (0xFE70, 0xFEFF))


def normalize_presentation_forms(text: str) -> str:
    """Arabic / Hebrew presentation forms and Latin / Armenian ligatures
    (glyph shapes some PDFs put in the text layer: "ﺍﻟﻔﺼﻞ", "ﬁ") -> the
    ordinary letters ("الفصل", "fi"). Nothing else is changed (a
    superscript "²" stays)."""
    if not text or not any(lo <= ord(c) <= hi for c in text for lo, hi in _PRESENTATION_RANGES):
        return text
    return "".join(unicodedata.normalize("NFKC", c) if any(lo <= ord(c) <= hi for lo, hi in _PRESENTATION_RANGES)
                   else c for c in text)


# ------------------------------------------------------------------ labels
# a label number: 3, 2.1, 1-2, 10–7, 3a, IV, iv, A, A.1, 三, 十二, ٣, ३, ３
# (case-sensitive inside, so a case-insensitive label match never reads a
# word like "did" as a Roman numeral; a bare letter only for appendices)
NUMBER = (r"(?:\d+(?:[.\-–‐]\d+)*(?-i:[a-z])?(?![^\W\d_])|(?-i:[IVXLCDM]+)(?:[.\-–]\d+)*(?![^\W\d_])"
          r"|(?-i:[A-Z])(?:[.\-–]?\d+)+(?![^\W\d_])|[一二三四五六七八九十百千零〇两兩]+)")
_CJK_NUMBER = r"(?:\d+(?:[.\-–‐]\d+)*|[一二三四五六七八九十百千零〇两兩]+)"
LETTER_NUMBER = r"(?-i:[A-Z])(?![^\W\d_])"
_CJK_FIGURE_WORDS = ("图", "圖", "図", "表")


def terms(*categories, langs=None):
    """All terms of the categories (optionally only some languages),
    longest first so "table of contents" wins over "table"."""
    out = set()
    for cat in categories:
        for lang, ts in KEYWORDS.get(cat, {}).items():
            if langs is None or lang in langs:
                out.update(ts)
    return sorted(out, key=len, reverse=True)


def _is_cjk_term(t):
    return any(script_of(c) in ("Han", "Hiragana", "Katakana", "Hangul") for c in t)


def _alt(ts):
    parts = []
    for t in ts:
        e = re.escape(t).replace(r"\ ", r"\s+")
        if not t.endswith(".") and t[-1:].isalpha() and not _is_cjk_term(t):
            e += r"\.?"            # "Fig" / "Fig."
        parts.append(e)
    return "|".join(parts)


def label_core(*categories) -> str:
    """Regex (no anchors, one group-free alternation) for a label of the
    given categories in any language / numbering style:
        word NUM          Chapter 3, Глава 3, Рис. 2.1, Abb. 4, 图1-1, 그림 3
        word qual NUM     Figura técnica 1.4.1
        NUM. word         3. fejezet, 2. ábra, 3. Bölüm, 4. luku
        第 NUM 章          第3章, 第三章, 第2部 / 제 NUM 장
    """
    ts = terms(*categories)
    alpha = [t for t in ts if not _is_cjk_term(t)]
    cjk = [t for t in ts if t not in alpha]
    word = _alt(alpha)
    num = NUMBER if "appendix" not in categories else rf"(?:{NUMBER}|{LETTER_NUMBER})"
    forms = [rf"(?:{word})(?:\s+[^\W\d_]{{3,}})?\s*{num}", rf"{NUMBER}\.?\s+(?:{word})(?![^\W\d_])"]
    if cjk:
        fig_like = [t for t in cjk if t in _CJK_FIGURE_WORDS or t in ("그림", "표")]
        circ = [t for t in cjk if t not in fig_like]
        if circ:
            forms.append(rf"(?:第|제)\s*{_CJK_NUMBER}\s*(?:{'|'.join(re.escape(t) for t in circ)})")
        if fig_like:
            forms.append(rf"(?:{'|'.join(re.escape(t) for t in fig_like)})\s*{_CJK_NUMBER}")
    return "(?:" + "|".join(forms) + ")"


_LABEL_CACHE = {}


def label_regex(*categories, anchored=True):
    key = (categories, anchored)
    if key not in _LABEL_CACHE:
        core = label_core(*categories)
        _LABEL_CACHE[key] = re.compile((r"^\s*(" if anchored else "(") + core + ")", re.IGNORECASE)
    return _LABEL_CACHE[key]


def match_label(text: str, *categories):
    """(label, rest) when text starts with such a label, else None."""
    m = label_regex(*categories).match(text or "")
    if not m:
        return None
    rest = (text[m.end():]).lstrip(" .:：-–—  \t")
    return m.group(1).strip(), rest


# ----------------------------------------------------------------- headings
_LEAD_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[ivxlcdm]+|[IVXLCDM]+)[.:)\s]+")
_TRAIL_RE = re.compile(r"[\s.:：。]+$")


def normalize_heading(text: str) -> str:
    t = re.sub(r"<[^>]+>", "", text or "")
    t = _TRAIL_RE.sub("", _LEAD_NUM_RE.sub("", t.strip()))
    return re.sub(r"\s+", " ", t).casefold()


_HEADING_INDEX = None


def heading_kind(text: str, categories=None):
    """Category of a structural heading ("references", "notes", "index",
    "contents", "preface", "introduction", "ack", "glossary", "abstract",
    "keywords", "dedication", "foreword") in any language, else None."""
    global _HEADING_INDEX
    if _HEADING_INDEX is None:
        _HEADING_INDEX = {}
        for cat in ("references", "notes", "index", "contents", "preface", "foreword", "introduction", "ack",
                    "glossary", "abstract", "keywords", "dedication"):
            for t in terms(cat):
                _HEADING_INDEX.setdefault(t.casefold(), cat)
    t = normalize_heading(text)
    if not t or len(t) > 60:
        return None
    cat = _HEADING_INDEX.get(t)
    if cat and (categories is None or cat in categories):
        return cat
    return None


def is_heading(text: str, category: str) -> bool:
    return heading_kind(text, (category,)) == category


def is_blank_page_notice(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "")).casefold().strip(" .!-–—")
    return 0 < len(t) < 90 and any(p in t for p in BLANK_PAGE)


# -------------------------------------------------------------------- names
def name_split_regex():
    joiners = "|".join(re.escape(j) for j in sorted(NAME_JOINERS, key=len, reverse=True))
    return re.compile(rf"\s*(?:,\s*(?:{joiners})\s+|\s(?:{joiners})\s|&|;)\s*")


def strip_byline(text: str) -> str:
    pref = "|".join(re.escape(p) for p in sorted(BYLINE_PREFIXES, key=len, reverse=True))
    return re.sub(rf"^\s*(?:{pref})\s+", "", text or "", flags=re.IGNORECASE)


# Unicode upper / lower case letter classes for regexes (Python's re has no \p{Lu})
_UP = "".join(sorted({chr(i) for i in range(0x41, 0x0530) if chr(i).isupper()} |
                     {chr(i) for i in range(0x1E00, 0x1FFF) if chr(i).isupper()}))
_LO = "".join(sorted({chr(i) for i in range(0x61, 0x0530) if chr(i).islower()} |
                     {chr(i) for i in range(0x1E00, 0x1FFF) if chr(i).islower()}))
UPPER = "[" + "".join(re.escape(c) for c in _UP) + "]"
LOWER = "[" + "".join(re.escape(c) for c in _LO) + "]"
SENTENCE_END_CLASS = "[" + re.escape(SENTENCE_END) + "]"
CLOSERS_CLASS = "[" + re.escape(CLOSERS) + "]"


# --------------------------------------------------------- language detect
_STOPWORDS = {
    "en": "the of and to in is that for with as are was on by this be it from",
    "es": "de la que el en los del las por una para con se su al es como más",
    "pt": "de que não uma os das dos com para é do da em ao se mais pela",
    "fr": "le la les des est une dans pour que qui du au sur pas avec ce sont",
    "it": "il della che di per una sono gli delle nel con non alla del dei le",
    "de": "der die und das ist nicht mit den von zu sich auf dem eine für des",
    "nl": "de het een van en is dat niet met voor op zijn in te die",
    "sv": "och att det som en är på för med av till inte den har",
    "da": "og at det er en til på med af for ikke den de som har",
    "no": "og det er som en til på med av for ikke den har de",
    "fi": "ja on ei että se oli kun mutta tai myös ovat joka",
    "pl": "się nie na że jest do to jak przez oraz od być są dla",
    "cs": "se na je že to jako pro jsou nebo by jeho ale",
    "sk": "sa na je že to ako pre sú alebo by jeho ale",
    "ro": "și de în la cu este pe care nu din o un pentru mai",
    "hu": "az és hogy nem egy is van meg csak mint már ez",
    "tr": "ve bir bu da için ile olarak de daha çok gibi olan",
    "id": "dan yang di untuk dengan dari ini itu dalam tidak pada",
    "vi": "và của là có các những được trong cho không một với",
    "ca": "el la i de que els les per amb una és del al",
    "hr": "je i u na se da su za od kao ili",
    "sl": "je in v na se da so za od kot ali",
}
_STOP_SETS = {k: set(v.split()) for k, v in _STOPWORDS.items()}


def detect_language(text: str, default: str = "en") -> str:
    """ISO 639-1 code of the main language of `text` (for xml:lang)."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    sample = text[:200000]
    counts = Counter(s for s in map(script_of, sample) if s)
    if not counts:
        return default
    script, _n = counts.most_common(1)[0]
    if counts.get("Hiragana", 0) + counts.get("Katakana", 0) > 0.05 * sum(counts.values()):
        return "ja"
    if script == "Han":
        return "zh"
    if script == "Hangul":
        return "ko"
    if script == "Cyrillic":
        if re.search(r"[іїєґІЇЄҐ]", sample):
            return "uk"
        if re.search(r"[ђћљњџјЂЋЉЊЏЈ]", sample):
            return "sr"
        if re.search(r"[ыэёЫЭЁ]", sample):
            return "ru"
        return "bg" if sample.count("ъ") > len(sample) / 400 else "ru"
    if script == "Arabic":
        if re.search(r"[ٹڈڑںے]", sample):
            return "ur"
        if re.search(r"[پچژگکی]", sample):
            return "fa"
        return "ar"
    simple = {"Greek": "el", "Hebrew": "he", "Devanagari": "hi", "Bengali": "bn", "Thai": "th", "Tamil": "ta",
              "Telugu": "te", "Gujarati": "gu", "Gurmukhi": "pa", "Kannada": "kn", "Malayalam": "ml",
              "Georgian": "ka", "Armenian": "hy", "Ethiopic": "am", "Khmer": "km", "Lao": "lo",
              "Myanmar": "my", "Sinhala": "si"}
    if script in simple:
        return simple[script]
    toks = [w.casefold().strip(".,;:()[]«»\"'¿¡!?") for w in sample.split()]
    tc = Counter(toks)
    scores = {lang: sum(tc[w] for w in stops) for lang, stops in _STOP_SETS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default


# ---------------------------------------------------------------------- OCR
# ISO 639-1 -> PaddleOCR language model name
_PADDLE = {"en": "en", "zh": "ch", "zh-hant": "chinese_cht", "ja": "japan", "ko": "korean", "fr": "fr",
           "de": "german", "es": "es", "pt": "pt", "it": "it", "ru": "ru", "uk": "uk", "be": "be", "bg": "bg",
           "ar": "ar", "fa": "fa", "ur": "ur", "hi": "hi", "mr": "mr", "ne": "ne", "ta": "ta", "te": "te",
           "kn": "ka", "ka": "ka", "nl": "nl", "no": "no", "sv": "sv", "da": "da", "fi": "fi", "pl": "pl",
           "cs": "cs", "sk": "sk", "sl": "sl", "hr": "hr", "sr": "rs_latin", "ro": "ro", "hu": "hu", "tr": "tr",
           "id": "id", "ms": "ms", "vi": "vi", "la": "la", "et": "et", "lt": "lt", "lv": "lv", "ga": "ga",
           "cy": "cy", "is": "is", "mt": "mt", "sq": "sq", "az": "az", "uz": "uz", "el": "el", "th": "th",
           "he": "he", "ca": "ca"}
_PADDLE_BY_SCRIPT = {"Latin": "latin", "Cyrillic": "cyrillic", "Arabic": "arabic", "Devanagari": "devanagari",
                     "Han": "ch", "Hangul": "korean", "Hiragana": "japan", "Katakana": "japan"}

LANGUAGES = {  # code -> English name, for the settings dropdowns
    "auto": "Detect automatically", "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "it": "Italian", "de": "German", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "pl": "Polish", "cs": "Czech", "sk": "Slovak", "sl": "Slovenian", "hr": "Croatian",
    "sr": "Serbian", "ro": "Romanian", "hu": "Hungarian", "tr": "Turkish", "el": "Greek", "ru": "Russian",
    "uk": "Ukrainian", "bg": "Bulgarian", "ar": "Arabic", "fa": "Persian", "ur": "Urdu", "he": "Hebrew",
    "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "ms": "Malay", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ca": "Catalan",
    "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian",
}


def ocr_language(lang: str, sample_text: str = "") -> str:
    """PaddleOCR model name for an ISO language code ("auto" -> from the
    sample text's script, else English)."""
    lang = (lang or "auto").lower()
    if lang in set(_PADDLE.values()) | set(_PADDLE_BY_SCRIPT.values()) and lang not in _PADDLE:
        return lang                      # already an engine model name ("ch", "japan", "latin" ...)
    if lang == "auto":
        lang = detect_language(sample_text, default="en") if sample_text else "en"
    if lang in _PADDLE:
        return _PADDLE[lang]
    return _PADDLE_BY_SCRIPT.get(dominant_script(sample_text), "en")


def resolve_language(setting: str, text: str) -> str:
    """xml:lang value: the setting, or detected from the text for "auto"."""
    setting = (setting or "auto").strip()
    if setting.lower() in ("", "auto"):
        return detect_language(text)
    return setting
