import json
import os
from uuid import uuid4
from pathlib import Path

import streamlit as st

from room import Room

st.set_page_config(page_title="Group chat", page_icon="💬")


@st.cache_resource
def open_room():
    return Room(Path(__file__).resolve().parent / "group-data")


def submit(action, value):
    try:
        action(value)
    except ValueError as error:
        st.error(str(error))
    else:
        st.rerun()


room = open_room()
people, _, _ = room.snapshot()
if not people:
    st.title("Create your group")
    with st.form("group"):
        members = []
        for column, job, default in zip(st.columns(2), ("Task owner", "Reviewer"), ("Mei", "Bo")):
            with column:
                st.subheader(job)
                name = st.text_input("Name", default, key=f"name_{job}", max_chars=60)
                role = st.text_area("Role", key=f"role_{job}", placeholder="Describe this person's role.")
                members.append((name.strip(), role.strip()))
        if st.form_submit_button("Create group", type="primary"):
            submit(room.create, members)
    st.stop()

with st.sidebar:
    names = {i: name for i, name, _ in room.members()}
    selected = st.selectbox("Conversation", [None, *names],
                            format_func=lambda i: "Group chat" if i is None else names[i])
    st.header("Group members")
    for name, role in people:
        st.subheader(name)
        st.text(role)
    with st.form("add_member", clear_on_submit=True):
        name = st.text_input("New member name", max_chars=60)
        role = st.text_area("New member role")
        if st.form_submit_button("Add member"):
            submit(room.add_member, (name, role))
    if st.button("Open work folder"):
        os.startfile(room.work)

def show_files(task_id):
    for path in room.solo_files(task_id):
        label = str(path.relative_to(room.solo_folder(task_id)))
        data = path.read_bytes()
        with st.expander(label):
            try:
                st.code(data.decode("utf-8"), language=None)
            except UnicodeDecodeError:
                st.caption(f"{len(data)} bytes")
            st.download_button("下载", data, file_name=path.name, key=f"file_{task_id}_{label}")


if selected is not None:
    st.title(f"Private chat · {names[selected]}")
    history = room.private_messages(selected)
    end_key = f"handoff_end_{selected}"
    draft_key = f"handoff_draft_{selected}"
    for msg_id, sender, body in history:
        with st.chat_message(sender):
            st.markdown(body)
        if st.button(f"交给 {names[selected]} 执行", key=f"handoff_{msg_id}"):
            st.session_state[end_key] = msg_id
            st.session_state.pop(draft_key, None)
            st.session_state.pop(f"first_{msg_id}", None)
    last = st.session_state.get(end_key)
    if last is not None:
        candidates = {i: (sender, body) for i, sender, body in history if i <= last}
        first = st.selectbox("从哪条消息开始", list(candidates), index=len(candidates)-1, key=f"first_{last}",
                             format_func=lambda i: f"#{i} {candidates[i][1][:50]}")
        st.caption(f"选中范围 #{first} – #{last}")
        for i, (sender, body) in candidates.items():
            if i >= first:
                st.text(f"{names[selected] if sender == 'assistant' else '你'}：{body}")
        if st.button("生成任务草稿"):
            try:
                draft = room.draft_card(selected, first, last)
                st.session_state[draft_key] = (first, last, uuid4().hex, draft)
            except (OSError, ValueError, RuntimeError) as error:
                st.error(str(error))
        saved = st.session_state.get(draft_key)
        if saved and saved[:2] == (first, last):
            _, _, token, draft = saved
            with st.form(f"card_{token}"):
                deliverable = st.text_area("交付物", draft["deliverable"], key=f"output_{token}")
                criteria = st.text_area("完成条件", draft["criteria"], key=f"criteria_{token}")
                command = st.text_input("验收命令（可选）", key=f"check_{token}")
                if st.form_submit_button("开始"):
                    try:
                        room.start_card(selected, deliverable, criteria, command, token)
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        del st.session_state[draft_key], st.session_state[end_key]
                        st.rerun()
    for task_id, status, card, outcome in room.deliveries(selected):
        st.subheader(f"任务 #{task_id} · " + {"awaiting_confirmation": "待确认"}.get(status, status))
        st.text(card)
        st.write(outcome)
        show_files(task_id)
        if status == "awaiting_confirmation" and st.button("接受", key=f"accept_{task_id}"):
            room.accept_delivery(selected, task_id)
            st.rerun()
    with st.expander("派任务"):
        with st.form(f"dispatch_{selected}"):
            description = st.text_area("任务说明")
            reviewer = st.selectbox("审阅者", [i for i in names if i != selected], format_func=names.get)
            if st.form_submit_button("确认提交"):
                try:
                    room.start(description, executor_id=selected, reviewer_id=reviewer, from_private=True)
                except ValueError as error:
                    st.error(str(error))
                else:
                    st.rerun()
    for task_id, status, description in room.dispatched_tasks(selected):
        with st.expander(f"查看任务 #{task_id} · {status}"):
            st.write(description)
            for name, phase, output in room.task_outputs(task_id):
                st.caption(f"{name} · {phase}")
                st.markdown(output)
    message = st.chat_input("Send a private message", key=f"private_{selected}")
    if message:
        try:
            room.private_chat(selected, message)
        except (ValueError, RuntimeError, OSError) as error:
            st.error(str(error))
        else:
            st.rerun()
    st.stop()

st.title("Group chat")


