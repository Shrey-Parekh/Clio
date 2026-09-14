"""Getting words out of files that aren't plain text (6.9), and out of project
folders.

One function per format, all read-only, none of them clever. The point is to
hand the rest of Clio a string: `files.py` decides what to say, and the model
only ever sees text that came out of here.

Each extractor also produces a `summary` - a deterministic sentence like "12
pages" or "3 sheets, 412 rows". That is what she says about a file without
spending a model call, and it is true even when the network is down.

**Projects are read the same way.** A folder with a README and a manifest is a
document too: what it is, what it needs, and the command it runs with. That
answer is what 7.1 registers and 7.2 runs, so reading a project and running it
agree by construction rather than by luck.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.documents")

# Enough for a chunked summary to be worth doing, small enough that one bad file
# cannot eat the machine's memory.
MAX_CHARS = 400_000
_SAMPLE_ROWS = 5
_SAMPLE_CELLS = 12

_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".heic"}
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "tests", "docs"}
_MANIFESTS = ("pyproject.toml", "requirements.txt", "package.json", "Cargo.toml",
              "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
              "environment.yml", "Dockerfile", "Makefile")
_READMES = ("README.md", "README.rst", "README.txt", "README")
_ENTRY_POINTS = ("main.py", "app.py", "run.py", "train.py", "manage.py", "index.js",
                 "server.js", "main.go", "main.rs")


@dataclass(frozen=True)
class Document:
    kind: str          # "pdf", "word", "slides", "sheet", "table", "data", "project", "image"
    text: str          # what the model may be shown
    summary: str       # a true sentence, written without a model
    truncated: bool = False


def supported(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in _EXTRACTORS or suffix in _IMAGES


def extract(path: Path, max_chars: int = MAX_CHARS) -> Document | None:
    """None means "not a format I know", which the caller says out loud."""
    suffix = path.suffix.lower()
    if path.name.startswith("~$"):
        # Word, Excel and PowerPoint leave these behind while a document is
        # open. They carry the right extension and none of the contents, so
        # every parser throws on them. Found live: three sitting in Documents.
        return Document("lock", "", f"{path.name} is the stub Office leaves while a "
                                    "document is open, not the document itself")
    if suffix in _IMAGES:
        # Honest rather than silent: his Groq account serves no vision model, so
        # there is nothing to look at pictures with (checked 2026-09-14).
        return Document(kind="image", text="",
                        summary=f"{path.name} is an image, and I've no way to look at pictures yet")
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        return None
    document = extractor(path, max_chars)
    log.info("Document read", extra={"extra_fields": {
        "file": path.name, "kind": document.kind,
        "chars": len(document.text), "truncated": document.truncated}})
    return document


def _pdf(path: Path, max_chars: int) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = len(reader.pages)
    parts: list[str] = []
    size = 0
    read = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            # One unreadable page in a hundred is not a failed document.
            continue
        read += 1
        if text.strip():
            parts.append(text)
            size += len(text)
        if size >= max_chars:
            break
    body = "\n\n".join(parts)[:max_chars]
    if not body.strip():
        # A scan is pages of pictures. Saying "it's empty" would be a lie.
        return Document("pdf", "", f"{path.name} is a {pages}-page scan with no text in it - "
                                   "I'd need to be able to read pictures", False)
    return Document("pdf", body, f"{path.name} is a {pages}-page PDF",
                    len(body) >= max_chars or read < pages)


def _word(path: Path, max_chars: int) -> Document:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    tables = 0
    for table in document.tables:
        tables += 1
        for row in table.rows[:_SAMPLE_ROWS]:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    whole = "\n".join(parts)
    words = sum(len(p.split()) for p in parts)
    summary = f"{path.name} is a Word document, about {words} words"
    if tables:
        summary += f" and {tables} table{'s' if tables > 1 else ''}"
    return Document("word", whole[:max_chars], summary, len(whole) > max_chars)


def _slides(path: Path, max_chars: int) -> Document:
    from pptx import Presentation

    deck = Presentation(str(path))
    parts: list[str] = []
    count = 0
    for index, slide in enumerate(deck.slides, start=1):
        count = index
        lines = [f"--- slide {index} ---"]
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
        # Speaker notes are where the actual argument usually lives.
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        if notes:
            lines.append(f"(notes) {notes}")
        if len(lines) > 1:
            parts.append("\n".join(lines))
    whole = "\n\n".join(parts)
    return Document("slides", whole[:max_chars], f"{path.name} is a deck of {count} slides",
                    len(whole) > max_chars)


def _sheet(path: Path, max_chars: int) -> Document:
    from openpyxl import load_workbook

    # read_only + data_only: values rather than formulas, and it does not load
    # the whole workbook into memory just to say what shape it is.
    book = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    totals: list[str] = []
    try:
        for sheet in book.worksheets:
            rows = sheet.max_row or 0
            columns = sheet.max_column or 0
            totals.append(f"{sheet.title} ({rows} rows, {columns} columns)")
            parts.append(f"--- sheet {sheet.title}: {rows} rows, {columns} columns ---")
            for row in sheet.iter_rows(max_row=_SAMPLE_ROWS + 1, max_col=_SAMPLE_CELLS,
                                       values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
    finally:
        book.close()
    return Document("sheet", "\n".join(parts)[:max_chars],
                    f"{path.name} is a spreadsheet: " + "; ".join(totals), False)


def _table(path: Path, max_chars: int) -> Document:
    """CSV and TSV, read with the standard library and sniffed for delimiter,
    because a semicolon-separated export is still a csv file to Windows."""
    raw = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(raw), dialect))
    if not rows:
        return Document("table", "", f"{path.name} is an empty table", False)
    header, body_rows = rows[0], rows[1:]
    lines = [" | ".join(header)] + [" | ".join(r) for r in body_rows[:_SAMPLE_ROWS]]
    summary = (f"{path.name} has {len(body_rows)} rows and {len(header)} columns: "
               + ", ".join(header[:_SAMPLE_CELLS]))
    return Document("table", "\n".join(lines), summary, len(body_rows) > _SAMPLE_ROWS)


def _data(path: Path, max_chars: int) -> Document:
    """JSON described by its shape. A model given 4,000 lines of braces learns
    less than it does from "a list of 300 objects with these keys"."""
    raw = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Document("data", raw[:2000], f"{path.name} isn't valid JSON ({exc.msg})", False)
    return Document("data", _shape(parsed), f"{path.name} is JSON: {_shape(parsed, deep=False)}",
                    len(raw) >= max_chars)


def _shape(value, deep: bool = True, depth: int = 0) -> str:
    pad = "  " * depth
    if isinstance(value, dict):
        keys = list(value)[:_SAMPLE_CELLS]
        if not deep or depth >= 2:
            return f"an object with {len(value)} keys: {', '.join(map(str, keys))}"
        lines = [f"{pad}object, {len(value)} keys"]
        for key in keys:
            lines.append(f"{pad}  {key}: {_shape(value[key], deep, depth + 2)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "an empty list"
        return f"a list of {len(value)}, first item: {_shape(value[0], deep and depth < 2, depth + 1)}"
    return type(value).__name__


def describe_project(folder: Path, max_chars: int = MAX_CHARS) -> Document:
    """What a project is, what it needs, and how it is run - from the files that
    already say so. No model, no guessing from the folder name."""
    parts: list[str] = []
    found: list[str] = []

    for name in _READMES:
        readme = folder / name
        if readme.exists():
            parts.append(f"--- {name} ---\n"
                         + readme.read_text(encoding="utf-8", errors="replace")[:20_000])
            found.append(name)
            break

    for name in _MANIFESTS:
        manifest = folder / name
        if manifest.exists():
            parts.append(f"--- {name} ---\n"
                         + manifest.read_text(encoding="utf-8", errors="replace")[:8_000])
            found.append(name)

    entries = [name for name in _ENTRY_POINTS if (folder / name).exists()]
    scripts = [p.name for p in folder.glob("*.ps1")] + [p.name for p in folder.glob("*.bat")]
    if entries or scripts:
        parts.append("--- entry points ---\n" + ", ".join(entries + scripts))

    command = run_command(folder, entries)
    summary = f"{folder.name} looks like " + (_language(found) or "a folder of files")
    if command:
        summary += f", run with {command}"
    elif found:
        summary += ", and I can't tell from its files how it's meant to be run"

    whole = "\n\n".join(parts)
    return Document("project", whole[:max_chars], summary, len(whole) > max_chars)


def run_command(folder: Path, entries: list[str] | None = None) -> str:
    """The command this project is run with, according to its own files. Empty
    when they do not say - 7.2 must never invent one."""
    if entries is None:
        entries = [name for name in _ENTRY_POINTS if (folder / name).exists()]
    package = folder / "package.json"
    if package.exists():
        try:
            scripts = json.loads(
                package.read_text(encoding="utf-8", errors="replace")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name in ("dev", "start", "serve", "build"):
            if name in scripts:
                return f"npm run {name}"
    if (folder / "pyproject.toml").exists() or (folder / "requirements.txt").exists():
        # A package with a __main__ is run as a module; a loose script is not.
        package = _package_with_main(folder)
        if package:
            return f"python -m {package}"
        if entries:
            return f"python {entries[0]}"
    if entries:
        first = entries[0]
        return f"python {first}" if first.endswith(".py") else f"node {first}"
    if (folder / "Makefile").exists():
        return "make"
    if (folder / "Cargo.toml").exists():
        return "cargo run"
    if (folder / "go.mod").exists():
        return "go run ."
    return ""


def _package_with_main(folder: Path) -> str:
    """The real, correctly-cased package name. Windows matches paths without
    caring about case, so `folder / folder.name` finds `clio` inside `Clio` and
    yields "python -m Clio" - a command that works here and nowhere else."""
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if child.name in _SKIP_DIRS:
            continue
        if (child / "__main__.py").exists():
            return child.name
    return ""


def _language(found: list[str]) -> str:
    for name, said in (("pyproject.toml", "a Python project"),
                       ("requirements.txt", "a Python project"),
                       ("package.json", "a Node project"), ("Cargo.toml", "a Rust project"),
                       ("go.mod", "a Go project"), ("pom.xml", "a Java project"),
                       ("build.gradle", "a Java project"), ("Gemfile", "a Ruby project"),
                       ("composer.json", "a PHP project"), ("Dockerfile", "a Docker project")):
        if name in found:
            return said
    return "a project with a readme" if found else ""


_EXTRACTORS = {
    ".pdf": _pdf,
    ".docx": _word, ".docm": _word,
    ".pptx": _slides, ".pptm": _slides,
    ".xlsx": _sheet, ".xlsm": _sheet,
    ".csv": _table, ".tsv": _table,
    ".json": _data,
}
