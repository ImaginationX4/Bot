import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from room import Room

CRITERIA = ["Names the function add", "Returns the sum"]
RESULT = "def add(a, b):\n    return a + b"
CHECKS = [{"criterion": CRITERIA[0], "status": "PASS", "evidence": "The definition says def add(a, b)."},
          {"criterion": CRITERIA[1], "status": "PASS", "evidence": "The function returns a + b."}]


def review(checks=CHECKS, verdict="PASS", issues=()):
    return json.dumps({"verdict": verdict, "checks": checks, "blocking_issues": list(issues)})


class TaskFlow(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(server, "DATA", folder))
        self.client = self.enterContext(TestClient(server.app))
        for name in ("Builder", "Auditor"):
            self.client.post("/api/bots", json={"name": name, "role": name, "system_prompt": name + " instructions"})
        self.spec = {"description": "Write an addition function", "criteria": CRITERIA, "engineer_id": 1, "reviewer_id": 2}

    def run_case(self, replies):
        with patch.object(server, "model_reply", side_effect=replies) as model:
            response = self.client.post("/api/tasks", json=self.spec)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "engineering")
        return self.client.get("/api/tasks/" + str(response.json()["id"])).json(), model.call_count

    def test_order_identity_memory_and_durable_pass(self):
        for bot in ("Builder", "Auditor"):
            bot_id = 1 if bot == "Builder" else 2
            with patch.object(server, "model_reply", return_value=bot + " private answer"):
                self.client.post(f"/api/bots/{bot_id}/chat", json={"text": bot + " private question"})
        seen = []
        def reply(bot, turns, prompt):
            with server.database() as db:
                saved = server.task_record(db, 1)
            seen.append((bot, turns, json.loads(prompt), saved))
            return RESULT if len(seen) == 1 else review(CHECKS[::-1])
        task, count = self.run_case(reply)
        self.assertEqual((count, task["status"], task["review"]["verdict"]), (2, "completed", "PASS"))
        payload = {"task": self.spec["description"], "criteria": CRITERIA}
        for index, (name, stage) in enumerate((("Builder", "engineering"), ("Auditor", "reviewing"))):
            bot, memory, sent, saved = seen[index]
            self.assertEqual((bot["name"], bot["role"], saved["status"]), (name, name, stage))
            self.assertIn(name + " instructions", bot["system_prompt"])
            self.assertEqual(memory, [{"question": name + " private question", "answer": name + " private answer"}])
            self.assertEqual(self.client.get(f"/api/bots/{index + 1}/chat").json(), memory)
            self.assertEqual(sent, payload if index == 0 else {**payload, "engineer_result": RESULT})
            self.assertEqual(saved["engineer_output"], None if index == 0 else RESULT)
        with TestClient(server.app) as reopened:
            self.assertEqual(reopened.get("/api/tasks/1").json(), task)
            self.assertEqual(reopened.get("/api/tasks").json()[0]["status"], "completed")

    def test_fail_is_terminal(self):
        checks = [CHECKS[0], {**CHECKS[1], "status": "FAIL", "evidence": "The body is empty."}]
        raw = review(checks, "FAIL", ["Missing sum"])
        task, count = self.run_case(["def add(a, b): pass", raw])
        self.assertEqual((count, task["status"], task["review"]), (2, "failed", json.loads(raw)))

    def test_json_code_block_is_validated_and_saved_as_an_object(self):
        task, count = self.run_case([RESULT, "```json\n" + review() + "\n```"])
        self.assertEqual((count, task["status"], task["review"]), (2, "completed", json.loads(review())))

    def test_invalid_reviews_cannot_complete(self):
        for raw in ("not JSON", "Preamble\n```json\n" + review() + "\n```", "```json\nnot JSON\n```",
                    review(CHECKS[:1]), review([CHECKS[0]] * 2),
                    review(CHECKS + [{**CHECKS[1], "criterion": "Invented criterion"}]),
                    review([CHECKS[0], {**CHECKS[1], "evidence": " "}]),
                    review([CHECKS[0], {**CHECKS[1], "status": "FAIL"}]),
                    review(issues=["Unresolved issue"]), review(verdict="FAIL")):
            with self.subTest(raw=raw):
                task, count = self.run_case([RESULT, raw])
                self.assertEqual((count, task["status"], task["review"], task["invalid_review"]), (2, "error", None, raw))
                self.assertEqual(task["engineer_output"], RESULT)

    def test_provider_failure_ends_execution(self):
        for replies, step, result in (([RuntimeError("offline")], "Engineer", None),
                                      ([RESULT, RuntimeError("offline")], "Reviewer", RESULT)):
            with self.subTest(step=step):
                task, count = self.run_case(replies)
                self.assertEqual((count, task["status"], task["engineer_output"]), (len(replies), "error", result))
                self.assertIn(step, task["error"])
                self.assertIsNone(task["review"])

    def test_invalid_task_never_runs(self):
        with patch.object(server, "model_reply") as model:
            for fields, code in (({"reviewer_id": 1}, 422), ({"engineer_id": 99}, 404),
                                 ({"criteria": []}, 422), ({"criteria": [" "]}, 422), ({"description": " "}, 422)):
                self.assertEqual(self.client.post("/api/tasks", json={**self.spec, **fields}).status_code, code)
            model.assert_not_called()
        self.assertEqual(self.client.get("/api/tasks").json(), [])

    def test_concurrent_submission_and_second_server(self):
        entered, release = Event(), Event()
        def reply(bot, turns, prompt):
            if bot["id"] == 1:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Test did not release Engineer")
                return RESULT
            return review()
        with patch.object(server, "model_reply", side_effect=reply) as model, ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.client.post, "/api/tasks", json=self.spec)
            try:
                self.assertTrue(entered.wait(5))
                with TestClient(server.app) as other:
                    self.assertEqual(other.get("/api/tasks/1").json()["status"], "engineering")
                    self.assertEqual(other.post("/api/tasks", json=self.spec).status_code, 409)
            finally:
                release.set()
            self.assertEqual(first.result(5).status_code, 201)
            self.assertEqual(model.call_count, 2)
        self.assertEqual([t["status"] for t in self.client.get("/api/tasks").json()], ["completed"])

    def test_interrupted_review_is_preserved_without_replay(self):
        with server.database() as db:
            db.execute("""INSERT INTO tasks(description,criteria,engineer_id,reviewer_id,status,engineer_output)
                          VALUES ('Work',?,1,2,'reviewing',?)""", (json.dumps(CRITERIA), RESULT))
        with patch.object(server, "model_reply") as model, TestClient(server.app) as reopened:
            task = reopened.get("/api/tasks/1").json()
            self.assertEqual((task["status"], task["engineer_output"], task["review"]), ("error", RESULT, None))
            model.assert_not_called()

    def test_original_group_flow(self):
        with TemporaryDirectory() as folder:
            group = Room(Path(folder))
            try:
                people = [("Alice", "Owner"), ("Bob", "Reviewer")]
                group.create(people)
                group.post("You", "Task", "Write a greeting")
                with patch("room.run_turn", return_value="Hello") as model:
                    group.run(people)
                self.assertEqual([m[1] for m in group.snapshot()[1]], ["Task", "Proposal", "Reply", "Work", "Final result"])
                self.assertEqual(model.call_count, 4)
            finally:
                group.db.close()
