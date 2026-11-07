"""Two independent reviews of a generated lesson.

The reviews do different jobs and do not see each other's output:

    1. Factual accuracy — compare the lesson with the research notes.
    2. Instructional quality — review alignment, clarity, structure,
       examples, and activities for the stated audience.

``review_lesson`` only collects the two reports. It does not merge them
into a single opinion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from errors import LessonCreatorError, public_error_message
from gemini_client import generate_json
from lesson_generator import lesson_to_markdown
from research import format_research_for_prompt

_FACTUAL_SYSTEM = """
You are an independent fact checker. You did not write the lesson, and you
are not reviewing teaching style.

Compare the lesson with the research notes only.
- Unsupported claim: the lesson states it, but the notes do not support it.
- Factual error: the lesson contradicts the notes or a well-known fact that
  the notes also let you check. Do not bring in outside sources you cannot quote.
- Missing information: something the learning objective needs that the lesson
  never teaches. Use the notes when they contain that information.

If the research status says nothing was retrieved, say the lesson cannot be
verified against sources. Do not invent sources, and do not praise the lesson
for being researched.

Do not comment on tone, lesson timing, activity design, or clarity except
where a sentence is too unclear to check.

Reply with one JSON object:
{
  "summary": "start with the words Factual accuracy:",
  "unsupported_claims": [
    {"claim": "", "why_unsupported": "", "suggestion": ""}
  ],
  "factual_errors": [
    {"claim": "", "correction": "", "suggestion": ""}
  ],
  "missing_information": [
    {"gap": "", "importance": "high or medium or low", "suggestion": ""}
  ]
}

Use empty lists when you find nothing in that category. Do not copy the lesson.
""".strip()

_INSTRUCTIONAL_SYSTEM = """
You are an independent instructional designer. You did not write the lesson.
You are not a fact checker. You will not receive research notes. Do not say
whether claims are true, and do not request citations.

Judge only whether a learner can meet the learning objective in the stated
time:
- objective alignment
- clarity of explanations
- structure and timing
- examples and practical applications
- activities and knowledge checks

Rate each area as strong, partial, or weak.
A weak rating means a learner would struggle to meet the objective.
Recommendations must be specific changes an instructor can make.

Reply with one JSON object:
{
  "summary": "start with the words Instructional quality:",
  "objective_alignment": {"rating": "strong", "findings": "", "recommendation": ""},
  "clarity": {"rating": "strong", "findings": "", "recommendation": ""},
  "structure": {"rating": "strong", "findings": "", "recommendation": ""},
  "examples": {"rating": "strong", "findings": "", "recommendation": ""},
  "activities_and_checks": {"rating": "strong", "findings": "", "recommendation": ""}
}
""".strip()


def _lesson_text(lesson: dict) -> str:
    """Use the instructor's edited text when the app has supplied it."""
    edited = lesson.get("edited_markdown")
    if isinstance(edited, str) and edited.strip():
        return edited.strip()
    markdown = lesson.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return markdown.strip()
    return lesson_to_markdown(lesson)


def _items(value: object, limit: int = 10) -> list[dict]:
    """Keep only dict rows, and cap how many a report can contain."""
    if not isinstance(value, list):
        return []
    rows = [row for row in value if isinstance(row, dict)]
    return rows[:limit]


def _clean(value: object) -> str:
    return str(value or "").strip()


def _summary(raw: dict, label: str) -> str:
    """Use the model's summary, and add the layer label if it left it off.

    The prompt asks for a label. Some replies repeat that instruction
    instead of writing the label, so that phrase is removed.
    """
    summary = _clean(raw.get("summary"))
    echoed = "start with the words "
    if summary.lower().startswith(echoed):
        summary = summary[len(echoed) :].strip()
    if not summary:
        return f"{label}: the review did not include a summary."
    if not summary.lower().startswith(label.lower()):
        return f"{label}: {summary}"
    return summary


