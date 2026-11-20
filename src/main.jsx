import React, { Fragment, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api } from "./api";
import Tasks from "./Tasks";
import "./style.css";

function Avatar({ bot }) {
  return <span className={"avatar tone-" + bot.id % 3} aria-hidden="true">{Array.from(bot.name)[0]}</span>;
}

function Profile({ bot, busy, setBusy, onSave, onCancel }) {
  const [error, setError] = useState("");
  async function save(event) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    if (Object.values(values).some(value => !value.trim())) {
      setError("请填写名称、角色和系统提示词。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      onSave(await api(bot ? "/bots/" + bot.id : "/bots", bot ? "PUT" : "POST", values));
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <section className="profile">
    <h1>{bot ? "编辑档案" : "创建 Bot"}</h1>
    <form onSubmit={save}>
      <label>名称<input name="name" defaultValue={bot?.name} placeholder="例如 Researcher" required /></label>
      <label>角色<input name="role" defaultValue={bot?.role} placeholder="例如研究员" required /></label>
      <div className="field"><label htmlFor="system-prompt">系统提示词</label>
        <textarea id="system-prompt" name="system_prompt" defaultValue={bot?.system_prompt} rows="5"
          placeholder="说明这个 Bot 应该怎样回答问题" required /></div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions"><button className="primary" disabled={busy}>保存 Bot</button>
        {onCancel && <button type="button" disabled={busy} onClick={onCancel}>取消</button>}</div>
    </form>
  </section>;
}

function Message({ name, text, mine }) {
  return <article className={mine ? "message mine" : "message"}>
    <strong>{name}</strong><div className="bubble">{text}</div>
  </article>;
}

function Chat({ bot, busy, setBusy, onEdit }) {
  const path = "/bots/" + bot.id + "/chat";
  const [turns, setTurns] = useState(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const scroll = useRef(null);
  function load() {
    setError("");
    api(path).then(setTurns).catch(err => setError(err.message));
  }
  useEffect(load, [path]);
  useEffect(() => {
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [turns, busy]);
  async function send(event) {
    event.preventDefault();
    if (busy || !turns || !draft.trim()) return;
    setBusy(true);
    setError("");
    try {
      const turn = await api(path, "POST", { text: draft.trim() });
      setTurns(previous => [...previous, turn]);
      setDraft("");
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <section className="chat">
    <header><Avatar bot={bot} /><div className="identity"><h1>{bot.name}</h1><p>{bot.role}</p></div>
      <button onClick={onEdit} disabled={busy}>编辑档案</button></header>
    <div className="transcript" ref={scroll} role="log" aria-label="聊天记录">
      {!turns && !error && <p className="hint">正在读取聊天记录…</p>}
      {turns?.length === 0 && !busy && <p className="hint">给 {bot.name} 发第一条消息。</p>}
      {turns?.map((turn, i) => <Fragment key={i}>
        <Message name="你" text={turn.question} mine /><Message name={bot.name} text={turn.answer} />
      </Fragment>)}
      {busy && <><Message name="你" text={draft.trim()} mine /><p role="status">{bot.name} 正在回复…</p></>}
    </div>
    <form className="composer" onSubmit={send}>
      {error && <p className="error" role="alert">{error} {!turns && <button type="button" onClick={load}>重试</button>}</p>}
      <div className="input-row"><textarea aria-label="消息" placeholder={"给 " + bot.name + " 发消息"}
        rows="2" value={draft} disabled={busy || !turns} onChange={e => setDraft(e.target.value)} />
        <button className="primary" disabled={busy || !turns || !draft.trim()}>发送 ↑</button></div>
    </form>
  </section>;
}

function App() {
  const [bots, setBots] = useState(null);
  const [selected, setSelected] = useState(null);
  const [editing, setEditing] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(location.hash === "#tasks");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bot = bots?.find(item => item.id === selected);
  useEffect(() => {
    const followHash = () => setTasksOpen(location.hash === "#tasks");
    window.addEventListener("hashchange", followHash);
    return () => window.removeEventListener("hashchange", followHash);
  }, []);
  function open(id) { setSelected(id); setEditing(false); if (location.hash) location.hash = ""; }
  function load() {
    setError("");
    api("/bots").then(items => { setBots(items); setSelected(items[0]?.id ?? null); }).catch(err => setError(err.message));
  }
  useEffect(load, []);
  function saved(item) {
    setBots(items => bot ? items.map(old => old.id === item.id ? item : old) : [...items, item]);
    open(item.id);
  }
  return <div className="workspace">
    <aside><div className="brand"><span className="mark" aria-hidden="true">••</span>GrokBot</div>
      <button className="task-link" disabled={busy} aria-current={tasksOpen ? "page" : undefined}
        onClick={() => { location.hash = "tasks"; }}>任务</button>
      <div className="list-title"><h2>我的 Bot</h2><span>{bots?.length ?? "…"}</span></div>
      <button className="add" onClick={() => open(null)} disabled={busy || !bots}>＋ 创建 Bot</button>
      <nav aria-label="Bot 列表">{bots?.map(item => <button key={item.id} disabled={busy}
        aria-current={!tasksOpen && selected === item.id ? "page" : undefined} onClick={() => open(item.id)}>
        <Avatar bot={item} /><span className="nav-copy"><strong>{item.name}</strong><small>{item.role}</small></span>
      </button>)}</nav>
    </aside>
    <main>{!bots ? <div className="profile">{error
      ? <p className="error" role="alert">{error} <button onClick={load}>重试</button></p>
      : <p>正在读取 Bot…</p>}</div>
      : tasksOpen ? <Tasks bots={bots} />
      : bot && !editing ? <Chat key={bot.id} bot={bot} busy={busy} setBusy={setBusy} onEdit={() => setEditing(true)} />
      : <Profile key={selected ?? "new"} bot={bot} busy={busy} setBusy={setBusy} onSave={saved}
          onCancel={bots.length ? () => open(bot?.id ?? bots[0].id) : null} />}</main>
  </div>;
}

createRoot(document.getElementById("root")).render(<App />);
