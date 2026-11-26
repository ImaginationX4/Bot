import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room


class PrivateChat(unittest.TestCase):
    def test_isolated_prompts_and_restart(self):
        marker = "C-ONLY-7F91"
        calls = []
        def fake(folder, mode, prompt, show_tool, env):
            self.assertEqual(mode, "plan")
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"], {"*": "deny"})
            context = json.loads(prompt.split("\n", 1)[1])
            calls.append(context)
            return context["history"][0][1]

        with tempfile.TemporaryDirectory() as directory, patch("room.run_turn", fake):
            root = Path(directory)
            room = Room(root)
            try:
                room.create([("A","Owner"), ("B","Reviewer")])
                room.add_member(("C","Writer"))
                room.post("A", "Final result", "group-only")
                _, (b, _, _), (c, _, _) = room.members()
                room.private_chat(c, marker)
                self.assertEqual([x["name"] for x in calls], ["C"])
                room.private_chat(b, "B-only")
                self.assertEqual(calls[1], {"name":"B","role":"Reviewer","history":[["user","B-only"]]})
                room.db.close()
                room = Room(root)
                self.assertEqual(room.private_history(c), [("user",marker), ("assistant",marker)])
                room.private_chat(c, "Repeat my first message")
                self.assertEqual([x["name"] for x in calls], ["C","B","C"])
                self.assertEqual(calls[2], {"name":"C","role":"Writer","history":[
                    ["user",marker], ["assistant",marker], ["user","Repeat my first message"]]})
                self.assertEqual(room.private_history(c)[-1], ("assistant",marker))
                self.assertEqual(room.db.execute("SELECT count(*) FROM group_tasks").fetchone(), (0,))
                self.assertEqual(room.snapshot()[1], [("A","Final result","group-only")])
            finally:
                room.db.close()


    def test_dispatch_from_private_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            room = Room(Path(directory))
            try:
                room.create([("A","Owner"), ("B","Reviewer")])
                room.add_member(("C","Writer"))
                room.post("A", "Reply", "UNRELATED-GROUP")
                marker = "C-PRIVATE-9E82"
                with patch("room.run_turn", return_value="private reply") as fake:
                    room.private_chat(3, marker)
                    self.assertEqual(fake.call_count, 1)
                    self.assertEqual(room.db.execute("SELECT count(*) FROM group_tasks").fetchone(), (0,))
                    fake.reset_mock()
                    fake.return_value = "public output"
                    with patch("room.Thread.start", lambda t: t.run()):
                        task = room.start("Draft public note", executor_id=3, reviewer_id=2, from_private=True)
                    prompts = [call.args[2] for call in fake.call_args_list]
                self.assertEqual([p.split(".")[0] for p in prompts], ["You are C","You are B"]*2)
                for p in prompts:
                    self.assertNotIn(marker, p)
                    self.assertNotIn("private reply", p)
                    self.assertNotIn("UNRELATED-GROUP", p)
                    self.assertIn("Draft public note", p)
                self.assertEqual(room.dispatched_tasks(3), [(task,"completed","Draft public note")])
                self.assertEqual(room.dispatched_tasks(2), [])
                self.assertEqual(len(room.task_outputs(task)), 4)
                self.assertEqual(room.db.execute("SELECT count(*) FROM group_tasks").fetchone(), (1,))
                self.assertEqual(room.private_history(3), [("user",marker),("assistant","private reply")])
            finally:
                room.db.close()
