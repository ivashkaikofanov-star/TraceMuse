import re
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


ASSETS = Path(__file__).with_name("assets")
FONT = "TraceMuseSans"
BOLD = "TraceMuseSansBold"
FONT_LOCK = Lock()
ACCENT = colors.HexColor("#A94218")
TEXT = colors.HexColor("#262523")
MUTED = colors.HexColor("#56514E")
PAPER_LINE = colors.HexColor("#DDD7D1")
MAX_TEXT_CHARACTERS = 24_000
MAX_TEXT_LINES = 480
MAX_ARCHIVE_ROWS = 100
MAX_IMAGE_ROWS = 15
PLAIN_TEXT = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F\u202A-\u202E\u2066-\u2069\uD800-\uDFFF\uFFFE\uFFFF]")

STATUS = {
    "created": "Создана", "complete": "Завершена", "completed": "Завершена",
    "partial": "Проверка выполнена частично", "failed": "Ошибка проверки", "error": "Ошибка",
    "disabled": "Не включено", "not_configured": "Не настроено", "not_checked": "Не проверено",
    "ok": "Выполнено", "unknown": "Не установлено", "unavailable": "Недоступно",
    "matched": "Найдено совпадение", "no_matches": "Совпадений нет",
    "manual": "Требуется ручная проверка", "manual_review": "Требуется ручная проверка",
    "low": "Низкий", "medium": "Средний", "high": "Высокий",
    "limited": "Ограниченная", "local_only": "Локальная проверка",
    "no_match": "Совпадений нет", "local_snapshot": "Проверен локальный снимок реестра",
    "local_snapshot_match": "Совпадение в локальном снимке реестра",
    "local_snapshot_no_match": "Совпадений в локальном снимке нет",
    "catalog_declaration": "Заявление в каталоге", "unestablished": "Не установлена",
    "insufficient_data": "Недостаточно данных", "insufficient": "Недостаточно данных",
    "brand_visual": "Сопоставление со справочным логотипом",
    "brand_text": "Упоминание бренда в тексте",
    "identical_image": "Совпадение SHA-1",
    "visual_similarity": "Визуальное сходство",
    "embedded_url": "Ссылка из материала", "commons_link": "Ссылка на Wikimedia Commons из материала",
}
FIELD = {
    "id": "ID", "name": "Название", "title": "Название", "location": "Расположение",
    "sha256": "SHA-256", "sha1": "SHA-1", "phash": "Перцептивный хеш",
    "width": "Ширина", "height": "Высота", "format": "Формат", "size": "Размер, байт",
    "size_bytes": "Размер, байт", "hash_origin": "Способ получения хеша",
    "url": "Ссылка", "license_url": "Условия лицензии", "status": "Статус",
    "checked_at": "Дата проверки", "confidence": "Уверенность", "match": "Основание",
    "license": "Лицензия", "creator": "Автор", "rights_holder": "Правообладатель",
    "commercial_use": "Коммерческое использование", "modification": "Изменение",
    "derivatives": "Производные материалы", "attribution": "Атрибуция",
    "attribution_required": "Обязательная атрибуция", "restrictions": "Ограничения",
    "share_alike": "Лицензия на тех же условиях", "evidence": "Доказательство",
    "method": "Метод поиска", "query": "Поисковый запрос", "registry": "Реестр",
    "catalog_creation_date": "Дата по каталогу", "snapshot_date": "Дата снимка реестра",
    "entry": "Запись реестра", "reference": "Реквизиты записи",
    "evidence_id": "Код доказательства", "evidence_type": "Тип доказательства",
    "quote": "Точная цитата", "finding": "Наблюдение", "action": "Следующее действие",
    "author": "Автор", "description": "Описание", "keywords": "Ключевые слова",
    "created": "Дата создания", "modified": "Дата изменения", "pages": "Страницы",
    "slides": "Слайды", "encoding": "Кодировка", "source": "Источник",
    "logo_references": "Справочные логотипы", "brand": "Бренд", "part": "Номер фрагмента",
}


