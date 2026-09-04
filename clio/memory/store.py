"""Long-term memory as plain files, with a rebuildable index over them.

    sessions/*.jsonl   every turn, append-only
    facts.md           distilled durable facts, hand-editable
    index.sqlite3      derived FTS5 index

The files are the source of truth; `rebuild_index()` reconstructs the database
from them, so nothing lives only inside it. A graph store was considered and
rejected: it needs a server alongside Clio and an LLM call per ingest, for
recall FTS5 already covers.

Recall never loads the whole history - facts plus the top few matching turns.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.memory.store")

_FACTS_HEADER = """# Clio's long-term memory

Edit this freely - Clio reads it at the start of every session and never
rewrites your wording. Any line starting with "- " is one fact; "## " groups
them. Delete anything you don't want her to remember.
"""

_DEFAULT_CATEGORY = "General"
_MAX_FACTS = 500

# Dropped from search queries: too common to narrow anything, and including them
# makes every question match every turn.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "did", "do",
    "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "is",
    "it", "its", "me", "my", "of", "on", "or", "our", "she", "so", "that", "the",
    "their", "them", "then", "there", "they", "this", "to", "was", "we", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "you", "your",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """For dedupe comparison only. Trailing punctuation is stripped so
    "Boss" and "Boss." are not stored as two separate facts."""
    return " ".join(text.lower().split()).rstrip(".!?,;:")


def _tokenize(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", query.lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


@dataclass(frozen=True)
class Turn:
    ts: str
    role: str
    content: str
    session: str


@dataclass(frozen=True)
class SearchHit:
    turn: Turn
    snippet: str


class MemoryStore:
    def __init__(self, root: str | Path):
        self._root = Path(root)
        self._sessions_dir = self._root / "sessions"
        self._facts_path = self._root / "facts.md"
        self._index_path = self._root / "index.sqlite3"

        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._index_path)
        self._db.row_factory = sqlite3.Row
        self._ensure_schema()

        self._session_id: str = ""
        self._session_path: Path | None = None

    def _ensure_schema(self) -> None:
        self._db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5(
                ts UNINDEXED, role UNINDEXED, session UNINDEXED, content
            );
            """
        )
        self._db.commit()

    # ----- sessions and turns -----

    @property
    def session_id(self) -> str:
        return self._session_id

    def start_session(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        self._session_id = f"{stamp}_{uuid.uuid4().hex[:6]}"
        self._session_path = self._sessions_dir / f"{self._session_id}.jsonl"
        log.info("Memory session started", extra={"extra_fields": {"session": self._session_id}})
        return self._session_id

    def append_turn(self, role: str, content: str) -> None:
        content = content.strip()
        if not content:
            return
        if self._session_path is None:
            self.start_session()

        turn = Turn(ts=_now(), role=role, content=content, session=self._session_id)
        with self._session_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(turn), ensure_ascii=False) + "\n")

        self._db.execute(
            "INSERT INTO turns (ts, role, session, content) VALUES (?, ?, ?, ?)",
            (turn.ts, turn.role, turn.session, turn.content),
        )
        self._db.commit()

    def recent_turns(self, limit: int = 8, exclude_session: str | None = None) -> list[Turn]:
        """The last `limit` turns, oldest first - what a new session pulls back
        in so the conversation picks up where it left off."""
        sql = "SELECT ts, role, session, content FROM turns"
        params: list = []
        if exclude_session:
            sql += " WHERE session != ?"
            params.append(exclude_session)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)

        rows = self._db.execute(sql, params).fetchall()
        return [Turn(ts=r["ts"], role=r["role"], content=r["content"], session=r["session"]) for r in reversed(rows)]

    def search(
        self,
        query: str,
        limit: int = 4,
        exclude_session: str | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> list[SearchHit]:
        terms = _tokenize(query)
        if not terms:
            return []

        match = " OR ".join(f'"{t}"' for t in terms)
        sql = (
            "SELECT ts, role, session, content, snippet(turns, 3, '', '', '...', 16) AS snip "
            "FROM turns WHERE turns MATCH ?"
        )
        params: list = [match]
        if exclude_session:
            sql += " AND session != ?"
            params.append(exclude_session)
        if roles:
            sql += f" AND role IN ({','.join('?' for _ in roles)})"
            params.extend(roles)
        sql += " ORDER BY bm25(turns) LIMIT ?"
        params.append(limit)

        try:
            rows = self._db.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            # Broad on purpose: a corrupted or locked index file raises
            # DatabaseError, not OperationalError, and recall failing must
            # degrade the answer, never crash the turn that asked the question.
            log.warning("Memory search failed", extra={"extra_fields": {"query": query, "error": str(exc)}})
            return []

        return [
            SearchHit(
                turn=Turn(ts=r["ts"], role=r["role"], content=r["content"], session=r["session"]),
                snippet=r["snip"],
            )
            for r in rows
        ]

    def recall(self, query: str, limit: int = 4, exclude_session: str | None = None) -> str:
        """Facts plus relevant things the user said before, ready to prime a
        model with. Empty when there is nothing worth recalling.

        Never returns her own past replies. Feeding an assistant its own output
        back as context is self-reinforcing: it repeats whatever it said last
        time the topic came up.
        """
        parts: list[str] = []

        facts = self.facts()
        if facts:
            parts.append("What you know about the user:\n" + "\n".join(f"- {f}" for f in facts))

        hits = self.search(query, limit=limit, exclude_session=exclude_session, roles=("user",))
        if hits:
            lines = [f"- ({h.turn.ts[:10]}) {h.turn.content}" for h in hits]
            parts.append("Things he has told you before:\n" + "\n".join(lines))

        return "\n\n".join(parts)

    # ----- durable facts -----

    def facts(self) -> list[str]:
        if not self._facts_path.exists():
            return []
        try:
            text = self._facts_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to read facts.md", extra={"extra_fields": {"error": str(exc)}})
            return []
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                fact = stripped[2:].strip()
                if fact:
                    out.append(fact)
        return out

    def add_fact(self, text: str, category: str = _DEFAULT_CATEGORY) -> bool:
        """Appends one fact under `category`. Returns False if it's blank or a
        duplicate of something already recorded, so repeated conversations about
        the same thing don't grow the file forever.
        """
        fact = " ".join(text.split()).lstrip("-*• ").strip()
        if not fact:
            return False

        existing = self.facts()
        if _normalize(fact) in {_normalize(f) for f in existing}:
            return False
        if len(existing) >= _MAX_FACTS:
            log.warning("Fact limit reached, not recording", extra={"extra_fields": {"limit": _MAX_FACTS}})
            return False

        if not self._facts_path.exists():
            self._facts_path.write_text(_FACTS_HEADER, encoding="utf-8")

        lines = self._facts_path.read_text(encoding="utf-8").splitlines()
        heading = f"## {category}"
        if heading in lines:
            insert_at = len(lines)
            for i in range(lines.index(heading) + 1, len(lines)):
                if lines[i].startswith("## "):
                    insert_at = i
                    break
            while insert_at > 0 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, f"- {fact}")
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([heading, f"- {fact}"])

        self._facts_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Fact recorded", extra={"extra_fields": {"category": category, "fact": fact}})
        return True

    # ----- maintenance -----

    def rebuild_index(self) -> int:
        """Rebuilds the index from the JSONL transcripts. The index holds nothing
        the plain files don't, so this is always safe."""
        self._db.execute("DELETE FROM turns")
        count = 0
        for path in sorted(self._sessions_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Skipping malformed turn", extra={"extra_fields": {"file": path.name}})
                    continue
                self._db.execute(
                    "INSERT INTO turns (ts, role, session, content) VALUES (?, ?, ?, ?)",
                    (
                        record.get("ts", ""),
                        record.get("role", ""),
                        record.get("session", path.stem),
                        record.get("content", ""),
                    ),
                )
                count += 1
        self._db.commit()
        log.info("Memory index rebuilt", extra={"extra_fields": {"turns": count}})
        return count

    def close(self) -> None:
        self._db.close()
