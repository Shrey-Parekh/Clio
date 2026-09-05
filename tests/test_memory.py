"""Long-term memory: survives a restart, stays plain text, and never feeds
Clio her own replies back. Run: python tests/test_memory.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.memory.store import MemoryStore  # noqa: E402


def main():
    root = tempfile.mkdtemp()
    try:
        store = MemoryStore(root)
        store.start_session()
        store.append_turn("user", "my chemistry assignment is due Friday")
        store.append_turn("assistant", "Noted, Friday it is.")
        store.close()

        # A new process over the same directory: this is what a restart is.
        store = MemoryStore(root)
        assert [t.content for t in store.recent_turns(limit=2)] == [
            "my chemistry assignment is due Friday",
            "Noted, Friday it is.",
        ]
        print("OK  turns survive the process ending")

        # Plain JSONL on disk, not an opaque blob.
        line = next((Path(root) / "sessions").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        assert "chemistry" in line and line.startswith("{")
        print("OK  transcript is plain readable JSONL")

        # Recall returns what the user said, never what she said back - feeding an
        # assistant its own output makes it repeat itself.
        recalled = store.recall("chemistry assignment", limit=4)
        assert "chemistry" in recalled
        assert "Noted, Friday it is." not in recalled, recalled
        print("OK  recall excludes her own replies")

        # Facts are hand-editable markdown and dedupe regardless of punctuation.
        assert store.add_fact("Prefers to be called boss", category="Preferences")
        assert not store.add_fact("prefers to be called boss.")
        facts_md = (Path(root) / "facts.md").read_text(encoding="utf-8")
        assert "## Preferences" in facts_md and "- Prefers to be called boss" in facts_md
        print("OK  facts.md is plain markdown, deduped across punctuation")

        (Path(root) / "facts.md").write_text(facts_md + "- Studies chemistry\n", encoding="utf-8")
        assert "Studies chemistry" in store.facts()
        print("OK  hand edits are picked up immediately")

        # The index is a cache: deleting it must lose nothing.
        store.close()
        (Path(root) / "index.sqlite3").unlink()
        rebuilt = MemoryStore(root)
        assert rebuilt.rebuild_index() == 2
        assert rebuilt.search("chemistry"), "search works again after a rebuild"
        print("OK  index deleted and rebuilt from the plain files, nothing lost")

        # A corrupt index must self-heal from the transcripts, not raise and not
        # cost the memory that is sitting intact in plain files next to it.
        rebuilt.close()
        (Path(root) / "index.sqlite3").write_text("not a database", encoding="utf-8")
        recovered = MemoryStore(root)
        assert recovered.search("chemistry"), "should have rebuilt itself from the JSONL"
        assert "Prefers to be called boss" in recovered.recall("anything")
        print("OK  corrupt index self-heals from the transcripts, nothing lost")
        recovered.close()

        print("\nAll memory checks passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
