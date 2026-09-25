"""Installing and updating apps through winget (7.8).

winget is faked with tables copied from its real output on this machine,
spinner frames included. What is checked: the readback names the exact id; a
search with several close names makes her ask instead of picking; "update
everything" is refused; uninstalling says it can't be undone; checking is free;
and the command that would run is winget's non-interactive form, from its own
catalogue, as an argument list.

Run: python tests/test_software.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.software import Software, parse_software  # noqa: E402
from clio.core import jobs, winget  # noqa: E402
from clio.core.jobs import JobRunner, explain  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

SEARCH_7ZIP = """\r   - \r   \\ \r
Name                               Id                        Version         Match         Source
-------------------------------------------------------------------------------------------------
7-Zip                              7zip.7zip                 26.03           Moniker: 7zip winget
NanaZip                            M2Team.NanaZip            7.0.1832.0      Tag: 7zip     winget
7-Zip ZS                           mcmilk.7zip-zstd          26.02-v1.5.7-R2 Tag: 7zip     winget
"""
SEARCH_ZIP = """Name     Id                 Version    Match     Source
-------------------------------------------------------
PeaZip   Giorgiotani.Peazip 10.0       Tag: zip  winget
NanaZip  M2Team.NanaZip     7.0.1832.0 Tag: zip  winget
7-Zip    7zip.7zip          26.03      Tag: zip  winget
Bandizip Bandisoft.Bandizip 7.36       Tag: zip  winget
"""
UPGRADES = """Name                   Id                 Version        Available      Source
---------------------------------------------------------------------------------
Google Chrome          Google.Chrome.EXE  153.0.8010.54  154.0.8037.58  winget
Discord                Discord.Discord    1.0.9200       1.0.9210       winget
Steam                  Valve.Steam        2.10.91.91     2.10.91.92     winget
AnyDesk                AnyDesk.AnyDesk    ad 9.6.12      9.7.16         winget
4 upgrades available.
"""
LIST_VLC = """Name             Id             Version Source
------------------------------------------------
VLC media player XPDM1ZW6815MQM 3.0.23  msstore
"""
NOTHING = "No installed package found matching input criteria.\n"


def fake_winget(args):
    verb, rest = args[0], args[1:]
    if verb == "search":
        return {"7zip": SEARCH_7ZIP, "zip": SEARCH_ZIP}.get(rest[0], "No package found.\n")
    if verb == "upgrade":
        return UPGRADES
    if verb == "list":
        return LIST_VLC if rest[0] in ("vlc", "vlc media player") else NOTHING
    raise AssertionError(args)


async def main():
    base = Path(tempfile.mkdtemp(prefix="clio-software-"))
    winget.run = fake_winget
    jobs.toast = lambda title, text: None
    try:
        # --- reading winget's tables ---

        rows = winget.parse_table(SEARCH_7ZIP)
        assert [p.id for p in rows] == ["7zip.7zip", "M2Team.NanaZip", "mcmilk.7zip-zstd"], rows
        assert rows[0].match == "Moniker: 7zip"
        pending = winget.parse_table(UPGRADES)
        assert len(pending) == 4, "the '4 upgrades available' line is not a row"
        assert pending[3].version == "ad 9.6.12" and pending[3].available == "9.7.16"
        assert winget.parse_table(LIST_VLC)[0].name == "VLC media player", "names keep their spaces"
        assert winget.parse_table(NOTHING) == []
        print("OK  winget's tables read by column, spinner and footer ignored")

        # --- the sentence ---

        assert parse_software("install 7-Zip") == ("install", "7-zip")
        assert parse_software("hey clio, update Chrome") == ("update", "chrome")
        assert parse_software("uninstall the VLC app") == ("uninstall", "vlc")
        assert parse_software("what needs updating") == ("updates", "")
        assert parse_software("is vlc installed") == ("check", "vlc")
        assert parse_software("the second one") == ("choose", "second")
        assert parse_software("what time is it") is None
        print("OK  install, update, uninstall, check and pick are recognised")

        # --- resolving: exact id, or he picks ---

        s = Software(JobRunner(base / "memory"))
        seven = s.resolve("install 7zip")
        assert seven.action == "install" and seven.package.id == "7zip.7zip", seven
        assert Software.describe(seven) == (
            "Installing 7-Zip, id 7zip.7zip, from winget, accepting its licence terms")

        several = s.resolve("install zip")
        assert not several.changes and "Which one?" in several.said, several
        assert "one, PeaZip; two, NanaZip; three, 7-Zip" in several.said, several.said
        picked = s.resolve("the third one")
        assert picked.action == "install" and picked.package.id == "7zip.7zip", picked
        assert s.resolve("the second one") is None, "a list answers one question"
        # Found live: "zip" is LiteMonitor's moniker - a system monitor. A
        # publisher's chosen short name is not a clear match.
        winget.run = lambda args: ("Name        Id                  Version Match\n"
                                   "------------------------------------------------\n"
                                   "LiteMonitor Diorser.LiteMonitor 1.3.6   Moniker: zip\n"
                                   "WinZip      Corel.WinZip        29.0    Tag: zip\n")
        assert not Software(JobRunner(base / "m4")).resolve("install zip").changes, \
            "a moniker alone picked the wrong app"
        winget.run = fake_winget
        print("OK  a clear match is read back by id; several make her ask and he picks")

        chrome = s.resolve("update chrome")
        assert Software.describe(chrome) == (
            "Updating Google Chrome from 153.0.8010.54 to 154.0.8037.58, id Google.Chrome.EXE")
        # Found live: "update my notes" became Microsoft Sticky Notes after 7s
        # of winget. "My" is his own things, decided without asking winget.
        def no_winget(args):
            raise AssertionError(f"asked winget about his notes: {args}")
        winget.run = no_winget
        assert s.resolve("update my notes") is None, "not software - left for the notes intent"
        assert s.resolve("uninstall my tasks") is None
        assert "one app at a time" in s.resolve("update my apps").said
        winget.run = fake_winget
        vlc = s.resolve("uninstall vlc")
        assert Software.describe(vlc).endswith("That can't be undone from here"), vlc
        assert vlc.package.id == "XPDM1ZW6815MQM", "a Store install is uninstalled by its own id"
        for everything in ("update everything", "update all my apps"):
            refused = s.resolve(everything)
            assert not refused.changes and "one app at a time" in refused.said, refused
        print("OK  update and uninstall find what's installed; 'update everything' is refused")

        # --- checking is free and says it plainly ---

        listed = s.resolve("what needs updating")
        assert listed.said == ("4 apps have updates: Google Chrome, Discord, Steam and 1 more. "
                               "The full list is in the window."), listed.said
        assert "AnyDesk  ad 9.6.12 -> 9.7.16" in listed.listing
        assert s.resolve("is vlc installed").said == "Yes, VLC media player is installed, version 3.0.23."
        assert s.resolve("is photoshop installed").said == "I can't see photoshop installed."

        def broken(args):
            raise winget.WingetError("winget took more than a minute to answer")
        winget.run = broken
        assert "couldn't ask winget" in Software(JobRunner(base / "m2")).resolve("install 7zip").said
        winget.run = fake_winget
        print("OK  what needs updating and is-it-installed answer freely; winget failures are said")

        # --- what would actually run ---

        args = winget.install_args(seven.package)
        assert args[:5] == ["winget", "install", "--id", "7zip.7zip", "--exact"], args
        for flag in ("--source", "--silent", "--disable-interactivity", "--accept-package-agreements"):
            assert flag in args, flag
        assert "--accept-source-agreements" not in args, "source terms are his to accept"
        assert "--silent" in winget.uninstall_args(vlc.package)
        assert explain("Installer failed with exit code: 1602") == \
            "it was cancelled - the admin prompt was probably declined"
        assert explain("Installer failed with exit code: 1603") == "the installer failed with code 1603"
        print("OK  non-interactive winget, its own catalogue, as a list; failures in plain words")

        # --- through the real router ---

        o = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, memory_root=str(base / "m3"))
        caps = {c.name: c for c in o._router.capabilities()}
        assert caps["software"].permission is Permission.CONFIRM
        assert caps["software_read"].permission is Permission.FREE
        matched = o._router.match("install 7zip")
        assert matched.intent == "software" and "id 7zip.7zip" in matched.description, matched
        assert o._router.match("what needs updating").intent == "software_read"
        assert o._router.match("update everything").intent == "software_read"
        assert o._router.match("install zip").intent == "software_read", "asking which is free"
        print("OK  changes are CONFIRM with the id read back; checking and choosing are free")

        print("\nAll software checks passed.")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
