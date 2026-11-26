import json
import os
import signal
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, RLock, Thread, Timer

ACCEPTANCE_TIMEOUT_SECONDS = 600

STEPS = (
    (0, "Proposal", "plan", "Propose a short approach for the other member to check. Use no tools yet."),
    (1, "Reply", "plan", "Respond to the proposal. Give one specific check or correction. Use no tools yet."),
    (0, "Work", "build", "Do the task. Use the reply from the other member. Report what you did and any files."),
    (1, "Final result", "plan", "Check the work against the task. Inspect any claimed files. Give the result and state any unmet condition."),
)


class TaskCancelled(Exception):
    pass


class DeadlineExpired(Exception):
    def __init__(self):
        super().__init__("Ralph task total time limit expired.")


def remaining(deadline):
    if deadline is None:
        return float("inf")
    seconds = deadline - time.time()
    if seconds <= 0:
        raise DeadlineExpired()
    return seconds


def terminate_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_turn(folder, agent, prompt, show_tool, deadline=None, spawn=None, env=None):
    command = shutil.which("opencode")
    if not command:
        raise RuntimeError("Install OpenCode and add it to PATH.")
    native = Path(command).parent / "node_modules/opencode-ai/bin/opencode.exe"
    args = [str(native) if native.is_file() else command, "run", "--format", "json", "--agent", agent]
    remaining(deadline)
    with (spawn or subprocess.Popen)(args, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          start_new_session=os.name != "nt", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        delay = 600 if deadline is None else max(0, min(600, deadline - time.time()))
        timer = Timer(delay, terminate_tree, args=(process,))
        timer.start()
        replies, failure, diagnostic, finish = [], "", "", ""
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    diagnostic = line.strip() or diagnostic
                    continue
                part = event.get("part", {})
                kind = event.get("type")
                if kind == "text":
                    replies.append(part["text"])
                elif kind == "tool_use":
                    state = part["state"]
                    title = state.get("title") or part["tool"]
                    detail = state.get("error") or state.get("output") or ""
                    show_tool(f"{part['tool']} · {state['status']} · {title}\n{str(detail)[:1500]}")
                elif kind == "step_finish":
                    finish = part.get("reason")
                elif kind == "error":
                    error = event.get("error") or {}
                    failure = error.get("data", {}).get("message") or error.get("name") or "OpenCode failed."
            code = process.wait()
        finally:
            timer.cancel()
            timer.join()
            if process.poll() is None:
                process.kill()
    remaining(deadline)
    reply = "\n\n".join(replies).strip()
    if failure or code or finish != "stop" or not reply:
        raise RuntimeError(failure or diagnostic or f"OpenCode stopped before it finished (exit {code}, reason {finish}).")
    return reply


class Room:
    def __init__(self, folder):
        self.work = folder / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.private_locks = {}
        self.dispatch_lock = Lock()
        self.active = ""
        self.stopping = False
        self.process = None
        self.worker = None
        self.db = sqlite3.connect(folder / "group.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY, name TEXT, role TEXT)")
        if "id" not in {row[1] for row in self.db.execute("PRAGMA table_info(people)")}:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("ALTER TABLE people RENAME TO legacy_people")
                self.db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, role TEXT)")
                self.db.execute("INSERT INTO people SELECT rowid,name,role FROM legacy_people")
                self.db.execute("DROP TABLE legacy_people")
        self.db.execute("CREATE TABLE IF NOT EXISTS private_dispatch (task_id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS private_messages (
            id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS task_members (
            task_id INTEGER PRIMARY KEY, executor_id INTEGER NOT NULL, reviewer_id INTEGER NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (name TEXT, kind TEXT, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS group_tasks (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        if "planner_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(task_members)")}:
            self.db.execute("ALTER TABLE task_members ADD COLUMN planner_id INTEGER")
        self.db.execute("""CREATE TABLE IF NOT EXISTS group_steps (
            task_id INTEGER, step INTEGER, output_message INTEGER NOT NULL, PRIMARY KEY(task_id, step))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS ralph_tasks (
            task_id INTEGER PRIMARY KEY, command TEXT NOT NULL, max_rounds INTEGER NOT NULL,
            round INTEGER NOT NULL DEFAULT 1, next_step INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS ralph_work (
            task_id INTEGER, round INTEGER, output_message INTEGER NOT NULL, PRIMARY KEY(task_id, round))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS acceptance (
            task_id INTEGER, round INTEGER, passed INTEGER NOT NULL, output TEXT NOT NULL,
            PRIMARY KEY(task_id, round))""")
        if "deadline" not in {row[1] for row in self.db.execute("PRAGMA table_info(ralph_tasks)")}:
            self.db.execute("ALTER TABLE ralph_tasks ADD COLUMN deadline REAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS team_plans (
            id INTEGER PRIMARY KEY, goal TEXT NOT NULL, planner INTEGER NOT NULL, cards TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS team_verdict (
            parent_id INTEGER PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS team_reference (
            parent_id INTEGER PRIMARY KEY, name TEXT NOT NULL, content BLOB NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS card_input (
            task_id INTEGER PRIMARY KEY, path TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS handoff_inputs (
            task_id INTEGER, path TEXT, PRIMARY KEY(task_id,path))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS solo_cards (
            task_id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, request_id TEXT UNIQUE NOT NULL,
            outcome TEXT NOT NULL DEFAULT '')""")
        with self.db:
            self.db.execute("""UPDATE group_tasks SET status='failed' WHERE status='running'
                AND id IN (SELECT task_id FROM solo_cards)""")
        self.pending()
        last = self.db.execute("SELECT kind FROM messages ORDER BY rowid DESC LIMIT 1").fetchone()
        if not self.db.execute("SELECT 1 FROM group_tasks").fetchone() and last and last[0] not in ("Final result", "Stopped"):
            self.post("Group", "Stopped", "The app closed during this task. Check the work folder before you continue.")

    def snapshot(self):
        with self.lock:
            people = self.db.execute("SELECT name, role FROM people ORDER BY rowid").fetchall()
            messages = self.db.execute("SELECT name, kind, body FROM messages ORDER BY rowid").fetchall()
            return people, messages, self.active

    def create(self, people):
        if len(people) != 2 or not all(name and role for name, role in people):
            raise ValueError("Enter both names and both roles.")
        if people[0][0].casefold() == people[1][0].casefold():
            raise ValueError("Give each person a different name.")
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM people").fetchone():
                raise ValueError("A group already exists. Reload the page to open it.")
            self.db.executemany("INSERT INTO people(name,role) VALUES (?, ?)", people)

    def members(self):
        with self.lock:
            return self.db.execute("SELECT id,name,role FROM people ORDER BY id").fetchall()

    def private_history(self, agent_id):
        with self.lock:
            return self.db.execute("""SELECT sender,body FROM private_messages
                WHERE agent_id=? ORDER BY id""", (agent_id,)).fetchall()

    def private_chat(self, agent_id, message):
        message = message.strip()
        if not message:
            raise ValueError("Enter a message.")
        with self.lock:
            member = self.db.execute("SELECT name,role FROM people WHERE id=?", (agent_id,)).fetchone()
            if member is None:
                raise ValueError("Choose an existing group member.")
            turn_lock = self.private_locks.setdefault(agent_id, Lock())
        with turn_lock:
            with self.lock, self.db:
                self.db.execute("INSERT INTO private_messages(agent_id,sender,body) VALUES (?,'user',?)",
                                (agent_id, message))
                context = {"name": member[0], "role": member[1], "history": self.private_history(agent_id)}
            prompt = "Reply to the user in character. Only converse.\n" + json.dumps(context, ensure_ascii=False)
            env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"*": "deny"}})}
            with TemporaryDirectory() as folder:
                reply = run_turn(folder, "plan", prompt, lambda _: None, env=env)
            with self.lock, self.db:
                self.db.execute("INSERT INTO private_messages(agent_id,sender,body) VALUES (?,'assistant',?)",
                                (agent_id, reply))

    def plan_team(self, goal, planner, candidates, reference=None, dependent=False, revise=False, final=False):
        revise = revise or final
        dependent = dependent or revise
        if reference is not None:
            name, _ = reference
            if (Path(name).name != name or Path(name).suffix.lower() not in (".txt", ".md")
                    or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)
                    or Path(name).is_reserved()):
                raise ValueError("请选择一个 .txt 或 .md 文件。")
        members = {i: {"id": i, "name": name, "role": role} for i, name, role in self.members()}
        if not goal.strip() or planner not in members or not candidates or not set(candidates) <= members.keys():
            raise ValueError("请填写总目标并选择规划者和可用成员。")
        if final and (len(set(candidates)) != 3 or planner in candidates):
            raise ValueError("最终验收需要规划者以外的三名候选成员。")
        stages = "开发、测试、最终验收三张卡，分别交给三名不同候选人；验收使用开发修订后的文件" if final else "两个串行子任务"
        context = {"goal": goal.strip(), "planner": members[planner], "candidates": [members[i] for i in candidates]}
        prompt = ("你是指定的规划者。根据目标和候选成员职责，自行分配" + stages + "。仅返回 JSON 数组。"
                  "每项含整数 owner_id，以及非空字符串 description、deliverable、criteria。\n"
                  + json.dumps(context, ensure_ascii=False))
        if dependent:
            prompt = ("第二步需要第一步的交付文件。第一张卡在交付物中明确列出要创建的文件名；"
                      "第二张卡增加 inputs 字段，列出它需要读取的第一步产物的工作目录相对路径（JSON 字符串数组）。" + prompt)
        if revise:
            prompt = "第二步审阅第一步原件，写出完整审阅报告，逐项列出问题、依据和修改建议。" + prompt
        with TemporaryDirectory() as folder:
            raw = run_turn(folder, "plan", prompt, lambda _: None,
                           env={**os.environ, "OPENCODE_CONFIG_CONTENT": '{"permission":{"*":"deny"}}'})
        cards = json.loads(raw)
        if not isinstance(cards, list) or len(cards) != (3 if final else 2):
            raise ValueError("任务卡数量与所选流程不符。")
        for card in cards:
            if (not isinstance(card, dict) or type(card.get("owner_id")) is not int
                    or card["owner_id"] not in candidates):
                raise ValueError("计划中的负责人必须在候选名单内。")
            if not all(isinstance(card.get(k), str) and card[k].strip()
                       for k in ("description", "deliverable", "criteria")):
                raise ValueError("每项分工都需要任务说明、交付物和完成条件。")
        for card in cards:
            card.pop("revise", None)
        if final:
            if len({c["owner_id"] for c in cards}) != 3:
                raise ValueError("开发、测试、最终验收必须分给不同成员。")
            cards[2].pop("inputs", None)
        if revise:
            cards[1]["revise"] = True
        cards[0].pop("inputs", None)
        if dependent:
            paths = cards[1].get("inputs")
            if not isinstance(paths, list) or not paths:
                raise ValueError("计划必须指定第二步需要的第一步交付文件。")
            for path in paths:
                if (not isinstance(path, str) or not path or Path(path).as_posix() != path
                        or Path(path).anchor or ".." in Path(path).parts
                        or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in path)):
                    raise ValueError("交接文件必须使用工作目录内的相对路径。")
            cards[1]["inputs"] = list(dict.fromkeys(paths))
        else:
            cards[1].pop("inputs", None)
        with self.lock, self.db:
            parent = self.db.execute("INSERT INTO team_plans(goal,planner,cards) VALUES (?,?,?)",
                                   (goal.strip(), planner, json.dumps(cards, ensure_ascii=False))).lastrowid
            if final:
                self.db.execute("INSERT INTO team_verdict VALUES (?, '待验收', '')", (parent,))
            if reference is not None:
                self.db.execute("INSERT INTO team_reference VALUES (?,?,?)", (parent, *reference))
            return parent

    def team_plans(self):
        with self.lock:
            return self.db.execute("SELECT id,goal,planner,cards FROM team_plans ORDER BY id DESC").fetchall()

    def team_child(self, parent, position):
        with self.lock:
            return self.db.execute("""SELECT c.task_id,t.status,c.outcome FROM solo_cards c
                JOIN group_tasks t ON c.task_id=t.id WHERE c.request_id=?""",
                                   (f"team:{parent}:{position}",)).fetchone()

    def team_inputs(self, parent, required):
        first = self.team_child(parent, 1)
        if not first:
            return [], "第一步尚未开始"
        task_id, status, outcome = first
        if status == "failed":
            return [], f"第一步失败：{outcome}"
        if status not in ("awaiting_confirmation", "completed"):
            return [], "第一步尚未交付"
        folder = self.solo_folder(task_id)
        outputs = {p.relative_to(folder).as_posix(): p for p in self.solo_files(task_id)}
        missing = [name for name in required if name not in outputs]
        if missing:
            return [], "第一步缺少指定交付物：" + "、".join(missing)
        return [(name, outputs[name]) for name in required], ""

    def start_team(self, parent):
        if not self.dispatch_lock.acquire(blocking=False):
            raise ValueError("已有分工任务正在派单。")
        try:
            with self.lock:
                reference = self.db.execute("SELECT name,content FROM team_reference WHERE parent_id=?", (parent,)).fetchone()
                row = self.db.execute("SELECT cards,goal FROM team_plans WHERE id=?", (parent,)).fetchone()
            if not row:
                raise ValueError("找不到该分工任务。")
            cards = json.loads(row[0])
            for position, card in enumerate(cards[:2], 1):
                inputs = []
                if card.get("inputs"):
                    if self.team_child(parent, position):
                        continue
                    inputs, reason = self.team_inputs(parent, card["inputs"])
                    if reason:
                        if len(cards) == 3:
                            with self.lock, self.db:
                                self.db.execute("UPDATE team_verdict SET status='验收失败',detail=? WHERE parent_id=?",
                                                (reason, parent))
                        return
                criteria = card["criteria"]
                if card.get("revise"):
                    criteria += "\n读取交接的原件，将完整审阅报告写入文件，每条列出问题、依据和修改建议。"
                self.start_card(card["owner_id"], card["deliverable"], criteria, "",
                                f"team:{parent}:{position}", description=card["description"], reference=reference, inputs=inputs)
            if cards[1].get("revise"):
                self.revise_team(parent, cards[0], reference)
            if len(cards) == 3:
                self.accept_team(parent, row[1], cards[2])
        finally:
            self.dispatch_lock.release()

    def revise_team(self, parent, original_card, reference):
        original = self.team_child(parent, 1)
        review = self.team_child(parent, 2)
        if not review or review[1] not in ("completed", "awaiting_confirmation") or self.team_child(parent, 3):
            return
        inputs = []
        expected = []
        for child, category in ((original, "original"), (review, "review")):
            folder = self.solo_folder(child[0])
            for file in self.solo_files(child[0]):
                name = file.relative_to(folder).as_posix()
                inputs.append((f"{category}/{name}", file))
                if category == "original":
                    expected.append(name)
        if not expected or not any(name.startswith("review/") for name, _ in inputs):
            raise ValueError("缺少原件或审阅报告，无法修订。")
        description = ("原任务卡：" + json.dumps(original_card, ensure_ascii=False)
                       + "\n审阅者回复：" + review[2]
                       + "\n阅读完整审阅报告、原件和参考资料。逐条将建议中的每个断言与资料原文对照，报告本身不作为证据。只有资料支持整个建议时才修改。"
                       + "一次观察只能支持该次现象，不能证明频率、唯一性或普遍规律。证据不足时保留原文，说明证据边界，不加入推测。")
        self.start_card(original_card["owner_id"], "在工作目录中按原相对路径输出修订版：" + "、".join(expected),
                        "最终回复按审阅报告顺序逐条写“已修改”或“保留原文”，说明依据。保留其他内容。", "",
                        f"team:{parent}:3", description=description, reference=reference, inputs=inputs,
                        revision_files=expected)

    def team_verdict(self, parent):
        with self.lock:
            return self.db.execute("SELECT status,detail FROM team_verdict WHERE parent_id=?", (parent,)).fetchone()

    def accept_team(self, parent, goal, card):
        if self.team_verdict(parent)[0] != "待验收":
            return
        revision = self.team_child(parent, 3)
        try:
            if not revision or revision[1] != "completed":
                raise ValueError("开发修订阶段没有成功交付。")
            folder = self.solo_folder(revision[0])
            inputs = [(p.relative_to(folder).as_posix(), p) for p in self.solo_files(revision[0])]
            criteria = (card["criteria"] + "\n根据原始目标独立设计用例，实际执行 inputs/ 中的修订版。"
                        "不要修改被测文件。记录真实命令和结果，同时写 acceptance.json，格式为非空 JSON 数组。"
                        "每项含字符串 input、command、expected、actual（后两项为输出文本），整数 exit_code、"
                        "expected_exit_code，以及布尔值 passed。错误路径用例可预期非零退出码。")
            tid = self.start_card(card["owner_id"], card["deliverable"], criteria, "", f"team:{parent}:4",
                                  description=f"原始目标：{goal}\n{card['description']}", inputs=inputs)
            check_folder = self.solo_folder(tid)
            child = self.team_child(parent, 4)
            if child[1] != "awaiting_confirmation":
                raise ValueError(child[2])
            report = check_folder / "acceptance.json"
            if report not in self.solo_files(tid):
                raise ValueError("缺少 acceptance.json")
            results = json.loads(report.read_text(encoding="utf-8-sig"))
            if not isinstance(results, list) or not results:
                raise ValueError("没有验收用例")
            for n, case in enumerate(results, 1):
                if (not isinstance(case, dict) or not all(isinstance(case.get(k), str)
                        for k in ("input", "command", "expected", "actual"))
                        or type(case.get("expected_exit_code")) is not int or type(case.get("exit_code")) is not int
                        or case.get("passed") is not True or case["expected"].strip() != case["actual"].strip()
                        or case["exit_code"] != case["expected_exit_code"]):
                    raise ValueError(f"验收用例 {n} 未通过或记录不完整。")
            status, detail = "验收通过", f"{len(results)} 项通过"
        except (ValueError, OSError) as error:
            status, detail = "验收失败", str(error)
        with self.lock, self.db:
            self.db.execute("UPDATE team_verdict SET status=?, detail=? WHERE parent_id=?", (status, detail, parent))
            child = self.team_child(parent, 4)
            if child:
                self.db.execute("UPDATE group_tasks SET status=? WHERE id=?",
                                ("completed" if status == "验收通过" else "failed", child[0]))

    def solo_folder(self, task_id):
        return self.work.parent / "deliveries" / str(task_id)

    def solo_files(self, task_id):
        folder = self.solo_folder(task_id)
        with self.lock:
            row = self.db.execute("SELECT path FROM card_input WHERE task_id=?", (task_id,)).fetchone()
            sources = {folder / name for name, in self.db.execute("SELECT path FROM handoff_inputs WHERE task_id=?", (task_id,))}
        if row:
            sources.add(folder / row[0])
        return sorted(p for p in folder.rglob("*") if p not in sources and p.is_file() and p.resolve().is_relative_to(folder.resolve())
                      and ".git" not in p.relative_to(folder).parts)

    def start_card(self, agent_id, deliverable, criteria, command, request_id, *, description=None, reference=None, inputs=(), revision_files=()):
        if not deliverable.strip() or not criteria.strip():
            raise ValueError("请填写交付物和完成条件。")
        card = f"交付物\n{deliverable.strip()}\n\n完成条件\n{criteria.strip()}"
        if description:
            card = f"任务说明\n{description}\n\n{card}"
        with self.lock, self.db:
            existing = self.db.execute("SELECT task_id FROM solo_cards WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return existing[0]
            member = self.db.execute("SELECT name,role FROM people WHERE id=?", (agent_id,)).fetchone()
            if not member or self.active or self.pending():
                raise ValueError("请选择已有成员，并等待当前任务结束。")
            task_id = self.db.execute("INSERT INTO messages VALUES ('You','Task',?)", (card,)).lastrowid
            self.db.execute("INSERT INTO group_tasks VALUES (?,'running')", (task_id,))
            self.db.execute("INSERT INTO solo_cards(task_id,agent_id,request_id) VALUES (?,?,?)",
                            (task_id, agent_id, request_id))
            if reference is not None:
                relative = Path("reference", reference[0]).as_posix()
                self.db.execute("INSERT INTO card_input VALUES (?,?)", (task_id, relative))
            self.db.executemany("INSERT INTO handoff_inputs VALUES (?,?)",
                                [(task_id, f"inputs/{name}") for name, _ in inputs])
            self.active = f"{member[0]} · Work"
        folder = self.solo_folder(task_id)
        status, outcome = "failed", "执行中断。"
        try:
            folder.mkdir(parents=True)
            prompt = f"You are {member[0]}. {member[1]}. Execute this task card:\n{card}"
            if reference is not None:
                (folder / "reference").mkdir()
                (folder / relative).write_bytes(reference[1])
                prompt += f"\n\n参考文件（相对当前工作目录）：{relative}"
            for name, source in inputs:
                relative_input = f"inputs/{name}"
                target = folder / relative_input
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                label = "修订输入" if revision_files else "第一步交付物"
                prompt += f"\n{label}（相对当前工作目录）：{relative_input}"
            outcome = run_turn(folder, "build", prompt, lambda _: None)
            if revision_files:
                outputs = {p.relative_to(folder).as_posix() for p in self.solo_files(task_id)}
                missing = set(revision_files) - outputs
                status = "failed" if missing else "completed"
                if missing:
                    outcome += "\n缺少修订版：" + "、".join(sorted(missing))
            elif command.strip():
                passed, output, _ = self.execute_check(command.strip(), None, folder)
                status = "completed" if passed else "failed"
                outcome += "\n" + output
            elif self.solo_files(task_id):
                status = "awaiting_confirmation"
            else:
                status, outcome = "failed", outcome + "\n工作目录中没有交付文件。"
        except Exception as error:
            status, outcome = "failed", str(error)
        finally:
            with self.lock, self.db:
                self.active = ""
                self.db.execute("UPDATE group_tasks SET status=? WHERE id=?", (status, task_id))
                self.db.execute("UPDATE solo_cards SET outcome=? WHERE task_id=?", (outcome, task_id))
        return task_id

    def add_member(self, member):
        name, role = (field.strip() for field in member)
        with self.lock, self.db:
            members = self.members()
            if not members or not name or not role:
                raise ValueError("Enter a name and role for the new group member.")
            if any(name.casefold() == other.casefold() for _, other, _ in members):
                raise ValueError("Give each person a different name.")
            self.db.execute("INSERT INTO people(name,role) VALUES (?,?)", (name, role))

    def task_assignment(self, executor_id, reviewer_id, planner_id=None):
        members = {i: (name, role) for i, name, role in self.members()}
        if executor_id is None and reviewer_id is None:
            executor_id, reviewer_id = list(members)[:2]
        if executor_id == reviewer_id or executor_id not in members or reviewer_id not in members:
            raise ValueError("Choose two different group members.")
        if planner_id is not None and planner_id not in members:
            raise ValueError("Choose an existing planner.")
        assigned = [members[executor_id], members[reviewer_id]]
        if planner_id is not None:
            assigned.append(members[planner_id])
        return (executor_id, reviewer_id, planner_id), assigned

    def post(self, name, kind, body):
        with self.lock, self.db:
            self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", (name, kind, body))

    def expire(self, task_id):
        with self.lock, self.db:
            self.db.execute("UPDATE group_tasks SET status='failed' WHERE id=?", (task_id,))
            self.db.execute("INSERT INTO messages VALUES ('Bot','Stopped',?)", (str(DeadlineExpired()),))

    def pending(self):
        with self.lock:
            row = self.db.execute("""SELECT t.id, t.status, COALESCE(r.next_step, COUNT(s.step)), r.deadline
                FROM group_tasks t LEFT JOIN group_steps s ON s.task_id=t.id
                LEFT JOIN ralph_tasks r ON r.task_id=t.id
                WHERE t.status NOT IN ('completed','failed','cancelled') AND NOT EXISTS (SELECT 1 FROM solo_cards WHERE task_id=t.id) GROUP BY t.id ORDER BY t.id LIMIT 1""").fetchone()

            if row and not self.active and row[3] is not None and time.time() >= row[3]:
                self.expire(row[0])
                return None
            return row[:3] if row else None

    def ralph_progress(self):
        with self.lock:
            return self.db.execute("""SELECT r.round, r.max_rounds, t.status, a.round, a.passed, a.output
                FROM group_tasks t JOIN ralph_tasks r ON r.task_id=t.id
                LEFT JOIN acceptance a ON a.task_id=t.id
                WHERE t.id=(SELECT MAX(id) FROM group_tasks) ORDER BY a.round DESC LIMIT 1""").fetchone()

    def start(self, task, command=None, max_rounds=3, total_seconds=3600, *, executor_id=None, reviewer_id=None, from_private=False, planner_id=None):
        if command is not None and (not command.strip() or (max_rounds is not None and max_rounds < 1)):
            raise ValueError("Enter an acceptance command and at least one round.")
        if command is not None and total_seconds is not None and not (0 < total_seconds < float("inf")):
            raise ValueError("Enter a positive total time limit.")
        with self.lock:
            people, _, active = self.snapshot()
            if active or self.pending() or len(people) < 2 or not task.strip():
                raise ValueError("Create the group and finish the current task before sending another.")
            if planner_id is not None and (from_private or command is not None):
                raise ValueError("Planner selection is available for ordinary group tasks.")
            ids, assigned = self.task_assignment(executor_id, reviewer_id, planner_id)
            with self.db:
                task_id = self.db.execute("INSERT INTO messages VALUES ('You','Task',?)", (task.strip(),)).lastrowid
                self.db.execute("INSERT INTO group_tasks VALUES (?, 'ready')", (task_id,))
                self.db.execute("INSERT INTO task_members(task_id,executor_id,reviewer_id,planner_id) VALUES (?,?,?,?)", (task_id, *ids))
                if from_private:
                    self.db.execute("INSERT INTO private_dispatch VALUES (?,?)", (task_id, ids[0]))
                if command is not None:
                    deadline = None if total_seconds is None else time.time() + total_seconds
                    self.db.execute("INSERT INTO ralph_tasks(task_id,command,max_rounds,deadline) VALUES (?,?,?,?)",
                                    (task_id, command, max_rounds or 0, deadline))
            self.launch(assigned, task_id, 0)
            return task_id

    def dispatched_tasks(self, agent_id):
        with self.lock:
            return self.db.execute("""SELECT t.id,t.status,m.body FROM private_dispatch d
                JOIN group_tasks t ON t.id=d.task_id JOIN messages m ON m.rowid=t.id
                WHERE d.agent_id=? ORDER BY t.id DESC""", (agent_id,)).fetchall()

    def task_outputs(self, task_id):
        with self.lock:
            return self.db.execute("""SELECT m.name,m.kind,m.body FROM group_steps s
                JOIN messages m ON m.rowid=s.output_message WHERE s.task_id=? ORDER BY s.step""",
                                   (task_id,)).fetchall()

    def resume(self, task_id, retry=False):
        with self.lock:
            pending = self.pending()
            if self.active or not pending or pending[0] != task_id:
                raise ValueError("This task is no longer waiting to resume.")
            if pending[1] == "running" and not retry:
                raise ValueError("Check the work folder before retrying this step.")
            ids = self.db.execute("SELECT executor_id,reviewer_id,planner_id FROM task_members WHERE task_id=?",
                                  (task_id,)).fetchone()
            _, assigned = self.task_assignment(*(ids or (None, None)))
            self.launch(assigned, task_id, pending[2])

    def launch(self, people, task_id, first):
        self.active = "Acceptance" if first == 4 else STEPS[first][1]
        self.worker = Thread(target=self.run, args=(people, task_id, first), daemon=True)
        self.worker.start()

    def check_stop(self):
        if self.stopping:
            raise TaskCancelled()

    @contextmanager
    def spawn(self, *args, **kwargs):
        with self.lock:
            self.check_stop()
            process = self.process = subprocess.Popen(*args, **kwargs)
        try:
            with process:
                yield process
        finally:
            with self.lock:
                self.process = None

    def stop(self, task_id):
        with self.lock:
            pending = self.pending()
            if not self.active or not pending or pending[0] != task_id or not self.ralph_progress():
                raise ValueError("This Ralph task is no longer running.")
            self.stopping = True
            if self.process is not None:
                terminate_tree(self.process)
            worker = self.worker
        worker.join()

    def save_step(self, task_id, step, name, phase, reply):
        with self.lock, self.db:
            self.check_stop()
            message = self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", (name, phase, reply)).lastrowid
            ralph = self.db.execute("SELECT round FROM ralph_tasks WHERE task_id=?", (task_id,)).fetchone()
            if ralph and step == 2:
                self.db.execute("INSERT INTO ralph_work VALUES (?,?,?)", (task_id, ralph[0], message))
                next_step = 4  # Run acceptance after this round's saved Work.
            else:
                self.db.execute("INSERT INTO group_steps VALUES (?, ?, ?)", (task_id, step, message))
                next_step = step + 1
            if ralph:
                self.db.execute("UPDATE ralph_tasks SET next_step=? WHERE task_id=?", (next_step, task_id))
            status = "completed" if step + 1 == len(STEPS) else "ready"
            self.db.execute("UPDATE group_tasks SET status=? WHERE id=?", (status, task_id))

    def save_acceptance(self, task_id, number, limit, passed, output, timed_out=False):
        with self.lock, self.db:
            self.check_stop()
            self.db.execute("INSERT INTO acceptance VALUES (?,?,?,?)", (task_id, number, passed, output))
            # Zero represents unlimited rounds in the existing NOT NULL column.
            exhausted = timed_out or (not passed and 0 < limit <= number)
            next_round = number if passed or exhausted else number + 1
            self.db.execute("UPDATE ralph_tasks SET round=?,next_step=? WHERE task_id=?",
                            (next_round, 3 if passed else 2, task_id))
            self.db.execute("UPDATE group_tasks SET status=? WHERE id=?",
                            ("failed" if exhausted else "ready", task_id))
            verdict = "Passed" if passed else "Failed"
            self.db.execute("INSERT INTO messages VALUES ('Bot','Acceptance',?)",
                            (f"Round {number}: {verdict}\n{output}",))

    def run_acceptance(self, task_id, deadline):
        with self.lock:
            command, number, limit = self.db.execute(
                "SELECT command,round,max_rounds FROM ralph_tasks WHERE task_id=?", (task_id,)).fetchone()
            self.active = f"Acceptance · Round {number}"
        passed, output, timed_out = self.execute_check(command, deadline)
        self.save_acceptance(task_id, number, limit, passed, output, timed_out)

    def execute_check(self, command, deadline, folder=None):
        remaining(deadline)
        with self.spawn(command, shell=True, cwd=folder or self.work, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", start_new_session=os.name != "nt") as process:
            try:
                output, _ = process.communicate(timeout=min(ACCEPTANCE_TIMEOUT_SECONDS, remaining(deadline)))
            except (subprocess.TimeoutExpired, DeadlineExpired):
                terminate_tree(process)
                output, _ = process.communicate()
                remaining(deadline)
                reason = f"Acceptance timed out after {ACCEPTANCE_TIMEOUT_SECONDS} s."
                return False, f"{reason}\n{output}", True
        remaining(deadline)
        output = f"Exit code {process.returncode}\n{output}"
        return process.returncode == 0, output, False

    def run(self, people, task_id, first):
        try:
            step = first
            with self.lock:
                row = self.db.execute("SELECT deadline FROM ralph_tasks WHERE task_id=?", (task_id,)).fetchone()
            deadline = row[0] if row else None
            while True:
                remaining(deadline)
                if step == 4:
                    self.run_acceptance(task_id, deadline)
                    pending = self.pending()
                    if not pending:
                        break
                    step = pending[2]
                    continue
                index, phase, agent, instruction = STEPS[step]
                if len(people) == 3:
                    executor, reviewer, planner = people
                    name, role = planner if step == 0 else people[index]
                    team = f"Planner: {planner[0]}. Executor: {executor[0]}. Reviewer: {reviewer[0]}."
                else:
                    name, role = people[index]
                    team = f"Your teammate is {people[1 - index][0]}."
                with self.lock, self.db:
                    self.check_stop()
                    self.db.execute("UPDATE group_tasks SET status='running' WHERE id=?", (task_id,))
                    self.active = f"{name} · {phase}"
                    start = self.db.execute("SELECT task_id FROM private_dispatch WHERE task_id=?",
                                            (task_id,)).fetchone()
                    messages = self.db.execute("SELECT name,kind,body FROM messages WHERE rowid>=? ORDER BY rowid",
                                               (start[0] if start else 0,)).fetchall()
                prompt = (f"You are {name}. Your role is {role}. {team}\n"
                          f"Write one public reply in your own name. Use short sentences. {instruction}\n"
                          "The last Task is the user's current request. Use this shared chat history:\n"
                          + json.dumps(messages, ensure_ascii=False))
                options = {"deadline": deadline} if deadline is not None else {}
                if row:
                    options["spawn"] = self.spawn
                reply = run_turn(self.work, agent, prompt, lambda detail: self.post(name, "Tool", detail), **options)
                remaining(deadline)
                self.save_step(task_id, step, name, phase, reply)
                pending = self.pending()
                if not pending:
                    break
                step = pending[2]
        except TaskCancelled:
            pass
        except Exception as exc:
            with self.lock:
                if not self.stopping:
                    if isinstance(exc, DeadlineExpired) or (deadline is not None and time.time() >= deadline):
                        self.expire(task_id)
                    else:
                        self.post("Bot" if step == 4 else name, "Stopped", str(exc))
        finally:
            with self.lock, self.db:
                if self.stopping:
                    self.db.execute("UPDATE group_tasks SET status='cancelled' WHERE id=?", (task_id,))
                    self.stopping = False
                self.active = ""
