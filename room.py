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

    def add_member(self, member):
        name, role = (field.strip() for field in member)
        with self.lock, self.db:
            members = self.members()
            if not members or not name or not role:
                raise ValueError("Enter a name and role for the new group member.")
            if any(name.casefold() == other.casefold() for _, other, _ in members):
                raise ValueError("Give each person a different name.")
            self.db.execute("INSERT INTO people(name,role) VALUES (?,?)", (name, role))

    def task_pair(self, executor_id, reviewer_id):
        members = {i: (name, role) for i, name, role in self.members()}
        if executor_id is None and reviewer_id is None:
            executor_id, reviewer_id = list(members)[:2]
        if executor_id == reviewer_id or executor_id not in members or reviewer_id not in members:
            raise ValueError("Choose two different group members.")
        return (executor_id, reviewer_id), [members[executor_id], members[reviewer_id]]

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
                WHERE t.status NOT IN ('completed','failed','cancelled') GROUP BY t.id ORDER BY t.id LIMIT 1""").fetchone()

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

    def start(self, task, command=None, max_rounds=3, total_seconds=3600, *, executor_id=None, reviewer_id=None, from_private=False):
        if command is not None and (not command.strip() or (max_rounds is not None and max_rounds < 1)):
            raise ValueError("Enter an acceptance command and at least one round.")
        if command is not None and total_seconds is not None and not (0 < total_seconds < float("inf")):
            raise ValueError("Enter a positive total time limit.")
        with self.lock:
            people, _, active = self.snapshot()
            if active or self.pending() or len(people) < 2 or not task.strip():
                raise ValueError("Create the group and finish the current task before sending another.")
            ids, pair = self.task_pair(executor_id, reviewer_id)
            with self.db:
                task_id = self.db.execute("INSERT INTO messages VALUES ('You','Task',?)", (task.strip(),)).lastrowid
                self.db.execute("INSERT INTO group_tasks VALUES (?, 'ready')", (task_id,))
                self.db.execute("INSERT INTO task_members VALUES (?,?,?)", (task_id, *ids))
                if from_private:
                    self.db.execute("INSERT INTO private_dispatch VALUES (?,?)", (task_id, ids[0]))
                if command is not None:
                    deadline = None if total_seconds is None else time.time() + total_seconds
                    self.db.execute("INSERT INTO ralph_tasks(task_id,command,max_rounds,deadline) VALUES (?,?,?,?)",
                                    (task_id, command, max_rounds or 0, deadline))
            self.launch(pair, task_id, 0)
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
            ids = self.db.execute("SELECT executor_id,reviewer_id FROM task_members WHERE task_id=?",
                                  (task_id,)).fetchone()
            _, pair = self.task_pair(*(ids or (None, None)))
            self.launch(pair, task_id, pending[2])

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
        remaining(deadline)
        with self.spawn(command, shell=True, cwd=self.work, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", start_new_session=os.name != "nt") as process:
            try:
                output, _ = process.communicate(timeout=min(ACCEPTANCE_TIMEOUT_SECONDS, remaining(deadline)))
            except (subprocess.TimeoutExpired, DeadlineExpired):
                terminate_tree(process)
                output, _ = process.communicate()
                remaining(deadline)
                reason = f"Acceptance timed out after {ACCEPTANCE_TIMEOUT_SECONDS} s."
                self.save_acceptance(task_id, number, limit, False, f"{reason}\n{output}", timed_out=True)
                return
        remaining(deadline)
        output = f"Exit code {process.returncode}\n{output}"
        self.save_acceptance(task_id, number, limit, process.returncode == 0, output)

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
                name, role = people[index]
                with self.lock, self.db:
                    self.check_stop()
                    self.db.execute("UPDATE group_tasks SET status='running' WHERE id=?", (task_id,))
                    self.active = f"{name} · {phase}"
                    start = self.db.execute("SELECT task_id FROM private_dispatch WHERE task_id=?",
                                            (task_id,)).fetchone()
                    messages = self.db.execute("SELECT name,kind,body FROM messages WHERE rowid>=? ORDER BY rowid",
                                               (start[0] if start else 0,)).fetchall()
                prompt = (f"You are {name}. Your role is {role}. Your teammate is {people[1 - index][0]}.\n"
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
