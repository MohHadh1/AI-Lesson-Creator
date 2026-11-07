"""Run the sample lessons and check sources, reviews, and PDFs."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from pypdf import PdfReader

from app import _validate
from export import export_lesson_pdf
from lesson_generator import generate_lesson
from quality_control import review_lesson
from research import collect_research, iter_sources

LESSONS = [
    {
        "title": "HTTP Requests and Responses",
        "objective": (
            "Explain GET and POST methods and distinguish between "
            "2xx, 4xx, and 5xx response status categories."
        ),
        "file": "exports/http-requests-and-responses.pdf",
    },
    {
        "title": "Variables and Data Types in Python",
        "objective": (
            "Identify integers, floats, strings, and booleans, and explain "
            "how a variable name refers to a value."
        ),
        "file": "exports/python-variables-and-data-types.pdf",
    },
]


def fail(message: str) -> None:
    print("FAIL:", message)
    sys.exit(1)


def check_pdf(path: Path, lesson: dict, reviews: dict) -> None:
    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    for needle in (
        "QC Layer 1: Factual accuracy",
        "QC Layer 2: Instructional quality",
        "Factual accuracy:",
        "Instructional quality:",
        lesson["title"],
    ):
        if needle not in text:
            fail(f"{path.name} is missing {needle!r}")
    urls = set()
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            action = annot.get_object().get("/A") or {}
            uri = action.get("/URI")
            if uri:
                urls.add(str(uri))
    expected = {source["url"] for source in lesson.get("sources") or []}
    missing = expected - urls
    if missing:
        fail(f"{path.name} is missing source links: {sorted(missing)[:3]}")
    print(f"  pdf pages={len(reader.pages)} links={len(urls)} bytes={path.stat().st_size}")


def run_one(spec: dict) -> None:
    title = spec["title"]
    objective = spec["objective"]
    print(f"\n=== {title} ===", flush=True)
    research = collect_research(title, learning_objective=objective)
    print(
        f"  research web={len(research['web'])} books={len(research['books'])} "
        f"videos={len(research['youtube'])} errors={research['errors']}",
        flush=True,
    )
    lesson = generate_lesson(title, research, "introductory learners", 45, objective)
    source_urls = {item["url"] for item in iter_sources(research)}
    lesson_urls = {item["url"] for item in lesson.get("sources") or []}
    if lesson_urls - source_urls:
        fail("lesson contains a source that research did not retrieve")
    if not research["retrieved"] and lesson_urls:
        fail("lesson cited sources even though research retrieved none")
    if not research["retrieved"] and "without retrieved sources" not in lesson["markdown"].lower():
        fail("failed research was not disclosed in the lesson")
    if research["retrieved"] and "without retrieved sources" in lesson["markdown"].lower():
        fail("lesson says sources were missing even though research returned some")
    print(f"  lesson sections={len(lesson['sections'])} sources={len(lesson_urls)}", flush=True)

    reviews = review_lesson(lesson, research, "introductory learners")
    factual = reviews.get("factual")
    teaching = reviews.get("instructional")
    if reviews["errors"]["factual"] or reviews["errors"]["instructional"]:
        fail(f"review errors: {reviews['errors']}")
    if not factual or not teaching:
        fail("one review did not return a report")
    if factual["system"] == teaching["system"]:
        fail("the two reviews used the same system id")
    if factual["layer"] == teaching["layer"]:
        fail("the two reviews used the same layer title")
    if "unsupported_claims" not in factual or "areas" not in teaching:
        fail("the two reports do not have separate shapes")
    if factual["summary"] == teaching["summary"]:
        fail("the two summaries are identical")
    if not factual["summary"].startswith("Factual accuracy:"):
        fail("factual summary is missing its label")
    if not teaching["summary"].startswith("Instructional quality:"):
        fail("instructional summary is missing its label")
    print("  factual:", factual["summary"][:220])
    print("  teaching:", teaching["summary"][:220])
    print(f"  passed factual={factual['passed']} instructional={teaching['passed']}")

    path = export_lesson_pdf(lesson, spec["file"], reviews=reviews, edited_markdown=lesson["markdown"])
    check_pdf(path, lesson, reviews)


def main() -> None:
    if _validate("  ", "Explain GET") == "":
        fail("blank title was accepted")
    if _validate("HTTP", "   ") == "":
        fail("blank objective was accepted")
    if _validate("HTTP", "Explain GET") != "":
        fail("valid input was rejected")
    print("input validation ok", flush=True)
    try:
        for spec in LESSONS:
            run_one(spec)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
