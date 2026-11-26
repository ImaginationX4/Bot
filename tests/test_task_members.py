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