def _normalize_factual(raw: dict) -> dict:
    """Build the factual audit from the model's JSON."""
    unsupported = []
    for row in _items(raw.get("unsupported_claims")):
        claim = _clean(row.get("claim"))
        why = _clean(row.get("why_unsupported") or row.get("reason"))
        suggestion = _clean(row.get("suggestion") or row.get("improvement"))
        if claim or why:
            unsupported.append(
                {
                    "claim": claim or "Unspecified claim",
                    "why_unsupported": why or "The research notes do not support this statement.",
                    "suggestion": suggestion or "Remove the claim or tie it to a retrieved source.",
                }
            )

    errors = []
    for row in _items(raw.get("factual_errors")):
        claim = _clean(row.get("claim"))
        correction = _clean(row.get("correction"))
        suggestion = _clean(row.get("suggestion") or row.get("improvement"))
        if claim or correction:
            errors.append(
                {
                    "claim": claim or "Unspecified statement",
                    "correction": correction or "Check this statement against the research notes.",
                    "suggestion": suggestion or "Rewrite the statement so it matches the notes.",
                }
            )

    gaps = []
    for row in _items(raw.get("missing_information")):
        gap = _clean(row.get("gap") or row.get("missing"))
        suggestion = _clean(row.get("suggestion") or row.get("improvement"))
        importance = _clean(row.get("importance")).lower()
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        if gap:
            gaps.append(
                {
                    "gap": gap,
                    "importance": importance,
                    "suggestion": suggestion or "Add a short explanation that covers this gap.",
                }
            )

    summary = _summary(raw, "Factual accuracy")

    high_gaps = [gap for gap in gaps if gap["importance"] == "high"]
    passed = not unsupported and not errors and not high_gaps

    notes: list[str] = []
    for row in unsupported:
        notes.append(f"Unsupported claim: {row['claim']} Suggestion: {row['suggestion']}")
    for row in errors:
        notes.append(f"Factual error: {row['claim']} Suggestion: {row['suggestion']}")
    for row in gaps:
        notes.append(f"Missing ({row['importance']}): {row['gap']} Suggestion: {row['suggestion']}")
    if not notes:
        notes.append("No unsupported claims, factual errors, or missing information were identified.")

    return {
        "system": "source",
        "layer": "QC Layer 1: Factual accuracy",
        "passed": passed,
        "summary": summary,
        "unsupported_claims": unsupported,
        "factual_errors": errors,
        "missing_information": gaps,
        "notes": notes,
    }


def _rating_block(raw: object, label: str) -> dict:
    """Read one instructional rating. A missing rating is reported as partial."""
    if not isinstance(raw, dict):
        return {
            "area": label,
            "rating": "partial",
            "findings": "The review did not score this area.",
            "recommendation": f"Look again at {label.lower()} before teaching the lesson.",
        }
    rating = _clean(raw.get("rating")).lower()
    if rating not in {"strong", "partial", "weak"}:
        rating = "partial"
    findings = _clean(raw.get("findings") or raw.get("finding") or raw.get("notes"))
    recommendation = _clean(raw.get("recommendation") or raw.get("suggestion"))
    return {
        "area": label,
        "rating": rating,
        "findings": findings or "No detail was given for this area.",
        "recommendation": recommendation or "No change was suggested for this area.",
    }


def _normalize_instructional(raw: dict) -> dict:
    """Build the instructional audit from the model's JSON."""
    areas = [
        _rating_block(raw.get("objective_alignment"), "Objective alignment"),
        _rating_block(raw.get("clarity"), "Clarity"),
        _rating_block(raw.get("structure"), "Structure"),
        _rating_block(raw.get("examples"), "Examples and applications"),
        _rating_block(raw.get("activities_and_checks"), "Activities and knowledge checks"),
    ]
    summary = _summary(raw, "Instructional quality")

    passed = all(area["rating"] in {"strong", "partial"} for area in areas)
    notes = [
        f"{area['area']} ({area['rating']}): {area['findings']} Recommendation: {area['recommendation']}"
        for area in areas
    ]
    return {
        "system": "teaching",
        "layer": "QC Layer 2: Instructional quality",
        "passed": passed,
        "summary": summary,
        "areas": areas,
        "notes": notes,
    }


def run_source_check(lesson: dict, research: dict) -> dict:
    """Check the lesson against the research bundle.

    This reviewer looks at support and accuracy. It does not receive the
    teaching-check result.

    Args:
        lesson: Draft produced by ``lesson_generator.generate_lesson``.
        research: The same bundle that was given to the generator.

    Returns:
        A report dict with ``system`` set to ``"source"``.
    """
    if not isinstance(lesson, dict) or not lesson:
        raise LessonCreatorError("The factual review needs a generated lesson.")
    if not isinstance(research, dict):
        raise LessonCreatorError("The factual review needs the research bundle.")

    user_prompt = f"""
Review this lesson against the research notes. Do not review teaching style.

LESSON
{_lesson_text(lesson)}

RESEARCH NOTES
{format_research_for_prompt(research)}
""".strip()

    raw = generate_json(
        system=_FACTUAL_SYSTEM,
        user=user_prompt,
        temperature=0.1,
        max_output_tokens=4096,
    )
    return _normalize_factual(raw)


