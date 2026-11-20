import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from threading import RLock, Thread, Timer

STEPS = (
    (0, "Proposal", "plan", "Propose a short approach for the other member to check. Use no tools yet."),
    (1, "Reply", "plan", "Respond to the proposal. Give one specific check or correction. Use no tools yet."),
    (0, "Work", "build", "Do the task. Use the reply from the other member. Report what you did and any files."),
    (1, "Final result", "plan", "Check the work against the task. Inspect any claimed files. Give the result and state any unmet condition."),
)


def run_turn(folder, agent, prompt, show_tool):
    command = shutil.which("opencode")
    if not command:
        raise RuntimeError("Install OpenCode and add it to PATH.")
    native = Path(command).parent / "node_modules/opencode-ai/bin/opencode.exe"
    args = [str(native) if native.is_file() else command, "run", "--format", "json", "--agent", agent]
    with subprocess.Popen(args, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        timer = Timer(600, process.kill)
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
            if process.poll() is None:
                process.kill()
    reply = "\n\n".join(replies).strip()
    if failure or code or finish != "stop" or not reply:
        raise RuntimeError(failure or diagnostic or f"OpenCode stopped before it finished (exit {code}, reason {finish}).")
    return reply


class Room:
    def __init__(self, folder):
        self.work = folder / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.active = ""
        self.db = sqlite3.connect(folder / "group.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS people (name TEXT, role TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (name TEXT, kind TEXT, body TEXT)")
        last = self.db.execute("SELECT kind FROM messages ORDER BY rowid DESC LIMIT 1").fetchone()
        if last and last[0] not in ("Final result", "Stopped"):
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
            self.db.executemany("INSERT INTO people VALUES (?, ?)", people)

    def post(self, name, kind, body):
        with self.lock, self.db:
            self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", (name, kind, body))

    def start(self, task):
        with self.lock:
            people, _, active = self.snapshot()
            if active or len(people) != 2 or not task.strip():
                raise ValueError("Create the group and wait for the current task to finish before you send a task.")
            self.post("You", "Task", task.strip())
            self.active = f"{people[0][0]} · Proposal"
            Thread(target=self.run, args=(people,), daemon=True).start()

    def run(self, people):
        try:
            for index, phase, agent, instruction in STEPS:
                name, role = people[index]
                with self.lock:
                    self.active = f"{name} · {phase}"
                    _, messages, _ = self.snapshot()
                prompt = (f"You are {name}. Your role is {role}. Your teammate is {people[1 - index][0]}.\n"
                          f"Write one public reply in your own name. Use short sentences. {instruction}\n"
                          "The last Task is the user's current request. Use this shared chat history:\n"
                          + json.dumps(messages, ensure_ascii=False))
                reply = run_turn(self.work, agent, prompt, lambda detail: self.post(name, "Tool", detail))
                self.post(name, phase, reply)
        except Exception as exc:
            self.post(name, "Stopped", str(exc))
        finally:
            with self.lock:
                self.active = ""
