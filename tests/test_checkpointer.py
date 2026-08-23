"""
Tests for durable chat threads (ADR-022).

No model calls here. What ADR-022 actually claims is about storage, and
all three claims are checkable without spending a token:

  1. a checkpoint survives the object that wrote it (the "restart" test)
  2. the checkpointer's tables and the ADR-019 store's tables coexist in
     one file, and neither breaks the other
  3. secretary_graph.py really is wired to the durable saver

Every test runs against a tmp_path database. Touching the real
secretary.db from a test would put fake threads in the history you read
with `python store.py`.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import ast
import operator
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

import store

REPO = Path(__file__).resolve().parent.parent


# --- a graph small enough to reason about -----------------------------------
#
# Same STATE shape as secretary_graph.AgentState (a list with an
# operator.add reducer), because that reducer is what has to survive the
# round trip through storage. Deliberately not importing the real graph:
# that would construct an Anthropic client and a Gmail connector, and
# point the checkpointer at the real database.

class _State(TypedDict):
    messages: Annotated[list, operator.add]


def _tiny_graph(saver: SqliteSaver):
    builder = StateGraph(_State)
    builder.add_node("echo", lambda s: {"messages": ["reply"]})
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=saver)


def _saver(db: Path) -> SqliteSaver:
    """A checkpointer wired exactly the way secretary_graph.py wires one."""
    conn = sqlite3.connect(db, check_same_thread=False, timeout=10.0)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


# --- 1. the point of the whole change --------------------------------------

def test_thread_survives_a_new_saver(tmp_path):
    """The InMemorySaver bug, as a test. A second SqliteSaver over the same
    file is what a server restart looks like from the database's side."""
    db = tmp_path / "secretary.db"
    config = {"configurable": {"thread_id": "monday"}}

    first = _saver(db)
    _tiny_graph(first).invoke({"messages": ["hello"]}, config)
    first.conn.close()                      # the process goes away

    second = _saver(db)                     # a new process starts
    state = _tiny_graph(second).get_state(config)

    assert state.values["messages"] == ["hello", "reply"], (
        "history did not survive a new checkpointer over the same file — "
        "durability is what ADR-022 is for")
    second.conn.close()


def test_threads_stay_separate(tmp_path):
    """A different thread_id must be a different conversation, or
    `thread_id` in ChatRequest means nothing."""
    db = tmp_path / "secretary.db"
    saver = _saver(db)
    graph = _tiny_graph(saver)

    graph.invoke({"messages": ["from monday"]},
                 {"configurable": {"thread_id": "monday"}})
    graph.invoke({"messages": ["from tuesday"]},
                 {"configurable": {"thread_id": "tuesday"}})

    monday = graph.get_state({"configurable": {"thread_id": "monday"}})
    assert monday.values["messages"] == ["from monday", "reply"]
    saver.conn.close()


def test_unknown_thread_is_empty_not_an_error(tmp_path):
    """A fresh thread_id from a client must start a conversation, not 500."""
    db = tmp_path / "secretary.db"
    saver = _saver(db)

    state = _tiny_graph(saver).get_state(
        {"configurable": {"thread_id": "never-used"}})

    assert not state.values
    saver.conn.close()


# --- 2. one file, two schemas ----------------------------------------------

def test_checkpoint_tables_coexist_with_the_store(tmp_path, monkeypatch):
    """ADR-022 rejected a separate database on the grounds that the table
    names do not collide. That is a claim about a real file, so check it on
    one: set up the store schema and the checkpointer schema together, then
    use both."""
    db = tmp_path / "secretary.db"
    monkeypatch.setattr(store, "DB_PATH", db)

    store_conn = store.connect()            # emails/extractions/runs/fts
    saver = _saver(db)                      # checkpoints/writes

    names = {r["name"] for r in store_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"emails", "extractions", "runs"} <= names
    assert {"checkpoints", "writes"} <= names

    # And both still work afterwards. A schema that merely EXISTS proves
    # less than one that is still writable next to the other.
    _tiny_graph(saver).invoke({"messages": ["x"]},
                              {"configurable": {"thread_id": "t"}})
    # Subset, like the two table assertions above: this test is about the
    # two schemas coexisting, not about how many things counts() reports.
    # Exact equality here made adding the proposals table (ADR-023) look
    # like a checkpointer regression.
    assert store.counts(store_conn).items() >= {
        "emails": 0, "extractions": 0, "failures": 0, "runs": 0}.items()

    saver.conn.close()
    store_conn.close()


def test_store_connect_leaves_wal_on(tmp_path, monkeypatch):
    """Both writers assume WAL (ADR-019 calls it a correctness
    requirement). SqliteSaver.setup() also sets it, so this pins the
    mode they agree on rather than trusting two libraries to match."""
    db = tmp_path / "secretary.db"
    monkeypatch.setattr(store, "DB_PATH", db)

    conn = store.connect()
    saver = _saver(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"
    saver.conn.close()
    conn.close()


# --- 3. the wiring ---------------------------------------------------------

def test_secretary_graph_uses_the_durable_saver():
    """Read the source rather than import it: importing builds an Anthropic
    client and a Gmail connector, and would create checkpoint tables in the
    real secretary.db. The names used at compile time are all this needs."""
    source = (REPO / "secretary_graph.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(tree) if isinstance(n, ast.Call)}

    assert "SqliteSaver" in called
    assert "InMemorySaver" not in called, (
        "back on the in-memory saver — chat threads would silently stop "
        "surviving restarts (ADR-022)")


def test_checkpointer_shares_the_store_database():
    """ADR-022's one-file decision, as a check on the source: the path
    handed to sqlite3.connect must be store.DB_PATH, not a literal."""
    source = (REPO / "secretary_graph.py").read_text(encoding="utf-8")

    assert "sqlite3.connect(store.DB_PATH" in source
    assert "check_same_thread=False" in source
