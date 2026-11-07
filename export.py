"""Save a lesson, its retrieved sources, and both QC reports as a PDF.

The PDF uses the instructor's edited markdown when the app provides it.
Retrieved sources are written from the lesson's source list, which comes
from search results rather than from the model.
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from quality_control import report_to_markdown

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONTS_READY = False


def _register_fonts() -> None:
    """Use Arial when Windows provides it so punctuation is reliable."""
    global _FONT, _FONT_BOLD, _FONTS_READY
    if _FONTS_READY:
        return
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        regular = Path(r"C:\Windows\Fonts\arial.ttf")
        bold = Path(r"C:\Windows\Fonts\arialbd.ttf")
        italic = Path(r"C:\Windows\Fonts\ariali.ttf")
        bold_italic = Path(r"C:\Windows\Fonts\arialbi.ttf")
        if not (regular.exists() and bold.exists()):
            return
        pdfmetrics.registerFont(TTFont("LessonArial", str(regular)))
        pdfmetrics.registerFont(TTFont("LessonArial-Bold", str(bold)))
        if italic.exists():
            pdfmetrics.registerFont(TTFont("LessonArial-Italic", str(italic)))
        if bold_italic.exists():
            pdfmetrics.registerFont(TTFont("LessonArial-BoldItalic", str(bold_italic)))
        pdfmetrics.registerFontFamily(
            "LessonArial",
            normal="LessonArial",
            bold="LessonArial-Bold",
            italic="LessonArial-Italic" if italic.exists() else "LessonArial",
            boldItalic="LessonArial-BoldItalic" if bold_italic.exists() else "LessonArial-Bold",
        )
        _FONT = "LessonArial"
        _FONT_BOLD = "LessonArial-Bold"
    except Exception:
        _FONT = "Helvetica"
        _FONT_BOLD = "Helvetica-Bold"
    _FONTS_READY = True


def _styles() -> dict[str, ParagraphStyle]:
    """Paragraph styles used by the lesson and the two audits."""
    ink = HexColor("#1f2933")
    muted = HexColor("#52606d")
    return {
        "h1": ParagraphStyle(
            "LessonH1",
            fontName=_FONT_BOLD,
            fontSize=16,
            leading=20,
            textColor=ink,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "LessonH2",
            fontName=_FONT_BOLD,
            fontSize=13,
            leading=16,
            textColor=HexColor("#0b3a5b"),
            spaceBefore=8,
            spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "LessonH3",
            fontName=_FONT_BOLD,
            fontSize=11,
            leading=14,
            textColor=ink,
            spaceBefore=6,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "LessonBody",
            fontName=_FONT,
            fontSize=10,
            leading=14,
            textColor=ink,
            alignment=TA_LEFT,
        ),
        "bullet": ParagraphStyle(
            "LessonBullet",
            fontName=_FONT,
            fontSize=10,
            leading=14,
            textColor=ink,
            leftIndent=14,
            bulletIndent=2,
        ),
        "note": ParagraphStyle(
            "LessonNote",
            fontName=_FONT,
            fontSize=10,
            leading=14,
            textColor=muted,
            leftIndent=8,
        ),
        "footer": ParagraphStyle(
            "LessonFooter",
            fontName=_FONT,
            fontSize=9,
            textColor=muted,
        ),
    }


def _xml(text: str) -> str:
    """Escape text so it is safe inside a ReportLab paragraph."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline(text: str) -> str:
    """Turn a single line of markdown into a ReportLab paragraph string."""
    links: list[tuple[str, str]] = []

    def store(label: str, url: str) -> str:
        links.append((label, url))
        return f"\x00{len(links) - 1}\x00"

    converted = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda match: store(match.group(1), match.group(2)),
        text,
    )
    converted = re.sub(
        r"(https?://[^\s<]+)",
        lambda match: store(match.group(1).rstrip(".,);"), match.group(1).rstrip(".,);")),
        converted,
    )
    converted = _xml(converted)
    converted = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", converted)
    converted = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", converted)

    def restore(match: re.Match) -> str:
        label, url = links[int(match.group(1))]
        safe_url = _xml(url)
        safe_label = _xml(label)
        return f'<link href="{safe_url}" color="blue">{safe_label}</link>'

    return re.sub(r"\x00(\d+)\x00", restore, converted)


def markdown_to_flowables(markdown: str, styles: dict[str, ParagraphStyle]) -> list:
    """Convert a small markdown subset into PDF flowables."""
    flow: list = []
    paragraph: list[str] = []

    def flush() -> None:
        if not paragraph:
            return
        body = " ".join(line.strip() for line in paragraph)
        flow.append(Paragraph(_inline(body), styles["body"]))
        flow.append(Spacer(1, 6))
        paragraph.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[level:].strip()
            style = styles["h1"] if level <= 1 else styles["h2"] if level == 2 else styles["h3"]
            flow.append(Paragraph(_inline(text), style))
            continue
        if stripped in {"---", "***"}:
            flush()
            flow.append(HRFlowable(width="100%", thickness=0.4, color=HexColor("#d9e2ec")))
            flow.append(Spacer(1, 6))
            continue
        if stripped.startswith(">"):
            flush()
            flow.append(Paragraph(_inline(stripped.lstrip("> ").strip()), styles["note"]))
            flow.append(Spacer(1, 4))
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
        if bullet or numbered:
            flush()
            item = bullet.group(1) if bullet else numbered.group(1)
            flow.append(Paragraph("• " + _inline(item), styles["bullet"]))
            continue
        paragraph.append(stripped)
    flush()
    return flow


