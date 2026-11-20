import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
app = FastAPI(title="GrokBot")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Profile(BaseModel):
    name: Text
    role: Text
    system_prompt: Text


class Question(BaseModel):
    text: Text


@contextmanager
def database():
    with closing(sqlite3.connect(DATA / "grokbot.sqlite3")) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        yield db


with database() as db:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, system_prompt TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            bot_id INTEGER NOT NULL REFERENCES bots(id), seq INTEGER NOT NULL,
            question TEXT NOT NULL, answer TEXT NOT NULL, PRIMARY KEY (bot_id, seq)
        );
    """)


def get_bot(db, bot_id):
    bot = db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    if bot is None:
        raise HTTPException(404, "找不到这个 Bot。")
    return dict(bot)


@app.get("/api/bots")
def list_bots():
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM bots ORDER BY id")]


@app.post("/api/bots", status_code=201)
def create_bot(profile: Profile):
    with database() as db:
        cursor = db.execute("INSERT INTO bots (name, role, system_prompt) VALUES (:name, :role, :system_prompt)",
                            profile.model_dump())
        return get_bot(db, cursor.lastrowid)


@app.put("/api/bots/{bot_id}")
def edit_bot(bot_id: int, profile: Profile):
    with database() as db:
        db.execute("UPDATE bots SET name=:name, role=:role, system_prompt=:system_prompt WHERE id=:id",
                   {**profile.model_dump(), "id": bot_id})
        return get_bot(db, bot_id)


def read_turns(db, bot_id):
    rows = db.execute("SELECT question, answer FROM turns WHERE bot_id = ? ORDER BY seq", (bot_id,))
    return [dict(row) for row in rows]


@app.get("/api/bots/{bot_id}/chat")
def history(bot_id: int):
    with database() as db:
        get_bot(db, bot_id)
        return read_turns(db, bot_id)


def model_reply(bot, turns, question):
    base = os.getenv("ANTHROPIC_BASE_URL")
    token = os.getenv("ANTHROPIC_AUTH_TOKEN")
    model = os.getenv("ANTHROPIC_MODEL") or os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
    if not all((base, token, model)):
        raise HTTPException(503, "请在后端设置模型地址、令牌和模型名，再重启服务。")
    messages = []
    for turn in turns:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})
    system = f"你的名字是 {bot['name']}。你的角色是 {bot['role']}。\n{bot['system_prompt']}"
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/v1/messages", timeout=httpx.Timeout(120, connect=10),
            headers={"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": 4096, "system": system, "messages": messages},
        )
        response.raise_for_status()
        answer = "\n".join(item["text"] for item in response.json()["content"] if item["type"] == "text").strip()
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, f"模型服务返回 HTTP {error.response.status_code}，请检查后端配置后重试。") from error
    except (httpx.RequestError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(502, "模型请求失败，请稍后重试。") from error
    if not answer:
        raise HTTPException(502, "模型没有返回文本，请重试。")
    return answer


@app.post("/api/bots/{bot_id}/chat", status_code=201)
def chat(bot_id: int, question: Question):
    with database() as db:
        bot = get_bot(db, bot_id)
        turns = read_turns(db, bot_id)
    answer = model_reply(bot, turns, question.text)
    try:
        with database() as db:
            db.execute("INSERT INTO turns (bot_id, seq, question, answer) VALUES (?, ?, ?, ?)",
                       (bot_id, len(turns) + 1, question.text, answer))
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "对话已在其他窗口更新，请刷新后重新发送。") from error
    return {"question": question.text, "answer": answer}


app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="web")
