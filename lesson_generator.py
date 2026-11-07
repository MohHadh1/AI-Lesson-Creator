"""Turn research notes into a structured 45-minute lesson.

The generator reads the research bundle and writes the lesson. It does
not run quality control. Sources in the finished lesson are copied from
the research bundle, so a failed search cannot become a fake citation.
"""

from __future__ import annotations

from gemini_client import generate_json
from errors import LessonCreatorError
from research import format_research_for_prompt, iter_sources

_SYSTEM = """
You are an instructor who writes classroom lessons.
Write a lesson an instructor can teach in the requested number of minutes.
Use clear explanations, concrete examples, practical applications, a short
activity, and knowledge checks with answers for the instructor.

Ground factual statements in the research notes when research succeeded.
Mention a source only by its number, such as (source 2). Never invent a
title, author, book, or URL. If the research status says the search failed,
do not claim that you researched the topic and do not cite sources.

Reply with one JSON object and no markdown fence. Use this shape:
{
  "overview": "two or three sentences",
  "schedule": [{"minutes": 5, "segment": "what happens in this block"}],
  "sections": [
    {
      "heading": "section title",
      "minutes": 10,
      "explanation": "teaching explanation",
      "examples": ["a concrete example"],
      "practical_applications": ["where a learner uses this"]
    }
  ],
  "activities": [
    {
      "title": "activity name",
      "minutes": 8,
      "instructions": "what the instructor asks learners to do"
    }
  ],
  "knowledge_checks": [
    {"question": "a check for understanding", "answer": "the answer the instructor can use"}
  ]
}

Rules for the JSON:
- schedule minutes must add up to the lesson length.
- Include at least two sections, one activity, and three knowledge checks.
- Section minutes plus activity minutes should fit inside the lesson length.
- examples and practical_applications are lists of strings.
- Do not include a sources array. The application attaches retrieved URLs.
""".strip()


def _text(value: object) -> str:
    """Return a clean string, or an empty string for missing values."""
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value: object) -> list[str]:
    """Accept a string or a list of strings from the model."""
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for entry in value:
        if isinstance(entry, str) and _text(entry):
            items.append(_text(entry))
        elif isinstance(entry, dict):
            for key in ("text", "example", "description", "application", "item"):
                if _text(entry.get(key)):
                    items.append(_text(entry.get(key)))
                    break
    return items


def _minutes(value: object, default: int) -> int:
    """Read a positive minute count, falling back when the model omits it."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number


def _normalize_lesson(raw: dict, title: str, objective: str, audience: str, duration: int) -> dict:
    """Turn the model's JSON into the lesson dict the rest of the app uses."""
    sections = []
    for entry in raw.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        explanation = _text(entry.get("explanation") or entry.get("body"))
        heading = _text(entry.get("heading") or entry.get("title"))
        if not explanation or not heading:
            continue
        sections.append(
            {
                "heading": heading,
                "minutes": _minutes(entry.get("minutes"), 8),
                "explanation": explanation,
                "examples": _string_list(entry.get("examples")),
                "practical_applications": _string_list(
                    entry.get("practical_applications") or entry.get("applications")
                ),
            }
        )

    activities = []
    for entry in raw.get("activities") or []:
        if isinstance(entry, str) and _text(entry):
            activities.append({"title": "Activity", "minutes": 8, "instructions": _text(entry)})
            continue
        if not isinstance(entry, dict):
            continue
        instructions = _text(entry.get("instructions") or entry.get("body") or entry.get("description"))
        if not instructions:
            continue
        activities.append(
            {
                "title": _text(entry.get("title")) or "Activity",
                "minutes": _minutes(entry.get("minutes"), 8),
                "instructions": instructions,
            }
        )

    checks = []
    for entry in raw.get("knowledge_checks") or raw.get("checks") or []:
        if not isinstance(entry, dict):
            continue
        question = _text(entry.get("question") or entry.get("prompt"))
        answer = _text(entry.get("answer") or entry.get("response"))
        if question and answer:
            checks.append({"question": question, "answer": answer})

    schedule = []
    for entry in raw.get("schedule") or raw.get("timing") or []:
        if not isinstance(entry, dict):
            continue
        segment = _text(entry.get("segment") or entry.get("activity") or entry.get("label"))
        if not segment:
            continue
        schedule.append({"minutes": _minutes(entry.get("minutes"), 5), "segment": segment})

    if len(sections) < 2:
        raise LessonCreatorError(
            "The model returned a lesson without enough explanations. Retry lesson generation."
        )
    if not activities:
        raise LessonCreatorError(
            "The model returned a lesson without an activity. Retry lesson generation."
        )
    if len(checks) < 2:
        raise LessonCreatorError(
            "The model returned a lesson without knowledge checks. Retry lesson generation."
        )

    overview = _text(raw.get("overview") or raw.get("summary"))
    if not overview:
        overview = f"This lesson teaches {title.lower()} in {duration} minutes."

    return {
        "title": title,
        "learning_objective": objective,
        "audience": audience,
        "duration_minutes": duration,
        "overview": overview,
        "schedule": schedule,
        "sections": sections,
        "activities": activities,
        "knowledge_checks": checks,
    }


