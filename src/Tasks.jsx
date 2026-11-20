import React, { useEffect, useState } from "react";
import { api } from "./api";

const stages = {
  engineering: ["Engineer 正在执行", "进行中"], reviewing: ["Reviewer 正在验收", "进行中"],
  completed: ["评审结束", "已完成 · PASS"], failed: ["评审结束", "未通过 · FAIL"], error: ["已停止", "执行错误"],
  NEEDS_HUMAN_REVIEW: ["两次评审结束", "需要人工复核 · NEEDS_HUMAN_REVIEW"],
};

function CreateTask({ bots, onCreated }) {
  const [engineer, setEngineer] = useState((bots.find(b => b.name === "Engineer") ?? bots[0])?.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (bots.length < 2) return <p>请先创建两个 Bot，再分配任务。</p>;
  const reviewers = bots.filter(b => b.id !== engineer);
  const option = b => <option key={b.id} value={b.id}>{b.name} · {b.role}</option>;
  async function submit(event) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const description = data.get("description").trim();
    const criteria = data.get("criteria").split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (!description || !criteria.length) { setError("请填写任务描述和至少一条验收条件。"); return; }
    setBusy(true);
    setError("");
    try {
      onCreated(await api("/tasks", "POST", { description, criteria, engineer_id: engineer, reviewer_id: Number(data.get("reviewer")) }));
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <form onSubmit={submit}>
    <label>任务描述<textarea name="description" rows="3" required /></label>
    <label>验收条件（每行一条）<textarea name="criteria" rows="3" required /></label>
    <div className="assignments">
      <label>Engineer<select value={engineer} onChange={e => setEngineer(Number(e.target.value))}>{bots.map(option)}</select></label>
      <label>Reviewer<select key={engineer} name="reviewer" defaultValue={(reviewers.find(b => b.name === "Reviewer") ?? reviewers[0]).id}>
        {reviewers.map(option)}</select></label>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <button className="primary" disabled={busy}>创建并执行</button>
  </form>;
}

function Attempt({ attempt, final }) {
  const { number, engineer_output, run, review, invalid_review } = attempt;
  return <section className="task-attempt" aria-label={`尝试 ${number}`}>
    <h3>第 {number} 次尝试 · {number === 1 ? "初次执行" : "修复"}</h3>
    <h4>Engineer 输出</h4><pre>{engineer_output ?? "等待 Engineer 输出"}</pre>
    {run && <details><summary>测试结果 · {run.passed ? "PASS" : "FAIL"}</summary>
      <pre>{[run.error, ...run.checks.map(c => `${c.passed ? "PASS" : "FAIL"} · ${c.test}\n${c.evidence}`),
        run.output].filter(Boolean).join("\n\n")}</pre></details>}
    <h4>{final ? "最终评审" : "Reviewer 反馈"}{review && " · " + review.verdict}</h4>
    <pre>{review ? JSON.stringify(review, null, 2) : (invalid_review ?? "尚无评审")}</pre>
  </section>;
}

function TaskDetail({ id, bots }) {
  const [task, setTask] = useState(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let mounted = true, timer;
    async function load() {
      try {
        const item = await api("/tasks/" + id);
        if (!mounted) return;
        setTask(item);
        setError("");
        if (["engineering", "reviewing"].includes(item.status)) timer = setTimeout(load, 750);
      } catch (err) { if (mounted) setError(err.message); }
    }
    load();
    return () => { mounted = false; clearTimeout(timer); };
  }, [id, refresh]);
  const name = botId => bots.find(b => b.id === botId)?.name;
  const attempts = task?.attempts ?? (task?.engineer_output ? [{ ...task, number: 1 }] : []);
  return <>
    {error && <p className="error" role="alert">{error} <button onClick={() => setRefresh(n => n + 1)}>刷新</button></p>}
    {task && <article className="task-detail">
      <h2>{task.description}</h2>
      <p>Engineer · {name(task.engineer_id)}　 Reviewer · {name(task.reviewer_id)}</p>
      <dl aria-live="polite"><dt>当前步骤</dt><dd>{stages[task.status][0]}</dd><dt>最终状态</dt><dd>{stages[task.status][1]}</dd></dl>
      <h3>验收条件</h3><ol>{task.criteria.map((c, i) => <li key={i}>{c}</li>)}</ol>
      {attempts.map((attempt, i) => <Attempt key={attempt.number} attempt={attempt}
        final={!["engineering", "reviewing"].includes(task.status) && i === attempts.length - 1} />)}
      {task.error && <p className="error" role="alert">{task.error}</p>}
    </article>}
  </>;
}

export default function Tasks({ bots }) {
  const [items, setItems] = useState(null);
  const [id, select] = useState("");
  const [error, setError] = useState("");
  function load() {
    setError("");
    api("/tasks").then(rows => { setItems(rows); select(rows[0]?.id ?? ""); }).catch(err => setError(err.message));
  }
  useEffect(load, []);
  function created(task) { setItems(rows => [task, ...rows]); select(task.id); }
  return <section className="tasks">
    <header><h1>任务</h1>{id !== "" && <button onClick={() => select("")}>新建任务</button>}</header>
    {error && <p className="error" role="alert">{error} <button onClick={load}>重试</button></p>}
    {items?.length > 0 && <div className="field"><label htmlFor="saved-task">已保存的任务</label>
      <select id="saved-task" value={id} onChange={e => select(e.target.value)}>
      <option value="">新建任务</option>{items.map(t => <option key={t.id} value={t.id}>#{t.id} · {t.description}</option>)}
    </select></div>}
    {items && (id === "" ? <CreateTask bots={bots} onCreated={created} /> : <TaskDetail key={id} id={id} bots={bots} />)}
  </section>;
}