def run_teaching_check(lesson: dict, audience: str) -> dict:
    """Check whether the lesson is clear and usable for the audience.

    This reviewer does not receive the research bundle or the factual report.

    Args:
        lesson: Draft produced by ``lesson_generator.generate_lesson``.
        audience: Who the lesson is for.

    Returns:
        A report dict with ``system`` set to ``"teaching"``.
    """
    if not isinstance(lesson, dict) or not lesson:
        raise LessonCreatorError("The instructional review needs a generated lesson.")

    learners = audience.strip() or str(lesson.get("audience") or "introductory learners")
    objective = str(lesson.get("learning_objective") or "").strip() or "Not stated"
    duration = lesson.get("duration_minutes") or 45
    user_prompt = f"""
Review this lesson as an instructional designer.
Audience: {learners}
Learning objective: {objective}
Time available: {duration} minutes

You do not have research notes. Do not judge factual accuracy.

LESSON
{_lesson_text(lesson)}
""".strip()

    raw = generate_json(
        system=_INSTRUCTIONAL_SYSTEM,
        user=user_prompt,
        temperature=0.2,
        max_output_tokens=4096,
    )
    return _normalize_instructional(raw)


def review_lesson(lesson: dict, research: dict, audience: str) -> dict:
    """Run both checks independently and return them side by side.

    The two model calls start together. Neither call receives the other
    report. A failure in one check is stored in ``errors`` and does not
    erase a report the other check already produced.

    Args:
        lesson: Draft lesson to review.
        research: Research bundle used to write the lesson.
        audience: Intended learners.

    Returns:
        A dict with ``source`` / ``factual`` and ``teaching`` /
        ``instructional``. Failed checks are ``None`` and explained in
        ``errors``.
    """
    results: dict[str, dict | None] = {"source": None, "teaching": None}
    errors: dict[str, str] = {}

    def _guard(name: str, func):
        try:
            results[name] = func()
        except LessonCreatorError as exc:
            errors[name] = str(exc)
        except Exception as exc:
            errors[name] = public_error_message(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_guard, "source", lambda: run_source_check(lesson, research))
        pool.submit(_guard, "teaching", lambda: run_teaching_check(lesson, audience))

    source_report = results["source"]
    teaching_report = results["teaching"]
    return {
        "source": source_report,
        "teaching": teaching_report,
        "factual": source_report,
        "instructional": teaching_report,
        "errors": {
            "factual": errors.get("source", ""),
            "source": errors.get("source", ""),
            "instructional": errors.get("teaching", ""),
            "teaching": errors.get("teaching", ""),
        },
    }


def report_to_markdown(report: dict | None, missing_message: str = "") -> str:
    """Render one audit as markdown for the screen and the PDF."""
    if not report:
        return missing_message or "This quality-control report has not been run."

    lines = [
        f"# {report.get('layer') or 'Quality-control report'}",
        "",
        f"**Result:** {'Passed' if report.get('passed') else 'Needs changes'}",
        "",
        report.get("summary") or "",
        "",
    ]

    if report.get("system") == "source":
        lines.extend(["## Unsupported claims", ""])
        rows = report.get("unsupported_claims") or []
        if not rows:
            lines.append("None identified.")
            lines.append("")
        for row in rows:
            lines.append(f"- **Claim:** {row.get('claim')}")
            lines.append(f"  Why it is unsupported: {row.get('why_unsupported')}")
            lines.append(f"  Suggestion: {row.get('suggestion')}")
        lines.append("")

        lines.extend(["## Factual errors", ""])
        rows = report.get("factual_errors") or []
        if not rows:
            lines.append("None identified.")
            lines.append("")
        for row in rows:
            lines.append(f"- **Claim:** {row.get('claim')}")
            lines.append(f"  Correction: {row.get('correction')}")
            lines.append(f"  Suggestion: {row.get('suggestion')}")
        lines.append("")

        lines.extend(["## Missing information", ""])
        rows = report.get("missing_information") or []
        if not rows:
            lines.append("None identified.")
            lines.append("")
        for row in rows:
            lines.append(f"- **{row.get('importance', 'medium').title()}:** {row.get('gap')}")
            lines.append(f"  Suggestion: {row.get('suggestion')}")
        lines.append("")
        return "\n".join(lines).strip() + "\n"

    lines.extend(["## Area ratings", ""])
    for area in report.get("areas") or []:
        lines.append(f"### {area.get('area')} — {area.get('rating')}")
        lines.append("")
        lines.append(area.get("findings") or "")
        lines.append("")
        lines.append(f"**Recommendation:** {area.get('recommendation')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