def lesson_to_markdown(lesson: dict) -> str:
    """Render a lesson dict as markdown the editor, preview, and PDF can share."""
    lines: list[str] = [
        f"# {lesson.get('title') or 'Untitled lesson'}",
        "",
        f"**Learning objective:** {lesson.get('learning_objective') or 'Not set'}",
        "",
        f"**Audience:** {lesson.get('audience') or 'Not set'}",
        "",
        f"**Duration:** {lesson.get('duration_minutes') or 45} minutes",
        "",
    ]

    status = _text(lesson.get("research_status"))
    if status:
        lines.extend([status, ""])

    lines.extend(["## Overview", "", lesson.get("overview") or "", ""])

    schedule = lesson.get("schedule") or []
    if schedule:
        lines.append("## Timing")
        lines.append("")
        for block in schedule:
            lines.append(f"- {block.get('minutes', '?')} min — {block.get('segment', '')}")
        total = sum(int(block.get("minutes") or 0) for block in schedule)
        if total and total != int(lesson.get("duration_minutes") or 45):
            lines.append("")
            lines.append(
                f"The planned blocks add up to {total} minutes. "
                f"Adjust them so the class fits in {lesson.get('duration_minutes') or 45} minutes."
            )
        lines.append("")

    lines.extend(["## Explanations", ""])
    for section in lesson.get("sections") or []:
        lines.append(f"### {section.get('heading')} ({section.get('minutes')} min)")
        lines.append("")
        lines.append(section.get("explanation") or "")
        lines.append("")
        examples = section.get("examples") or []
        if examples:
            lines.append("**Examples**")
            lines.append("")
            for example in examples:
                lines.append(f"- {example}")
            lines.append("")
        applications = section.get("practical_applications") or []
        if applications:
            lines.append("**Practical applications**")
            lines.append("")
            for application in applications:
                lines.append(f"- {application}")
            lines.append("")

    lines.extend(["## Activities", ""])
    for activity in lesson.get("activities") or []:
        lines.append(f"### {activity.get('title')} ({activity.get('minutes')} min)")
        lines.append("")
        lines.append(activity.get("instructions") or "")
        lines.append("")

    lines.extend(["## Knowledge checks", ""])
    lines.append("Answers are for the instructor.")
    lines.append("")
    for index, check in enumerate(lesson.get("knowledge_checks") or [], start=1):
        lines.append(f"{index}. {check.get('question')}")
        lines.append("")
        lines.append(f"**Answer:** {check.get('answer')}")
        lines.append("")

    lines.extend(["## References", ""])
    sources = lesson.get("sources") or []
    if sources:
        lines.append("These are the pages, books, and videos that were retrieved. They are not invented citations.")
        lines.append("")
        for source in sources:
            kind = source.get("kind") or "Source"
            title = source.get("title") or source.get("url")
            url = source.get("url") or ""
            lines.append(f"- {kind}: [{title}]({url})")
        lines.append("")
    else:
        lines.append(
            "No sources were retrieved. This draft does not cite websites, books, or videos."
        )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def generate_lesson(
    topic: str,
    research: dict,
    audience: str = "introductory learners",
    duration_minutes: int = 45,
    learning_objective: str = "",
) -> dict:
    """Ask Gemini to draft one lesson from the research bundle.

    Args:
        topic: Lesson title.
        research: Bundle returned by ``research.collect_research``.
        audience: Who the lesson is for.
        duration_minutes: Target class length. The app uses 45.
        learning_objective: What learners should be able to do afterward.

    Returns:
        A lesson dict that includes markdown, retrieved sources, and an
        honest research status.

    Raises:
        LessonCreatorError: The model call failed or the draft was unusable.
    """
    title = topic.strip()
    objective = learning_objective.strip() or f"Explain {title} at an introductory level."
    learners = audience.strip() or "introductory learners"
    duration = duration_minutes if duration_minutes and duration_minutes > 0 else 45
    if not title:
        raise LessonCreatorError("Lesson generation needs a title.")
    if not isinstance(research, dict):
        raise LessonCreatorError("Lesson generation needs the research bundle.")

    sources = iter_sources(research)
    research_failed = not sources
    if research_failed:
        research_status = (
            "This draft was written without retrieved sources. "
            + " ".join(str(item) for item in (research.get("errors") or []))
        ).strip()
    elif research.get("errors"):
        research_status = (
            "Research was partial. Some searches failed, and only the retrieved sources are listed. "
            + " ".join(str(item) for item in research.get("errors"))
        )
    else:
        research_status = (
            f"Research retrieved {len(sources)} source"
            f"{'' if len(sources) == 1 else 's'} from websites, books, or videos."
        )

    user_prompt = f"""
Lesson title: {title}
Learning objective: {objective}
Audience: {learners}
Lesson length: {duration} minutes

Write the lesson so an instructor can finish it within {duration} minutes.
The title and learning objective above are fixed. Do not change them.

{format_research_for_prompt(research)}
""".strip()

    raw = generate_json(
        system=_SYSTEM,
        user=user_prompt,
        temperature=0.4,
        max_output_tokens=8192,
    )
    lesson = _normalize_lesson(raw, title, objective, learners, duration)
    if research_failed and "without retrieved sources" not in lesson["overview"].lower():
        lesson["overview"] = (
            "This draft was written without retrieved sources. " + lesson["overview"]
        )
    lesson["sources"] = sources
    lesson["research_status"] = research_status
    lesson["research_retrieved"] = not research_failed
    lesson["markdown"] = lesson_to_markdown(lesson)
    return lesson
