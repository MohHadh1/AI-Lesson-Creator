"""AI Lesson Creator — Streamlit entry point.

Run the app with:
    streamlit run app.py

The pipeline lives in the other modules:

    1. research.py          Web pages, books, and YouTube videos.
    2. lesson_generator.py  A 45-minute lesson from that research.
    3. quality_control.py   Two separate reviews of the lesson.
    4. export.py            PDF with the lesson, sources, and both reviews.

API keys are read from the environment (see .env). This file never displays them.
"""

from __future__ import annotations

import os
import re

import streamlit as st
from dotenv import load_dotenv

from errors import LessonCreatorError, public_error_message
from export import build_lesson_pdf
from gemini_client import require_api_key
from lesson_generator import generate_lesson
from quality_control import report_to_markdown, review_lesson, run_source_check, run_teaching_check
from research import collect_research

load_dotenv()

LESSON_MINUTES = 45


def _key_is_set(name: str) -> bool:
    """Return True when an environment variable exists and is not blank."""
    return bool(os.getenv(name, "").strip())


def _init_state() -> None:
    """Create session values once so a retry can keep earlier work."""
    defaults = {
        "research": None,
        "research_error": "",
        "lesson": None,
        "generation_error": "",
        "factual_report": None,
        "factual_error": "",
        "factual_basis": "",
        "instructional_report": None,
        "instructional_error": "",
        "instructional_basis": "",
        "applied_markdown": "",
        "pending_markdown": "",
        "lesson_editor": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _validate(title: str, objective: str) -> str:
    """Return an error message when required fields are blank."""
    if not title.strip():
        return "Enter a lesson title before running this step."
    if not objective.strip():
        return "Enter a learning objective before running this step."
    return ""


def _has_edits() -> bool:
    """True when the editor holds text that generation did not last apply."""
    current = st.session_state.get("lesson_editor", "")
    applied = st.session_state.get("applied_markdown", "")
    return bool(str(current).strip()) and str(current).strip() != str(applied).strip()


def _stage_markdown(markdown: str, replace_edits: bool) -> None:
    """Put a new draft in the editor, or hold it when the instructor has edits."""
    if replace_edits or not _has_edits():
        st.session_state.lesson_editor = markdown
        st.session_state.applied_markdown = markdown
        st.session_state.pending_markdown = ""
        return
    st.session_state.pending_markdown = markdown


def _store_reviews(bundle: dict, basis_text: str) -> None:
    """Save successful reports and keep the previous report when a retry fails."""
    factual_error = (bundle.get("errors") or {}).get("factual") or ""
    teaching_error = (bundle.get("errors") or {}).get("instructional") or ""

    if bundle.get("factual"):
        st.session_state.factual_report = bundle["factual"]
        st.session_state.factual_error = ""
        st.session_state.factual_basis = basis_text
    elif factual_error:
        st.session_state.factual_error = factual_error
    else:
        st.session_state.factual_error = "The factual review did not return a report."

    if bundle.get("instructional"):
        st.session_state.instructional_report = bundle["instructional"]
        st.session_state.instructional_error = ""
        st.session_state.instructional_basis = basis_text
    elif teaching_error:
        st.session_state.instructional_error = teaching_error
    else:
        st.session_state.instructional_error = "The instructional review did not return a report."


def _reviewed_lesson(text: str) -> dict:
    """Copy the lesson and attach the exact text a review should read."""
    lesson = dict(st.session_state.lesson or {})
    lesson["edited_markdown"] = text
    lesson["audience"] = lesson.get("audience") or "introductory learners"
    lesson["duration_minutes"] = lesson.get("duration_minutes") or LESSON_MINUTES
    return lesson


def _run_research(title: str, objective: str) -> dict | None:
    """Search, and keep the previous bundle if this attempt fails."""
    try:
        research = collect_research(title.strip(), learning_objective=objective.strip())
    except LessonCreatorError as exc:
        st.session_state.research_error = str(exc)
        return None
    except Exception as exc:
        st.session_state.research_error = public_error_message(exc)
        return None
    st.session_state.research = research
    st.session_state.research_error = ""
    return research


def _run_generation(title: str, objective: str, audience: str, replace_edits: bool) -> dict | None:
    """Generate a lesson. A failure leaves the previous lesson and edits in place."""
    research = st.session_state.research
    if not research:
        st.session_state.generation_error = "Run research before generating a lesson."
        return None
    try:
        require_api_key()
        lesson = generate_lesson(
            title.strip(),
            research,
            audience.strip() or "introductory learners",
            LESSON_MINUTES,
            objective.strip(),
        )
    except LessonCreatorError as exc:
        st.session_state.generation_error = str(exc)
        return None
    except Exception as exc:
        st.session_state.generation_error = public_error_message(exc)
        return None
    st.session_state.lesson = lesson
    st.session_state.generation_error = ""
    _stage_markdown(lesson["markdown"], replace_edits)
    return lesson


def _run_one_check(kind: str, text: str, audience: str) -> None:
    """Retry a single quality check without touching the lesson editor."""
    lesson = st.session_state.lesson
    if not lesson:
        message = "Generate a lesson before running quality control."
        st.session_state[f"{kind}_error"] = message
        return
    reviewed = _reviewed_lesson(text)
    try:
        require_api_key()
        if kind == "factual":
            if not st.session_state.research:
                raise LessonCreatorError("Run research before the factual accuracy review.")
            report = run_source_check(reviewed, st.session_state.research)
        else:
            report = run_teaching_check(reviewed, audience)
    except LessonCreatorError as exc:
        st.session_state[f"{kind}_error"] = str(exc)
        return
    except Exception as exc:
        st.session_state[f"{kind}_error"] = public_error_message(exc)
        return
    st.session_state[f"{kind}_report"] = report
    st.session_state[f"{kind}_error"] = ""
    st.session_state[f"{kind}_basis"] = text


def _create_lesson(title: str, objective: str, audience: str, replace_edits: bool) -> None:
    """Run research, generation, and both reviews. Keep earlier work if a step fails."""
    with st.status("Creating the lesson...", expanded=True) as status:
        status.write("Searching websites, books, and YouTube.")
        research = _run_research(title, objective)
        if research is None:
            status.update(label="Research failed", state="error")
            return
        if research.get("errors"):
            status.write("Some searches failed. The lesson will use only what was retrieved.")
        elif not research.get("retrieved"):
            status.write("No sources came back. The lesson will say that research did not succeed.")

        status.write("Writing the 45-minute lesson.")
        lesson = _run_generation(title, objective, audience, replace_edits)
        if lesson is None:
            status.update(label="Lesson generation failed", state="error")
            return

        status.write("Running the factual and instructional reviews as separate checks.")
        bundle = review_lesson(
            _reviewed_lesson(lesson["markdown"]),
            research,
            audience.strip() or "introductory learners",
        )
        _store_reviews(bundle, lesson["markdown"])
        if st.session_state.factual_error or st.session_state.instructional_error:
            status.update(label="Lesson ready. A quality check still needs a retry.", state="error")
            return
        status.update(label="Lesson ready", state="complete")


def _slug(text: str) -> str:
    """Make a safe download file name from the lesson title."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "lesson"


def _render_messages() -> None:
    """Show step errors without hiding a report or edit that was kept."""
    labels = (
        ("Research", "research_error"),
        ("Lesson generation", "generation_error"),
        ("Factual accuracy review", "factual_error"),
        ("Instructional quality review", "instructional_error"),
    )
    for label, key in labels:
        message = st.session_state.get(key) or ""
        if message:
            st.error(f"{label}: {message}")

    research = st.session_state.research or {}
    if research.get("errors") and not st.session_state.research_error:
        st.warning("Research gaps: " + " ".join(str(item) for item in research["errors"]))


def _render_lesson_tab() -> None:
    """Editor and preview. The editor is the text the PDF exports."""
    lesson = st.session_state.lesson
    if lesson and not lesson.get("research_retrieved"):
        st.warning(
            "This draft was written without a complete source list. "
            "The references section only includes sources that were actually retrieved."
        )
    elif lesson and lesson.get("research_status"):
        st.info(lesson["research_status"])

    pending = st.session_state.pending_markdown
    if pending:
        st.warning("A new draft is ready. Your current edits are still in the editor.")
        if st.button("Replace editor with the new draft"):
            st.session_state.lesson_editor = pending
            st.session_state.applied_markdown = pending
            st.session_state.pending_markdown = ""

    editor_col, preview_col = st.columns(2, gap="large")
    with editor_col:
        st.subheader("Edit lesson")
        st.caption("Retries keep this text unless you choose to replace it.")
        st.text_area(
            "Lesson markdown",
            height=640,
            key="lesson_editor",
            label_visibility="collapsed",
            placeholder="The generated lesson will appear here.",
        )
    with preview_col:
        st.subheader("Preview")
        preview = st.session_state.lesson_editor.strip()
        if preview:
            st.markdown(preview)
        else:
            st.info("Generate a lesson to see the preview.")


def _render_source_list(items: list[dict], empty_text: str) -> None:
    """Show retrieved links. Snippets are the text returned by search."""
    if not items:
        st.caption(empty_text)
        return
    for item in items:
        title = item.get("title") or "Untitled"
        url = item.get("url") or ""
        st.markdown(f"**[{title}]({url})**")
        origin = item.get("origin") or ""
        channel = item.get("channel") or ""
        caption_bits = []
        if channel:
            caption_bits.append(channel)
        if origin == "open_library":
            caption_bits.append("Open Library")
        elif origin == "web_textbook_search":
            caption_bits.append("Textbook web search")
        elif origin == "duckduckgo":
            caption_bits.append("Web search")
        if caption_bits:
            st.caption(" · ".join(caption_bits))
        snippet = item.get("snippet") or item.get("description") or ""
        if snippet:
            st.write(snippet)


def _render_research_tab() -> None:
    """Show exactly what search returned, including failed searches."""
    research = st.session_state.research
    if not research:
        st.info("Research results will appear here after you create a lesson.")
        return

    websites, books, videos = st.columns(3)
    websites.metric("Websites", len(research.get("web") or []))
    books.metric("Books and textbook pages", len(research.get("books") or []))
    videos.metric("YouTube videos", len(research.get("youtube") or []))

    if research.get("retrieved"):
        st.caption(f"Research for “{research.get('topic', '')}”. Only these results are used as sources.")
    else:
        st.warning("No sources were retrieved. The lesson must not claim that research succeeded.")

    st.subheader("Websites")
    _render_source_list(research.get("web") or [], "No web pages were retrieved.")
    st.subheader("Books")
    _render_source_list(research.get("books") or [], "No books or textbook pages were retrieved.")
    st.subheader("YouTube videos")
    _render_source_list(research.get("youtube") or [], "No videos were retrieved.")


def _render_report(title: str, report: dict | None, error: str, basis: str) -> None:
    """Show one audit. A failed retry does not remove the previous audit."""
    st.subheader(title)
    if error and report:
        st.caption("The report below is the last successful review. The latest retry did not replace it.")
    if not report:
        st.info("This review has not produced a report yet.")
        return
    result = "Passed" if report.get("passed") else "Needs changes"
    st.metric("Result", result)
    editor = st.session_state.lesson_editor.strip()
    if basis and editor and editor != basis.strip():
        st.caption("This report was written before the latest editor changes. Re-check the edited lesson to review them.")
    st.markdown(report_to_markdown(report))


def _render_qc_tab(audience: str) -> None:
    """Show the two audits side by side. They are separate model calls."""
    if st.button("Re-check the edited lesson"):
        if not st.session_state.lesson:
            st.session_state.factual_error = "Generate a lesson before running quality control."
            st.session_state.instructional_error = "Generate a lesson before running quality control."
        else:
            text = st.session_state.lesson_editor.strip() or st.session_state.lesson.get("markdown", "")
            with st.spinner("Running both reviews on the edited lesson..."):
                bundle = review_lesson(
                    _reviewed_lesson(text),
                    st.session_state.research or {"errors": ["Research has not been run."], "retrieved": False},
                    audience.strip() or "introductory learners",
                )
            _store_reviews(bundle, text)
        st.rerun()

    left, right = st.columns(2, gap="large")
    with left:
        _render_report(
            "QC Layer 1: Factual accuracy",
            st.session_state.factual_report,
            st.session_state.factual_error,
            st.session_state.factual_basis,
        )
    with right:
        _render_report(
            "QC Layer 2: Instructional quality",
            st.session_state.instructional_report,
            st.session_state.instructional_error,
            st.session_state.instructional_basis,
        )


def _render_export_tab(title: str) -> None:
    """Download a PDF of the edited lesson, sources, and both reports."""
    lesson = st.session_state.lesson
    edited = st.session_state.lesson_editor.strip()
    if not lesson and not edited:
        st.info("Generate a lesson before exporting.")
        return

    st.write(
        "The PDF uses the text in the editor, then adds the retrieved sources and both quality-control reports."
    )
    payload = dict(lesson or {"title": title.strip() or "Lesson", "sources": []})
    if title.strip() and not payload.get("title"):
        payload["title"] = title.strip()
    try:
        pdf_bytes = build_lesson_pdf(
            payload,
            reviews={
                "factual": st.session_state.factual_report,
                "instructional": st.session_state.instructional_report,
                "errors": {
                    "factual": st.session_state.factual_error,
                    "instructional": st.session_state.instructional_error,
                },
            },
            edited_markdown=edited or None,
        )
    except Exception as exc:
        st.error(f"PDF export failed: {public_error_message(exc)}")
        return

    file_title = payload.get("title") or title or "lesson"
    st.download_button(
        "Download PDF",
        data=pdf_bytes,
        file_name=f"{_slug(str(file_title))}.pdf",
        mime="application/pdf",
        type="primary",
    )
    st.caption(f"{len(pdf_bytes):,} bytes")


def main() -> None:
    """Draw the lesson planner and run the pipeline when a button is clicked."""
    st.set_page_config(page_title="AI Lesson Creator", page_icon=":books:", layout="wide")
    _init_state()
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {border-right: 1px solid rgba(49, 51, 63, 0.12);}
        div[data-testid="stMetric"] {
            background: rgba(49, 51, 63, 0.04);
            border: 1px solid rgba(49, 51, 63, 0.08);
            padding: 0.55rem 0.75rem;
            border-radius: 0.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("AI Lesson Creator")
    st.write(
        "Research a topic, draft a 45-minute lesson, and review it with two independent quality checks."
    )

    with st.sidebar:
        st.header("Lesson")
        title = st.text_input("Lesson title", placeholder="HTTP Requests and Responses")
        objective = st.text_area(
            "Learning objective",
            height=140,
            placeholder="Explain GET and POST methods and distinguish between 2xx, 4xx, and 5xx response status categories.",
        )
        audience = st.text_input("Audience", value="introductory learners")
        st.caption(f"Lesson length is fixed at {LESSON_MINUTES} minutes.")
        replace_edits = False
        if _has_edits():
            replace_edits = st.checkbox(
                "Replace my lesson edits when generation succeeds",
                value=False,
            )
        st.divider()
        create_clicked = st.button("Create lesson", type="primary", use_container_width=True)
        retry_research = st.button("Retry research", use_container_width=True)
        retry_generation = st.button("Retry lesson generation", use_container_width=True)
        retry_factual = st.button("Retry factual review", use_container_width=True)
        retry_instructional = st.button("Retry instructional review", use_container_width=True)
        st.divider()
        st.caption("Gemini API key: " + ("found" if _key_is_set("GEMINI_API_KEY") else "not set"))
        st.caption("YouTube search does not use an API key.")

    if create_clicked or retry_research or retry_generation or retry_factual or retry_instructional:
        problem = _validate(title, objective)
        if problem:
            st.error(problem)
        elif create_clicked:
            _create_lesson(title, objective, audience, replace_edits)
        elif retry_research:
            with st.status("Retrying research...", expanded=True) as status:
                research = _run_research(title, objective)
                if research is None:
                    status.update(label="Research failed. Earlier results were kept.", state="error")
                else:
                    status.update(label="Research updated", state="complete")
        elif retry_generation:
            with st.status("Retrying lesson generation...", expanded=True) as status:
                lesson = _run_generation(title, objective, audience, replace_edits)
                if lesson is None:
                    status.update(label="Generation failed. The previous lesson and edits were kept.", state="error")
                else:
                    status.update(label="Lesson updated", state="complete")
        elif retry_factual:
            with st.spinner("Retrying the factual accuracy review..."):
                basis = st.session_state.lesson_editor.strip()
                if not basis and st.session_state.lesson:
                    basis = st.session_state.lesson.get("markdown", "")
                _run_one_check("factual", basis, audience)
        elif retry_instructional:
            with st.spinner("Retrying the instructional quality review..."):
                basis = st.session_state.lesson_editor.strip()
                if not basis and st.session_state.lesson:
                    basis = st.session_state.lesson.get("markdown", "")
                _run_one_check("instructional", basis, audience)

    saved = (st.session_state.research or {}).get("topic")
    if saved and title.strip() and saved != title.strip():
        st.info(f"The saved research is for “{saved}”. Retry research if this title should use a new search.")

    _render_messages()
    lesson_tab, research_tab, qc_tab, export_tab = st.tabs(
        ["Lesson", "Research", "Quality control", "Export"]
    )
    with lesson_tab:
        _render_lesson_tab()
    with research_tab:
        _render_research_tab()
    with qc_tab:
        _render_qc_tab(audience)
    with export_tab:
        _render_export_tab(title)


if __name__ == "__main__":
    main()
