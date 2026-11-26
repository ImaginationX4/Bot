import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room


class TaskMembers(unittest.TestCase):
    def selected_pair(self, restart):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            room = Room(folder)
            calls = []

            def fake(work, agent, prompt, show_tool):
                calls.append(prompt.split(". Your role")[0].removeprefix("You are "))
                return "ok"

            try:
                room.create([("A", "Owner"), ("B", "Review")])
                room.add_member(("C", "Build"))
                _, b, c = [row[0] for row in room.members()]
                with patch("room.run_turn", fake), patch("room.Thread.start", lambda t: t.run()):
                    save = room.save_step
                    def checkpoint(*args):
                        save(*args)
                        if restart:
                            raise SystemExit()

                    with patch.object(room, "save_step", checkpoint):
                        if restart:
                            with self.assertRaises(SystemExit):
                                room.start("Build", executor_id=c, reviewer_id=b)
                        else:
                            room.start("Build", executor_id=c, reviewer_id=b)
                    if restart:
                        task_id, status, count = room.pending()
                        self.assertEqual((status, count, calls), ("ready", 1, ["C"]))
                        room.db.close()
                        room = Room(folder)
                        self.assertEqual(room.pending()[0], task_id)
                        room.resume(task_id)
                self.assertEqual(calls, ["C", "B", "C", "B"])
                self.assertEqual(room.db.execute("SELECT executor_id,reviewer_id FROM task_members").fetchall(), [(c,b)])
                self.assertEqual(room.db.execute("SELECT status FROM group_tasks").fetchone(), ("completed",))
            finally:
                room.db.close()

    def test_chosen_pair(self):
        self.selected_pair(False)

    def test_resume_chosen_pair(self):
        self.selected_pair(True)

    def test_old_pair_and_task(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            db = sqlite3.connect(folder / "group.db")
            db.executescript("""CREATE TABLE people(name TEXT, role TEXT);
                INSERT INTO people VALUES ('A','Build'),('B','Review');
                CREATE TABLE group_tasks(id INTEGER PRIMARY KEY,status TEXT);
                INSERT INTO group_tasks VALUES (1,'ready');
                CREATE TABLE messages(name TEXT,kind TEXT,body TEXT);
                INSERT INTO messages VALUES ('You','Task','Legacy');""")
            db.close()
            room = Room(folder)
            try:
                room.add_member(("C","Other"))
                with patch("room.run_turn", return_value="ok"), patch("room.Thread.start", lambda t:t.run()):
                    room.resume(1)
                    room.start("New")
                names = [m[0] for m in room.snapshot()[1] if m[1] != "Task"]
                self.assertEqual(names, ["A","B","A","B"] * 2)
            finally:
                room.db.close()

    def test_planner_handoff(self):
        self.check_planner_handoff(restart=False)

    def test_planner_handoff_after_restart(self):
        self.check_planner_handoff(restart=True)

    def check_planner_handoff(self, restart):
        with tempfile.TemporaryDirectory() as temp:
            room = Room(Path(temp))
            calls = []
            plan = "PLAN-7C91: write handoff.txt"
            feedback = "REVIEW-3D62: verify exact bytes"
            def fake(work, agent, prompt, show_tool):
                name = prompt.split(".")[0].removeprefix("You are ")
                phase = room.active.split(" · ")[1]
                calls.append((name, phase))
                path = work / "handoff.txt"
                if phase == "Proposal":
                    return plan
                if phase == "Reply":
                    self.assertIn(plan, prompt)
                    return feedback
                if phase == "Work":
                    self.assertIn(plan, prompt)
                    self.assertIn(feedback, prompt)
                    path.write_bytes(b"ready")
                    return "Created handoff.txt"
                return f"Read {path.name}: {path.read_bytes()!r}"

            try:
                room.create([("A","Planner"), ("B","Reviewer")])
                room.add_member(("C","Executor"))
                with patch("room.run_turn", fake), patch("room.Thread.start", lambda t: t.run()):
                    options = dict(planner_id=1, executor_id=3, reviewer_id=2)
                    if restart:
                        save = room.save_step
                        def checkpoint(*args):
                            save(*args)
                            raise SystemExit()
                        with patch.object(room, "save_step", checkpoint), self.assertRaises(SystemExit):
                            room.start("Write a handoff", **options)
                        task_id, status, count = room.pending()
                        self.assertEqual((status,count,calls), ("ready",1,[("A","Proposal")]))
                        room.db.close()
                        room = Room(Path(temp))
                        self.assertEqual(room.pending()[0], task_id)
                        room.resume(task_id)
                    else:
                        task_id = room.start("Write a handoff", **options)
                self.assertEqual(calls, [("A","Proposal"),("B","Reply"),("C","Work"),("B","Final result")])
                self.assertEqual(room.db.execute("SELECT planner_id,executor_id,reviewer_id FROM task_members WHERE task_id=?",
                                                 (task_id,)).fetchone(), (1,3,2))
                self.assertEqual(room.task_outputs(task_id)[-1], ("B","Final result","Read handoff.txt: b'ready'"))
                self.assertEqual(room.db.execute("SELECT status FROM group_tasks WHERE id=?", (task_id,)).fetchone(), ("completed",))
            finally:
                room.db.close()
