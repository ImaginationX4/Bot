import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room


class FinalAcceptance(unittest.TestCase):
    def exercise(self, data, timeout=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "check.py"
            script.write_text(
                "from pathlib import Path\nimport sys, time\n"
                "data = Path('handoff.txt').read_bytes()\n"
                "print(f'expected=ready actual={data!r}', flush=True)\n"
                + ("time.sleep(30)\n" if timeout else "")
                + "sys.exit(0 if data == b'ready' else 1)\n"
            )
            room = Room(root)
            observer = sqlite3.connect(root / "group.db", check_same_thread=False)
            def snapshot():
                return observer.execute("""SELECT status,passed,output FROM group_tasks
                    LEFT JOIN acceptance ON id=task_id""").fetchall()
            before_commit = []
            def trace(sql):
                if sql == "COMMIT" and room.db.execute("SELECT count(*) FROM acceptance").fetchone()[0]:
                    before_commit.append(snapshot())
            def fake(work, agent, prompt, show_tool):
                if room.active == "C · Work":
                    (work / "handoff.txt").write_bytes(data)
                return "Passed"
            try:
                room.create([("C","Executor"),("B","Reviewer")])
                room.db.set_trace_callback(trace)
                with patch("room.run_turn", fake), patch("room.ACCEPTANCE_TIMEOUT_SECONDS", 1 if timeout else 600):
                    task = room.start("Write handoff.txt", final_command=f'"{sys.executable}" -S "{script}"')
                    room.worker.join(20)
                self.assertFalse(room.worker.is_alive())
                self.assertEqual(room.task_outputs(task)[-1], ("B","Final result","Passed"))
                self.assertEqual(before_commit, [[("checking", None, None)]])
                passed = data == b"ready" and not timeout
                status = "completed" if passed else "failed"
                [(actual_status, result, reason)] = snapshot()
                self.assertEqual((actual_status, result), (status, int(passed)))
                expected = "Acceptance timed out after 1 s." if timeout else f"Exit code {0 if passed else 1}"
                self.assertTrue(reason.startswith(expected), reason)
                if not timeout:
                    self.assertIn(f"actual={data!r}", reason)
                self.assertEqual(room.snapshot()[1][-1], ("Bot","Task acceptance",f"Task #{task} · {status}\n{reason}"))
                self.assertIsNone(room.pending())
            finally:
                observer.close()
                room.db.close()

    def test_ready(self):
        self.exercise(b"ready")

    def test_wrong_despite_reviewer_pass(self):
        self.exercise(b"wrong")

    def test_timeout(self):
        self.exercise(b"ready", timeout=True)

