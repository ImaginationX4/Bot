import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from room import Room

CHECK = 'python -c "from pathlib import Path; import sys; data=Path(\'hello.txt\').read_bytes(); print(\'actual bytes\',data); sys.exit(data!=b\'hello\')"'


def inline_thread(target, args, daemon):
    return SimpleNamespace(start=lambda: target(*args))


def exercise(folder, correct, crash=False, resume=False):
    room = Room(folder)
    calls = folder / "calls"
    def fake(work, agent, prompt, show_tool, deadline=None, spawn=None):
        phase = room.active.split(" · ")[1]
        with calls.open("a") as out:
            print(phase, file=out)
        if phase == "Work":
            second = calls.read_text().splitlines().count("Work") == 2
            fixed = correct and second and "actual bytes b'wrong'" in prompt
            (work / "hello.txt").write_bytes(b"hello" if fixed else b"wrong")
        return phase

    save = room.save_acceptance
    def checkpoint(*args):
        save(*args)
        if crash:
            os._exit(39)

    with patch("room.run_turn", fake), patch.object(room, "save_acceptance", checkpoint):
        with patch("room.Thread", inline_thread):
            if resume:
                room.resume(1)
            else:
                room.create([("A", "Owner"), ("B", "Reviewer")])
                room.start("Create hello.txt", CHECK)
    return room


