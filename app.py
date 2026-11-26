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
    st.header("Group members")
    for (name, role), job in zip(people, ("Task owner", "Reviewer")):
        st.subheader(name)
        st.caption(job)
        st.text(role)
    if st.button("Open work folder"):
        os.startfile(room.work)

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
    task = st.chat_input("Give the group a task", disabled=bool(active or pending))
    if task:
        submit(lambda value: room.start(value, command, limit, total_seconds), task)


chat()
