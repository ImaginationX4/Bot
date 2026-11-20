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
    if active:
        st.status(active, state="running")
    task = st.chat_input("Give the group a task", disabled=bool(active))
    if task:
        submit(room.start, task)


chat()
