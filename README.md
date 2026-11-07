# AI Lesson Creator

A Streamlit web app that researches a topic, drafts a structured 45-minute lesson, and runs two independent quality-control reviews — all from a lesson title and a learning objective.

---

## Features

- **Research** — searches websites (DuckDuckGo), books (Open Library), and YouTube for relevant sources before writing a single word of lesson content.
- **Lesson generation** — uses Gemini to draft a complete 45-minute lesson: overview, timed schedule, explanations, examples, practical applications, an activity, and knowledge checks with instructor answers.
- **Two separate quality-control reviews** — a factual accuracy check (lesson vs. research sources) and an instructional quality check (alignment, clarity, structure, examples, activities) run in parallel as independent model calls.
- **Instructor editing** — a side-by-side markdown editor and live preview let instructors revise the draft. Retrying any step preserves edits unless the instructor explicitly chooses to replace them.
- **PDF export** — downloads a single PDF containing the edited lesson, the retrieved source list (no invented citations), and both QC reports.
- **Transparent error handling** — failed searches or API errors are reported per-step without wiping earlier successful results.

---

## Prerequisites

- Python 3.10 or later
- A [Gemini API key](https://aistudio.google.com/api-keys)

---

## Installation

1. **Clone or download the project.**

2. **Create and activate a virtual environment** (recommended):

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

---

## API key setup

1. Copy `.env.example` to `.env`:

   Windows:
   ```
   copy .env.example .env
   ```

   macOS/Linux:
   ```
   cp .env.example .env
   ```

2. Open `.env` and paste your Gemini API key:

   ```
   GEMINI_API_KEY=your_key_here
   ```

   The `.env` file is excluded from version control by `.gitignore`. Never commit a real key.

---

## Running the app

```bash
streamlit run app.py
```

The app opens in your browser at `http://localhost:8501`.

---

## Running the pipeline verification script

The verification script generates two sample lessons end-to-end and checks that the PDFs contain the expected content and source links. It requires a valid API key and an active internet connection.

```bash
python _verify_pipeline.py
```

Output files are written to the `exports/` directory.

---

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Streamlit entry point and UI |
| `research.py` | Web, book, and YouTube search |
| `lesson_generator.py` | Lesson drafting via Gemini |
| `quality_control.py` | Factual accuracy and instructional quality reviews |
| `export.py` | PDF generation with ReportLab |
| `gemini_client.py` | Gemini API wrapper with retry logic |
| `errors.py` | Shared error class and secret-scrubbing helper |
| `_verify_pipeline.py` | End-to-end pipeline verification script |

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini API key used for lesson generation and QC reviews |
| `GEMINI_MODEL` | No | Override the Gemini model ID (default: `gemini-3.5-flash`) |