class RalphTests(unittest.TestCase):
    def scenario(self, correct, restart=False):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            if restart:
                child = subprocess.run([sys.executable, "-c",
                    "from test_ralph import *; exercise(Path(sys.argv[1]),True,True)", directory],
                    cwd=Path(__file__).parent, timeout=30,
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
                self.assertEqual(child.returncode, 39)
            room = exercise(folder, correct, resume=restart)
            try:
                calls = (folder / "calls").read_text().splitlines()
                expected = ["Work", "Work", "Final result"] if correct else ["Work"] * 3
                self.assertEqual(calls, ["Proposal", "Reply"] + expected)
                results = room.db.execute("SELECT round,passed,output FROM acceptance ORDER BY round").fetchall()
                self.assertEqual([(r,p) for r,p,_ in results],
                                 [(1,0),(2,1)] if correct else [(1,0),(2,0),(3,0)])
                self.assertIn("actual bytes b'wrong'", results[0][2])
                self.assertEqual(room.db.execute("SELECT id,status FROM group_tasks").fetchall(),
                                 [(1, "completed" if correct else "failed")])
                self.assertIsNone(room.pending())
                self.assertEqual((room.work / "hello.txt").read_bytes(), b"hello" if correct else b"wrong")
            finally:
                room.db.close()

    def test_fix_on_second_round(self):
        self.scenario(True)

    def test_exhaust_three_rounds(self):
        self.scenario(False)

    def test_resume_after_failed_acceptance(self):
        self.scenario(True, restart=True)

    def test_timeout_stops_worker_and_releases_task(self):
        import socket
        import time

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            script = folder / "slow.py"
            script.write_text(
                "import socket, time\n"
                "with socket.socket() as sock:\n"
                "    sock.bind(('127.0.0.1', 0))\n"
                "    print(sock.getsockname()[1], flush=True)\n"
                "    time.sleep(30)\n"
                "    print('finished')\n"
            )
            room = Room(folder)
            try:
                room.create([("A", "Owner"), ("B", "Reviewer")])
                calls = []
                def fake(*args, **kwargs):
                    calls.append(room.active.split(" · ")[1])
                    return "ok"

                with patch("room.ACCEPTANCE_TIMEOUT_SECONDS", 1), patch("room.run_turn", fake):
                    with patch("room.Thread", inline_thread):
                        started = time.monotonic()
                        room.start("Wait", f'"{sys.executable}" "{script}"')
                        self.assertLess(time.monotonic() - started, 10)
                        self.assertEqual(calls, ["Proposal", "Reply", "Work"])
                        [(number, passed, output)] = room.db.execute("SELECT round,passed,output FROM acceptance")
                        reason, port = output.splitlines()
                        self.assertEqual((number, passed, reason), (1, 0, "Acceptance timed out after 1 s."))
                        # The script holds this port throughout its sleep.
                        with socket.socket() as probe:
                            probe.bind(("127.0.0.1", int(port)))
                        self.assertEqual(room.db.execute("SELECT status FROM group_tasks").fetchone(), ("failed",))
                        room.start("New task")
                        self.assertEqual(room.db.execute("SELECT status FROM group_tasks ORDER BY id").fetchall(),
                                         [("failed",), ("completed",)])
            finally:
                room.db.close()

    def test_total_deadline_kills_work(self):
        import socket
        import time
        import room as bot

        with tempfile.TemporaryDirectory() as directory:
            room = Room(Path(directory))
            script = ("import socket,time; from pathlib import Path; s=socket.socket(); "
                      "s.bind(('127.0.0.1',0)); Path('port').write_text(str(s.getsockname()[1])); time.sleep(30)")
            popen = subprocess.Popen
            phases = []
            real_turn = bot.run_turn
            def simulated_turn(*args, **kwargs):
                phases.append(room.active.split(" · ")[1])
                if phases[-1] == "Work":
                    return real_turn(*args, **kwargs)
                return "ok"

            def local_process(args, **kwargs):
                return popen([sys.executable, "-S", "-c", script], **kwargs)

            try:
                room.create([("A","Owner"),("B","Reviewer")])
                with patch("room.Thread", inline_thread), patch("room.shutil.which", return_value="opencode"):
                    # Keep the OS process-tree terminator real.
                    with patch.object(bot, "subprocess", wraps=subprocess) as runner:
                        runner.Popen.side_effect = local_process
                        runner.PIPE, runner.STDOUT = subprocess.PIPE, subprocess.STDOUT
                        runner.CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        start = time.monotonic()
                        with patch("room.run_turn", simulated_turn):
                            room.start("Hang", "echo unexpected", total_seconds=5)
                        self.assertEqual(runner.Popen.call_count, 1)
                        self.assertLess(time.monotonic()-start, 12)
                self.assertEqual(phases, ["Proposal","Reply","Work"])
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", int((room.work/"port").read_text())))
                self.assertEqual(room.db.execute("SELECT status FROM group_tasks").fetchone(), ("failed",))
                self.assertIn("total time limit expired", room.snapshot()[1][-1][2])
            finally:
                room.db.close()

    def restart_with_deadline(self, restart_time, total_seconds=60):
        deadline = None if total_seconds is None else 1060
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            room = Room(folder)
            query = "SELECT task_id,deadline FROM ralph_tasks"
            try:
                room.create([("A","Owner"),("B","Reviewer")])
                with patch("room.time.time", return_value=1000), patch.object(room, "launch"):
                    room.start("Resume", "echo OK", total_seconds=total_seconds)
                original = room.db.execute(query).fetchone()
                self.assertEqual(original, (1, deadline))
            finally:
                room.db.close()
            with patch("room.time.time", return_value=restart_time), patch("room.run_turn", return_value="ok") as turn:
                with patch("room.subprocess.Popen", wraps=subprocess.Popen) as processes:
                    room = Room(folder)
                    try:
                        self.assertEqual(room.db.execute(query).fetchone(), original)
                        if deadline is None or restart_time < deadline:
                            with patch("room.Thread", inline_thread):
                                room.resume(original[0])
                            self.assertEqual([c.kwargs.get("deadline") for c in turn.call_args_list], [deadline]*4)
                            self.assertEqual(processes.call_count, 1)
                            self.assertEqual(room.db.execute(query).fetchone(), original)
                            self.assertEqual(room.db.execute("SELECT round,passed FROM acceptance").fetchall(), [(1, 1)])
                            expected = "completed"
                        else:
                            self.assertIsNone(room.pending())
                            turn.assert_not_called()
                            processes.assert_not_called()
                            expected = "failed"
                        self.assertEqual(room.db.execute("SELECT status FROM group_tasks").fetchone(), (expected,))
                    finally:
                        room.db.close()

    def test_resume_preserves_deadline(self):
        self.restart_with_deadline(1040)

    def test_resume_unlimited_after_two_hours(self):
        self.restart_with_deadline(8200, total_seconds=None)

    def test_expired_on_restart(self):
        self.restart_with_deadline(1061)

    def test_unlimited_rounds_survive_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            room = Room(folder)
            try:
                room.create([("A", "Owner"), ("B", "Reviewer")])
                with patch.object(room, "launch"):
                    room.start("Resume", "echo OK", max_rounds=None)
                task_id = room.pending()[0]
            finally:
                room.db.close()
            room = Room(folder)
            try:
                number, limit, *_ = room.ralph_progress()
                self.assertEqual((number, limit), (1, 0))
                with patch("room.run_turn", return_value="ok"), patch("room.Thread", inline_thread):
                    room.resume(task_id)
                self.assertEqual(room.ralph_progress()[:5], (1, 0, "completed", 1, 1))
            finally:
                room.db.close()
