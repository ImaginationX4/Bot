import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from room import Room, STEPS


def crash_worker(folder, during_reply):
    room = Room(folder)
    room.create([("Alice", "Owner"), ("Bob", "Reviewer")])
    save = room.save_step

    def record(work, agent, prompt, show_tool):
        phase = room.active.split(" · ")[1]
        with (folder / "calls").open("a") as out:
            print(phase, file=out)
        if during_reply and phase == "Reply":
            os._exit(37)
        return phase

    def checkpoint(*args):
        save(*args)
        if not during_reply:
            os._exit(37)

    with patch("room.run_turn", record), patch.object(room, "save_step", checkpoint):
        with patch("room.Thread.start", lambda thread: thread.run()):
            room.start("Write a greeting")


class Recovery(unittest.TestCase):
    def check_restart(self, interrupted):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            result = subprocess.run([sys.executable, "-c",
                "from test_room_recovery import *; crash_worker(Path(sys.argv[1]), sys.argv[2]=='1')",
                temp, str(int(interrupted))], cwd=Path(__file__).resolve().parent,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}, timeout=20)
            self.assertEqual(result.returncode, 37)
            room = Room(folder)
            try:
                task_id, status, count = room.pending()
                self.assertEqual((status, count), ("running" if interrupted else "ready", 1))
                calls = (folder / "calls").read_text().splitlines()
                self.assertEqual(calls, ["Proposal", "Reply"] if interrupted else ["Proposal"])
                if interrupted:
                    with self.assertRaises(ValueError):
                        room.resume(task_id)

                def record(work, agent, prompt, show_tool):
                    phase = room.active.split(" · ")[1]
                    self.assertEqual(room.pending(), (task_id, "running", len(resumed) + 1))
                    resumed.append(phase)
                    return phase

                resumed = []
                with patch("room.run_turn", record), patch("room.Thread.start", lambda thread: thread.run()):
                    room.resume(task_id, retry=interrupted)
                self.assertEqual(resumed, ["Reply", "Work", "Final result"])
                self.assertIsNone(room.pending())
                self.assertEqual(room.db.execute("SELECT id,status FROM group_tasks").fetchall(),
                                 [(task_id, "completed")])
                outputs = room.db.execute("""SELECT m.body FROM group_steps s JOIN messages m
                    ON m.rowid=s.output_message WHERE task_id=? ORDER BY step""", (task_id,))
                self.assertEqual([row[0] for row in outputs], [step[1] for step in STEPS])
            finally:
                room.db.close()

    def test_restart_after_first_commit(self):
        self.check_restart(False)

    def test_restart_during_reply(self):
        self.check_restart(True)