def _register_fonts() -> None:
    with FONT_LOCK:
        if FONT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(FONT, str(ASSETS / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont(BOLD, str(ASSETS / "DejaVuSans-Bold.ttf")))
            pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=BOLD)


def _plain(value: object) -> str:
    return PLAIN_TEXT.sub("", str(value)).replace("\t", "    ")


def _label(value: object) -> str:
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    if value is None:
        return "Не указано"
    if isinstance(value, str):
        return STATUS.get(value, value)
    return str(value)


def _link(value: str) -> str:
    text = _plain(value)
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme in ("https", "http") and parsed.hostname
            and not parsed.username and not parsed.password and not re.search(r"\s", text)
        ):
            return f'<link href={quoteattr(text)} color="#A94218">{escape(text)}</link>'
    except ValueError:
        pass
    return escape(text)


def render_pdf(audit: dict) -> bytes:
    _register_fonts()
    body = ParagraphStyle(
        "Body", fontName=FONT, fontSize=9.5, leading=15, textColor=TEXT, spaceAfter=6,
        alignment=TA_LEFT, splitLongWords=True, allowWidows=True, allowOrphans=True,
    )
    title = ParagraphStyle(
        "Title", parent=body, fontName=BOLD, fontSize=25, leading=32, spaceAfter=13,
    )
    heading = ParagraphStyle(
        "Heading", parent=body, fontName=BOLD, textColor=ACCENT, fontSize=13,
        leading=18, spaceBefore=14, spaceAfter=7, keepWithNext=True,
    )
    subheading = ParagraphStyle("Subheading", parent=body, fontName=BOLD, keepWithNext=True)
    quiet = ParagraphStyle("Quiet", parent=body, fontSize=8.5, leading=13, textColor=MUTED)
    story = []

    def paragraph(value: object, style: ParagraphStyle = body) -> None:
        text = escape(_plain(value)).replace("\r\n", "\n").replace("\r", "\n")
        if text.strip():
            story.append(Paragraph(text.replace("\n", "<br/>"), style))

    def fields(values: dict, omit: tuple[str, ...] = (), depth: int = 0) -> None:
        for key, value in values.items():
            if key in omit or value in (None, "", [], {}):
                continue
            label = escape(_plain(FIELD.get(key, key.replace("_", " "))))
            if isinstance(value, dict) and depth < 4:
                story.append(Paragraph(f"<b>{label}</b>", subheading))
                fields(value, depth=depth + 1)
            elif isinstance(value, list) and depth < 4:
                story.append(Paragraph(f"<b>{label}</b>", subheading))
                for item in value:
                    if isinstance(item, dict):
                        fields(item, depth=depth + 1)
                        story.append(Spacer(1, 4))
                    else:
                        paragraph(_label(item))
            else:
                rendered = _link(value) if isinstance(value, str) and value.startswith(("https://", "http://")) else escape(_plain(_label(value)))
                story.append(Paragraph(f"<b>{label}</b> · {rendered}", body))

    def findings(section: dict) -> None:
        paragraph(f"Статус · {_label(section.get('status'))}")
        for item in section.get("items", []):
            paragraph(item.get("title") or item.get("url") or "Запись", subheading)
            fields(item, omit=("title",))
            story.append(Spacer(1, 5))
        for warning in section.get("warnings", []):
            paragraph(warning, quiet)

    result = audit.get("result") or {}
    extraction = result.get("extraction") or {}
    assessment = result.get("assessment") or {}
    ai = result.get("ai") or {}

    paragraph("Отчёт о проверке", title)
    paragraph(audit.get("filename") or "Материал", subheading)
    fields({
        "id": audit.get("id"), "Дата проверки": audit.get("created_at"),
        "status": audit.get("status"), "format": extraction.get("format") or audit.get("format"),
        "size_bytes": audit.get("size_bytes"), "sha256": audit.get("sha256") or extraction.get("sha256"),
    })
    paragraph("Оценка риска", heading)
    paragraph(_label(assessment.get("level") or audit.get("risk_level")), subheading)
    paragraph(assessment.get("reason", ""))
    for key, label in (("confidence", "Уровень уверенности"), ("risks", "Обнаруженные риски")):
        if key in assessment:
            fields({label: assessment[key]})
    for key, label in (("recommendations", "Рекомендации"), ("limitations", "Ограничения проверки")):
        if assessment.get(key):
            paragraph(label, subheading)
            for item in assessment[key]:
                if isinstance(item, dict):
                    fields(item)
                else:
                    paragraph(f"• {item}")

    paragraph("Источники и лицензии", heading)
    findings(result.get("sources") or {})

    paragraph("Правовые статусы в России", heading)
    findings(result.get("legal") or {})

    paragraph("Пояснения DeepSeek", heading)
    paragraph(f"Статус · {_label(ai.get('status'))}")
    paragraph(ai.get("summary", ""))
    if ai.get("model"):
        paragraph(ai["model"], quiet)
    if ai.get("findings"):
        for item in ai["findings"]:
            fields(item)
            story.append(Spacer(1, 5))
    else:
        for observation in ai.get("observations", []):
            paragraph(observation)
    for warning in ai.get("warnings", []):
        paragraph(warning, quiet)

    paragraph("Извлечённые данные", heading)
    links = extraction.get("links", [])
    if links:
        paragraph("Ссылки", subheading)
        for url in links:
            story.append(Paragraph(_link(url), body))
    if metadata := extraction.get("metadata"):
        fields({"Метаданные": metadata})
    if images := extraction.get("images"):
        shown = sorted(images, key=lambda image: not image.get("logo_references"))[:MAX_IMAGE_ROWS]
        fields({"Изображения": shown})
        if len(images) > len(shown):
            paragraph(f"Показано изображений · {len(shown)} из {len(images)}. Весь извлечённый список доступен в JSON.", quiet)
    if warnings := extraction.get("warnings"):
        fields({"Ограничения извлечения": warnings})
    if entries := extraction.get("entries"):
        fields({"Содержимое архива": entries[:MAX_ARCHIVE_ROWS]})
        if len(entries) > MAX_ARCHIVE_ROWS:
            paragraph(f"Показано записей · {MAX_ARCHIVE_ROWS} из {len(entries)}. Полный список доступен в JSON.", quiet)
    if text := extraction.get("text"):
        paragraph("Извлечённый текст", subheading)
        lines = text[:MAX_TEXT_CHARACTERS].splitlines()
        for line in lines[:MAX_TEXT_LINES]:
            if not line.strip():
                story.append(Spacer(1, 4))
            for offset in range(0, len(line), 3000):
                paragraph(line[offset:offset + 3000])
        if len(text) > MAX_TEXT_CHARACTERS or len(lines) > MAX_TEXT_LINES:
            paragraph("Показан фрагмент. Весь извлечённый текст доступен в JSON.", quiet)
    if not any(extraction.get(key) for key in ("text", "links", "metadata", "images", "entries", "warnings")):
        paragraph("Извлечённые данные отсутствуют.", quiet)

    def footer(canvas, document) -> None:
        width, _ = A4
        canvas.saveState()
        canvas.setStrokeColor(PAPER_LINE)
        canvas.line(document.leftMargin, 40, width - document.rightMargin, 40)
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 8)
        canvas.drawString(document.leftMargin, 27, "TraceMuse · Предварительная проверка")
        canvas.drawRightString(width - document.rightMargin, 27, str(document.page))
        canvas.restoreState()

    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, title="TraceMuse — отчёт о проверке", author="TraceMuse",
        leftMargin=49, rightMargin=49, topMargin=48, bottomMargin=58,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
