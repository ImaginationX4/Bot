import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room import Room

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


@unittest.skipUnless(AppTest, "requires Streamlit")
class PrivateDelivery(unittest.TestCase):
    def test_plan_from_selected_messages(self):
        old = "1. 整理输入\n2. 编写示例\n3. 发布计划"
        new = old.replace("编写示例", "验证样例")
        prompts = []
        def fake(folder, mode, prompt, show_tool, **kwargs):
            if prompt.startswith("Reply to the user"):
                return "同意生成 plan.md。"
            prompts.append(prompt)
            if mode == "plan":
                return json.dumps({"deliverable": "写入 plan.md", "criteria": old})
            Path(folder, "plan.md").write_text(prompt.split("完成条件\n")[1], encoding="utf-8")
            return "已交付 plan.md"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "app.py"
            shutil.copy(Path(__file__).resolve().parents[1] / "app.py", app)
            room = Room(root / "group-data")
            try:
                room.create([("A", "Owner"), ("C", "Writer")])
                with patch("room.run_turn", fake), patch("room.Room", return_value=room):
                    page = AppTest.from_file(str(app), default_timeout=30).run()
                    next(w for w in page.selectbox if w.label == "Conversation").set_value(2).run()
                    for message in ("不相关：ORANGE", "请创建 plan.md，步骤如下：\n" + old, "另一件事：BLUE"):
                        page.chat_input[0].set_value(message).run()
                    page.button(key="handoff_4").click().run()
                    self.assertEqual(page.selectbox(key="first_4").value, 4)
                    page.selectbox(key="first_4").set_value(3).run()
                    self.assertIn("选中范围 #3 – #4", [w.value for w in page.caption])
                    next(w for w in page.button if w.label == "生成任务草稿").click().run()
                    self.assertEqual(room.db.execute("SELECT count(*) FROM group_tasks").fetchone(), (0,))
                    selected = json.loads(prompts[0].split("\n", 1)[1])
                    self.assertEqual(selected, [["user","请创建 plan.md，步骤如下：\n"+old],["assistant","同意生成 plan.md。"]])
                    next(w for w in page.text_area if w.label == "完成条件").set_value(new)
                    next(w for w in page.button if w.label == "开始").click().run()
                    self.assertFalse(page.exception)
                    [(task, status, card, _)] = room.deliveries(2)
                    self.assertEqual(room.db.execute("SELECT count(*) FROM group_tasks").fetchone(), (1,))
                    self.assertEqual(status, "awaiting_confirmation")
                    self.assertEqual(prompts[1].split("task card:\n")[1], card)
                    self.assertEqual(card, "交付物\n写入 plan.md\n\n完成条件\n"+new)
                    self.assertEqual((room.solo_folder(task)/"plan.md").read_text(encoding="utf-8"), new)
                    self.assertIn(new.splitlines(), [w.value.splitlines() for w in page.code])
                    self.assertIn(f"任务 #{task} · 待确认", [w.value for w in page.subheader])
                    page.button(key=f"accept_{task}").click().run()
                    self.assertEqual(room.deliveries(2)[0][1], "completed")
                    self.assertEqual(len(prompts), 2)
            finally:
                room.db.close()
