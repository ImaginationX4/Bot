import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room


class RalphStop(unittest.TestCase):
    def check_stop(self, during_work):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            script = folder / "hang.py"
            script.write_text(
                "import socket, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "sock = socket.socket()\n"
                "sock.bind(('127.0.0.1', 0))\n"
                "Path(sys.argv[1]).write_text(str(sock.getsockname()[1]))\n"
                "if sys.argv[1] == 'parent':\n"
                "    subprocess.Popen([sys.executable, '-S', __file__, 'child'])\n"
                "time.sleep(30)\n"
            )
            room = Room(folder)
            args = [sys.executable, "-S", str(script), "parent"]
            phases = []
            def fake(work, agent, prompt, show_tool, spawn, **kwargs):
                phase = room.active.split(" · ")[1]
                phases.append(phase)
                if during_work and phase == "Work":
                    with spawn(args, cwd=work, start_new_session=sys.platform != "win32") as process:
                        process.wait()
                return phase

            try:
                room.create([("A", "Owner"), ("B", "Reviewer")])
                import shlex
                command = subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)
                with patch("room.run_turn", fake):
                    room.start("Stop test", command)
                    end = time.monotonic() + 15
                    while not (room.work / "child").exists() and time.monotonic() < end:
                        time.sleep(.02)
                    self.assertTrue((room.work / "child").exists())
                    room.stop(1)
                for name in ("parent", "child"):
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", int((room.work / name).read_text())))
                self.assertEqual(phases, ["Proposal", "Reply", "Work"])
                self.assertEqual(room.db.execute("SELECT count(*) FROM acceptance").fetchone(), (0,))
                expected = ["Task", "Proposal", "Reply"] + ([] if during_work else ["Work"])
                self.assertEqual([kind for _, kind, _ in room.snapshot()[1]], expected)
                self.assertIsNone(room.pending())
                room.db.close()
                room = Room(folder)
                self.assertEqual(room.db.execute("SELECT status FROM group_tasks WHERE id=1").fetchone(),
                                 ("cancelled",))
                with self.assertRaises(ValueError):
                    room.resume(1, retry=True)
                with patch("room.run_turn", return_value="ok"):
                    room.start("Next task")
                    room.worker.join(10)
                self.assertEqual(room.db.execute("SELECT status FROM group_tasks ORDER BY id").fetchall(),
                                 [("cancelled",), ("completed",)])
            finally:
                room.db.close()

    def test_stop_during_work(self):
        self.check_stop(True)

    def test_stop_during_acceptance(self):
        self.check_stop(False)