with st.expander("分工任务"):
    with st.form("plan_team"):
        goal = st.text_area("总目标")
        planner = st.selectbox("规划者", list(names), format_func=names.get)
        candidates = st.multiselect("可用成员", list(names), format_func=names.get)
        dependent = st.checkbox("第二步需要第一步的交付物")
        revise = st.checkbox("自动审阅并修订一次（含交付物交接）")
        final = st.checkbox("四成员最终验收（含测试和修订）")
        upload = st.file_uploader("参考文件（可选）", type=["txt", "md"])
        if st.form_submit_button("生成分工计划"):
            try:
                room.plan_team(goal, planner, candidates, (upload.name, upload.getvalue()) if upload else None, dependent=dependent, revise=revise, final=final)
            except (ValueError, RuntimeError, OSError) as error:
                st.error(str(error))
    for parent, goal, planner, raw in room.team_plans():
        st.subheader(f"分工任务 #{parent} · {goal}")
        st.caption(f"规划者：{names[planner]}")
        children = []
        blocked = False
        cards = json.loads(raw)
        for position, card in enumerate(cards[:2], 1):
            child = room.team_child(parent, position)
            children.append(child)
            owner = names[card["owner_id"]]
            st.write(f"{position}. {owner} · {card['description']}")
            st.text(f"交付物：{card['deliverable']}\n完成条件：{card['criteria']}")
            if card.get("inputs"):
                st.write("依赖第一步交付物：" + "、".join(card["inputs"]))
                if not child and children[0]:
                    _, reason = room.team_inputs(parent, card["inputs"])
                    if reason:
                        st.error("第二步未启动：" + reason)
                        blocked = True
            st.caption(child[1] if child else ("已阻止" if blocked else "待派单"))
            if child:
                st.write(child[2])
                show_files(child[0])
        if card.get("revise"):
            st.caption("审阅报告交付后自动交回原执行者修订一次")
            revision = room.team_child(parent, 3)
            if revision:
                st.write(f"修订版 · {revision[1]}")
                show_files(revision[0])
                st.write("执行者逐条回复")
                st.text(revision[2])
            elif all(children) and children[1][1] != "failed":
                children.append(None)
        if len(cards) == 3:
            acceptance = cards[2]
            st.write(f"最终验收 · {names[acceptance['owner_id']]} · {acceptance['description']}")
            st.text(f"交付物：{acceptance['deliverable']}\n完成条件：{acceptance['criteria']}")
            status, detail = room.team_verdict(parent)
            st.write(f"父任务：{status} {detail}")
            child = room.team_child(parent, 4)
            if child:
                show_files(child[0])
            if status == "待验收":
                children.append(None)
        if not blocked and not all(children) and st.button("开始", key=f"team_{parent}"):
            submit(room.start_team, parent)


@st.fragment(run_every=1)
def chat():
    _, messages, active = room.snapshot()
    for name, kind, body in messages:
        if kind == "Tool":
            title, _, detail = body.partition("\n")
            with st.expander(f"{name} · {title}"):
                st.code(detail or title, language=None)
        else:
            with st.chat_message("user" if kind == "Task" else name):
                st.caption(f"{name} · {kind}")
                if kind == "Task acceptance":
                    st.code(body, language=None)
                elif kind == "Stopped":
                    st.error(body)
                else:
                    st.markdown(body)
    progress = room.ralph_progress()
    if progress:
        number, limit, status, tested, passed, output = progress
        st.caption(f"Ralph · Round {number} of {limit or 'unlimited'} · {status}")
        if tested is not None:
            st.write(f"Latest acceptance · Round {tested} · {'Passed' if passed else 'Failed'}")
            st.code(output, language=None)
    if active:
        st.status(active, state="running")
    pending = room.pending()
    if active and progress and pending and st.button("Stop", key=f"stop_{pending[0]}"):
        submit(room.stop, pending[0])
    if pending and not active:
        task_id, status, step = pending
        retry = False
        if status == "running":
            st.warning(f"Step {step + 1} may have changed the work folder. Check it before retrying.")
            retry = st.checkbox(f"I checked the work folder and want to rerun step {step + 1}.",
                                key=f"retry_{task_id}_{step}")
        if st.button("Resume", disabled=status == "running" and not retry):
            submit(lambda value: room.resume(value, retry), task_id)
    command = None
    total_seconds = 3600
    limit = 3
    if not active and not pending and st.checkbox("Ralph mode"):
        command = st.text_input("Acceptance command (runs in the work folder)")
        if st.checkbox("No total time limit"):
            total_seconds = None
        else:
            total_seconds = st.number_input("Total time limit (seconds)", min_value=1, value=3600)
        if st.checkbox("No round limit"):
            limit = None
        else:
            limit = st.number_input("Maximum rounds", min_value=1, value=3)
    final_command = None
    executor = reviewer = planner = None
    if not active and not pending and command is None:
        names = {i: name for i, name, _ in room.members()}
        final_command = st.text_input("Acceptance command after final review (optional)")
        planner = st.selectbox("Planner (optional)", [None, *names],
                               format_func=lambda i: "Use executor" if i is None else names[i])
        executor = st.selectbox("Executor", list(names), format_func=names.get)
        reviewer = st.selectbox("Reviewer", [i for i in names if i != executor], format_func=names.get)
    task = st.chat_input("Give the group a task", disabled=bool(active or pending))
    if task:
        submit(lambda value: room.start(value, command, limit, total_seconds, executor_id=executor, reviewer_id=reviewer, planner_id=planner, final_command=final_command), task)


chat()
