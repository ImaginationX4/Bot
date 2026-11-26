import os
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

if selected is not None:
    st.title(f"Private chat · {names[selected]}")
    for sender, body in room.private_history(selected):
        with st.chat_message(sender):
            st.markdown(body)
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
                if kind == "Stopped":
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
    executor = reviewer = None
    if not active and not pending and command is None:
        names = {i: name for i, name, _ in room.members()}
        executor = st.selectbox("Executor", list(names), format_func=names.get)
        reviewer = st.selectbox("Reviewer", [i for i in names if i != executor], format_func=names.get)
    task = st.chat_input("Give the group a task", disabled=bool(active or pending))
    if task:
        submit(lambda value: room.start(value, command, limit, total_seconds, executor_id=executor, reviewer_id=reviewer), task)


chat()
