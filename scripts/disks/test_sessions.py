from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path, PurePath
from unittest import mock

from scripts.disks import sessions
from scripts.disks.measure import DisksData, Folder, Mount
from scripts.disks.sessions import State
from scripts.lib import scratch, session_folder

GIB = 1 << 30


def folder(name: str, size: int, children: list[Folder] | None = None) -> Folder:
    listed = children or []
    return {"name": name, "size": size, "other": size - sum(child["size"] for child in listed), "children": listed}


class ReadSessionsTests(unittest.TestCase):
    def test_sessions_show_largest_first_with_held_folders_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(os.path.realpath(directory)) / "session"
            for name, session_id in (("fps", "a1"), ("lfo", "b2"), ("demo-impl", ""), ("bare", None)):
                (root / name).mkdir(parents=True)
                if session_id is not None:
                    session_folder.write_record(root / name, {"owner": name, "session_id": session_id})
            (root / ".id").mkdir()
            (root / "link").symlink_to(root / "fps")
            _ = (root / "notes.txt").write_text("")
            # The measure finds the session root under the build drive, where the mount table puts it.
            backing = PurePath("/build") / root.relative_to("/")
            data: DisksData = {"measured_at": 0, "keep_bytes": GIB // 4, "drives": [
                {"mount": "/build", "tree": folder("/build", 500 * GIB, [folder(str(backing.relative_to("/build")), 40 * GIB, [
                    folder("fps", 28 * GIB, [folder("run", 21 * GIB)]), folder("lfo", 7 * GIB)])])}]}
            mounts = (Mount("254:0", PurePath("/"), PurePath("/build")), Mount("254:0", PurePath("/") / root.relative_to("/"), root))
            holds: dict[str, scratch.Hold] = {
                str(root / "fps" / "run"): {"task": "phase:fps/3", "maker": "x", "reason": "", "made": 1.0, "ended": 0.0},
                str(root / "fps" / "center-shot"): {"task": "phase:fps/3", "maker": "x", "reason": "", "made": 1.0, "ended": 5.0},
                str(root / "fps"): {"task": "phase:fps/3", "maker": "x", "reason": "", "made": 1.0, "ended": 0.0},
                str(root / "other" / "run"): {"task": "phase:x/1", "maker": "x", "reason": "", "made": 1.0, "ended": 0.0}}

            states: dict[str, State] = {"a1": "running", "b2": "ended"}

            def state(session_id: str) -> State:
                return states.get(session_id, "unknown")

            report = sessions.read_sessions(root, data, holds, mounts, state)
        self.assertEqual(report["root"], str(root))
        self.assertEqual([(row["name"], row["size"], row["state"]) for row in report["sessions"]],
                         [("fps", 28 * GIB, "running"), ("lfo", 7 * GIB, "ended"), ("bare", 0, "unknown"),
                          ("demo-impl", 0, "unknown")])
        self.assertEqual(report["sessions"][0]["held"], [{"name": "center-shot", "task": "phase:fps/3", "ended": True},
                                                         {"name": "run", "task": "phase:fps/3", "ended": False}])
        self.assertEqual([row["held"] for row in report["sessions"][1:]], [[], [], []])

    def test_no_session_root_gives_no_report(self) -> None:
        with mock.patch.object(session_folder, "session_root", return_value=None):
            self.assertIsNone(sessions.session_report())


if __name__ == "__main__":
    _ = unittest.main()