def _footer(canvas, doc) -> None:
    """Draw a small footer. It never includes secrets."""
    canvas.saveState()
    canvas.setFillColor(HexColor("#5c6570"))
    canvas.setFont(_FONT, 8)
    canvas.drawString(doc.leftMargin, 0.42 * inch, "AI Lesson Creator")
    canvas.drawRightString(letter[0] - doc.rightMargin, 0.42 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _lesson_body(lesson: dict, edited_markdown: str | None) -> str:
    """Choose the lesson text and leave room for a separate source list."""
    if isinstance(edited_markdown, str) and edited_markdown.strip():
        body = edited_markdown.strip()
    else:
        body = str(lesson.get("markdown") or "").strip()
    if not body:
        title = lesson.get("title") or "Untitled lesson"
        body = f"# {title}\n\nNo lesson text is available."
    # The structured source list is added after the lesson so edits cannot
    # drop the retrieved URLs, and so the model cannot invent them.
    parts = re.split(r"\n## References\s*\n", body, maxsplit=1)
    return parts[0].rstrip()


def _source_markdown(lesson: dict) -> str:
    """Build the reference section from retrieved sources only."""
    sources = lesson.get("sources") or []
    lines = ["## Retrieved sources", ""]
    real_sources = [
        source
        for source in sources
        if isinstance(source, dict) and str(source.get("url") or "").startswith(("http://", "https://"))
    ]
    if not real_sources:
        status = str(lesson.get("research_status") or "").strip()
        lines.append("No sources were retrieved, so this lesson has no citations.")
        if status:
            lines.append("")
            lines.append(status)
        return "\n".join(lines)

    lines.append("Each link below was returned by search. None were added by the lesson model.")
    lines.append("")
    for source in real_sources:
        kind = source.get("kind") or "Source"
        title = source.get("title") or source.get("url")
        url = source.get("url")
        lines.append(f"- {kind}: [{title}]({url})")
    return "\n".join(lines)


def _review_sections(reviews: dict | None) -> str:
    """Markdown for the two audits, in a fixed order."""
    reviews = reviews or {}
    factual = reviews.get("factual") or reviews.get("source")
    instructional = reviews.get("instructional") or reviews.get("teaching")
    errors = reviews.get("errors") or {}
    factual_missing = errors.get("factual") or errors.get("source") or "The factual accuracy review has not been run."
    teaching_missing = (
        errors.get("instructional")
        or errors.get("teaching")
        or "The instructional quality review has not been run."
    )
    parts = [
        report_to_markdown(factual, f"# QC Layer 1: Factual accuracy\n\n{factual_missing}"),
        report_to_markdown(
            instructional,
            f"# QC Layer 2: Instructional quality\n\n{teaching_missing}",
        ),
    ]
    return "\n\n".join(parts)


def build_lesson_pdf(
    lesson: dict,
    reviews: dict | None = None,
    edited_markdown: str | None = None,
) -> bytes:
    """Build the PDF in memory.

    The file contains the lesson text, the retrieved source list, and both
    quality-control reports.
    """
    if not isinstance(lesson, dict):
        raise TypeError("lesson must be a dict.")

    _register_fonts()
    styles = _styles()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=str(lesson.get("title") or "Lesson"),
        author="AI Lesson Creator",
    )
    story: list = []
    story.extend(markdown_to_flowables(_lesson_body(lesson, edited_markdown), styles))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=HexColor("#0b3a5b")))
    story.append(Spacer(1, 8))
    story.extend(markdown_to_flowables(_source_markdown(lesson), styles))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=HexColor("#0b3a5b")))
    story.append(Spacer(1, 8))
    story.extend(markdown_to_flowables(_review_sections(reviews), styles))
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def export_lesson_pdf(
    lesson: dict,
    destination: str | Path,
    reviews: dict | None = None,
    edited_markdown: str | None = None,
) -> Path:
    """Write ``lesson`` to a PDF file.

    Args:
        lesson: Lesson dict from ``lesson_generator.generate_lesson``.
            Instructor edits can also be passed as ``edited_markdown``.
        destination: File path to create, such as ``"lesson.pdf"``.
        reviews: Dict with the factual and instructional reports.
        edited_markdown: Lesson text from the instructor's editor.

    Returns:
        The path of the written PDF.
    """
    path = Path(destination)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        build_lesson_pdf(lesson, reviews=reviews, edited_markdown=edited_markdown)
    )
    return path
