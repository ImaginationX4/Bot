import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room

try:
    from streamlit import cache_resource
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


class TeamPlan(unittest.TestCase):
    @unittest.skipUnless(AppTest, "requires Streamlit")
    def test_original_and_swapped_assignments(self):
        for owners in ((2, 3), (3, 2)):
            with self.subTest(owners=owners):
                self.check_plan(owners)

    def check_plan(self, owners):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app.py"
            shutil.copy(Path(__file__).resolve().parents[1] / "app.py", app)
            room = Room(root / "group-data")
            room.create([("A", "规划"), ("B", "写操作步骤")])
            room.add_member(("C", "质量检查"))
            files = ("release_steps.md", "release_checklist.md")
            cards = [dict(owner_id=i, description=d, deliverable=f, criteria=c)
                     for i, d, f, c in zip(owners, ("编写发布步骤", "编写质量清单"), files, ("列出操作顺序", "列出检查项"))]
            calls = []
            def fake(folder, mode, prompt, show_tool, **kwargs):
                if mode == "plan":
                    context = json.loads(prompt.split("\n", 1)[1])
                    self.assertEqual(context["goal"], "准备一份发布材料")
                    self.assertEqual(context["planner"]["name"], "A")
                    self.assertEqual([m["role"] for m in context["candidates"]], ["写操作步骤","质量检查"])
                    calls.append("A")
                    return json.dumps(cards)
                owner = prompt.split(".")[0].removeprefix("You are ")
                card = cards[len(calls)-1]
                for key in ("description", "deliverable", "criteria"):
                    self.assertIn(card[key], prompt)
                calls.append(owner)
                (Path(folder) / card["deliverable"]).write_text(owner, encoding="utf8")
                return "交付完成"

            try:
                with patch("room.Room", return_value=room), patch("room.run_turn", fake):
                    cache_resource.clear()
                    page = AppTest.from_file(str(app), default_timeout=30).run()
                    next(w for w in page.text_area if w.label=="总目标").set_value("准备一份发布材料")
                    next(w for w in page.multiselect if w.label=="可用成员").set_value([2,3])
                    next(w for w in page.button if w.label=="生成分工计划").click().run()
                    self.assertFalse(page.exception)
                    self.assertEqual(calls, ["A"])
                    parent = room.team_plans()[0][0]
                    self.assertIsNone(room.team_child(parent, 1))
                    page.button(key=f"team_{parent}").click().run()
                    self.assertFalse(page.exception)
                    names = ["B" if i==2 else "C" for i in owners]
                    self.assertEqual(calls, ["A", *names])
                    for pos,(name,file) in enumerate(zip(names, files), 1):
                        child, status, _ = room.team_child(parent,pos)
                        self.assertEqual(status, "awaiting_confirmation")
                        self.assertEqual((room.solo_folder(child)/file).read_text(), name)
                        self.assertEqual(next(e for e in page.expander if e.label==file).code[0].value, name)
                    room.start_team(parent)
                    self.assertEqual(calls, ["A", *names])
            finally:
                cache_resource.clear()
                room.db.close()

    def test_rejects_outside_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            room = Room(Path(tmp))
            try:
                room.create([("A","规划"),("B","执行")])
                with patch("room.run_turn", return_value='[{"owner_id":1},{"owner_id":2}]'):
                    with self.assertRaisesRegex(ValueError, "候选名单"):
                        room.plan_team("发布", 1, [2])
                self.assertEqual(room.team_plans(), [])
            finally:
                room.db.close()

    @unittest.skipUnless(AppTest, "requires Streamlit")
    def test_reference_scoped_to_parent(self):
        original = b"\xef\xbb\xbfTEAM-ONLY-71A9\r\nsecond line\r\n"
        cards = [dict(owner_id=i, description="Write note", deliverable="note.md", criteria="Readable")
                 for i in (2, 3)]
        reads = []
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app.py"
            shutil.copy(Path(__file__).resolve().parents[1] / "app.py", app)
            room = Room(Path(tmp) / "group-data")
            room.create([("A","Planner"),("B","Writer")])
            room.add_member(("C","Reviewer"))
            def fake(folder, mode, prompt, show_tool, **kwargs):
                if mode == "plan":
                    return json.dumps(cards)
                base = Path(folder)
                marker = "参考文件（相对当前工作目录）："
                if with_file:
                    self.assertIn(marker, prompt)
                    relative = prompt.split(marker)[1]
                    self.assertEqual(relative, "reference/trial-notes.md")
                    self.assertEqual((base / relative).read_bytes(), original)
                    reads.append((base.name, relative))
                else:
                    self.assertNotIn(marker, prompt)
                    self.assertNotIn("trial-notes.md", prompt)
                    self.assertNotIn("TEAM-ONLY-71A9", prompt)
                    self.assertEqual(list(base.iterdir()), [])
                (base / "note.md").write_text("Delivered")
                return "Done"
            try:
                cache_resource.clear()
                with patch("room.Room", return_value=room), patch("room.run_turn", fake):
                    page = AppTest.from_file(str(app), default_timeout=30).run()
                    for parent, with_file in ((1, True), (2, False)):
                        upload = page.file_uploader[0]
                        if with_file:
                            upload.upload("trial-notes.md", original, "text/markdown")
                        else:
                            upload.clear()
                        next(w for w in page.text_area if w.label=="总目标").set_value(f"Trial {parent}")
                        page.multiselect[0].set_value([2,3])
                        next(w for w in page.button if w.label=="生成分工计划").click().run()
                        page.button(key=f"team_{parent}").click().run()
                        self.assertFalse(page.exception)
                        for pos in (1, 2):
                            child, status, outcome = room.team_child(parent, pos)
                            self.assertEqual(status, "awaiting_confirmation", outcome)
                            self.assertEqual([p.name for p in room.solo_files(child)], ["note.md"])
                    self.assertEqual(reads, [("1","reference/trial-notes.md"),("2","reference/trial-notes.md")])
                    self.assertNotIn("trial-notes.md", [w.label for w in page.expander])
            finally:
                cache_resource.clear()
                room.db.close()

    @unittest.skipUnless(AppTest, "requires Streamlit")
    def test_dependent_steps(self):
        import uuid
        for result in ("delivered", "missing", "failed"):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app = root / "app.py"
                shutil.copy(Path(__file__).resolve().parents[1] / "app.py", app)
                room = Room(root / "group-data")
                room.create([("A","Planner"),("B","Writer")])
                room.add_member(("C","Reviewer"))
                calls, markers = [], []
                cards = [
                    dict(owner_id=2, description="Write a manual", deliverable="manual.md", criteria="Readable"),
                    dict(owner_id=3, description="Review the manual", deliverable="review.md",
                         criteria="Quote evidence", inputs=["manual.md"]),
                ]
                def fake(folder, mode, prompt, show_tool, **kwargs):
                    if mode == "plan":
                        calls.append("A")
                        return json.dumps(cards)
                    owner = prompt.split(".")[0].removeprefix("You are ")
                    calls.append(owner)
                    if owner == "B":
                        if result == "failed":
                            raise RuntimeError("writer stopped")
                        markers.append(uuid.uuid4().hex)
                        filename = "manual.md" if result == "delivered" else "other.md"
                        (folder/filename).write_bytes(b"\xef\xbb\xbf" + markers[0].encode() + b"\r\n")
                    else:
                        self.assertNotIn(markers[0], prompt)
                        relative = prompt.split("第一步交付物（相对当前工作目录）：")[1]
                        self.assertEqual(relative, "inputs/manual.md")
                        data = (folder/relative).read_bytes()
                        (folder/"review.md").write_text("Reviewed: " + data.decode("utf-8-sig").strip())
                    return "Done"
                try:
                    cache_resource.clear()
                    with patch("room.Room", return_value=room), patch("room.run_turn", fake):
                        page = AppTest.from_file(str(app), default_timeout=30).run()
                        next(w for w in page.text_area if w.label=="总目标").set_value("Review handoff")
                        page.multiselect[0].set_value([2,3])
                        next(w for w in page.checkbox if w.label=="第二步需要第一步的交付物").check()
                        next(w for w in page.button if w.label=="生成分工计划").click().run()
                        self.assertIn("依赖第一步交付物：manual.md", [w.value for w in page.markdown])
                        self.assertEqual(calls, ["A"])
                        page.button(key="team_1").click().run()
                        self.assertFalse(page.exception)
                        child = room.team_child(1,2)
                        if result != "delivered":
                            self.assertEqual(calls, ["A","B"])
                            self.assertIsNone(child)
                            reason = "缺少指定交付物：manual.md" if result=="missing" else "第一步失败：writer stopped"
                            self.assertTrue(any(reason in w.value for w in page.error))
                            continue
                        self.assertEqual(calls, ["A","B","C"])
                        first = room.solo_folder(room.team_child(1,1)[0]) / "manual.md"
                        second = room.solo_folder(child[0])
                        self.assertEqual(first.read_bytes(), (second/"inputs/manual.md").read_bytes())
                        self.assertEqual([p.name for p in room.solo_files(child[0])], ["review.md"])
                        self.assertIn(markers[0], (second/"review.md").read_text())
                        self.assertNotIn(markers[0], room.team_plans()[0][3])
                        print(f"handoff inputs/manual.md marker={markers[0]} calls={calls}")
                finally:
                    cache_resource.clear()
                    room.db.close()

    def test_review_returns_once_to_writer(self):
        cards = [dict(owner_id=2, description="Original assignment", deliverable="manual.md", criteria="Usable"),
                 dict(owner_id=3, description="Review", deliverable="report.md", criteria="Cite sources", inputs=["manual.md"])]
        with tempfile.TemporaryDirectory() as tmp:
            room = Room(Path(tmp))
            calls = []
            def fake(folder, mode, prompt, show_tool, **kwargs):
                if mode == "plan":
                    return json.dumps(cards)
                actor = prompt.split(".")[0]
                calls.append(actor)
                if len(calls) == 1:
                    (folder/"manual.md").write_bytes(b"old")
                elif len(calls) == 2:
                    self.assertEqual((folder/"inputs/manual.md").read_bytes(), b"old")
                    (folder/"report.md").write_bytes(b"full review")
                else:
                    self.assertIn("Original assignment", prompt)
                    self.assertEqual((folder/"inputs/review/report.md").read_bytes(), b"full review")
                    self.assertEqual((folder/"inputs/original/manual.md").read_bytes(), b"old")
                    self.assertEqual((folder/"reference/source.txt").read_bytes(), b"facts")
                    (folder/"manual.md").write_bytes(b"new")
                return "reply"
            try:
                room.create([("A","Planner"),("B","Writer")])
                room.add_member(("C","Reviewer"))
                with patch("room.run_turn", fake):
                    parent = room.plan_team("Review",1,[2,3],("source.txt",b"facts"),revise=True)
                    room.start_team(parent)
                    room.start_team(parent)
                self.assertEqual(calls, ["You are B","You are C","You are B"])
                first = room.team_child(parent,1)[0]
                last,status,reply = room.team_child(parent,3)
                self.assertEqual((status,reply),("completed","reply"))
                self.assertEqual((room.solo_folder(first)/"manual.md").read_bytes(),b"old")
                self.assertEqual([f.read_bytes() for f in room.solo_files(last)], [b"new"])
            finally:
                room.db.close()
