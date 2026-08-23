"""
The interactive client ADR-020 predicted: "every future front end is a
client of these endpoints."

So this file speaks HTTP and imports nothing from the system it drives —
no graph, no connector, no Anthropic client. That restraint is the whole
experiment. If a REPL cannot be built against the API alone, then the
service is not really the interaction surface and ADR-020 is wishful
thinking. Anything awkward here is an API gap, and worth fixing in
server.py rather than papering over locally.

It also means the checkpointer (ADR-022) stays in ONE process. A client
that imported secretary_graph would open a second checkpointer over the
same database and keep its own half of your history.

    python server.py     # in one window
    python chat.py       # in another

Bare text is a message to the agent. Anything starting with / is a
command; /help lists them.

Where the tool trace went: the graph prints `-> tool(args)` in the
SERVER's window, not here. Getting it into this one means adding the tool
calls to ChatResponse — an API change, and its own decision.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime

import httpx
from rich.console import Console
from rich.markdown import Markdown

from console import use_utf8

# Not imported from server.py, deliberately. `from server import HOST, PORT`
# would drag FastAPI, run_digest, the Gmail connector and the Anthropic
# client into this process — exactly the coupling a client should not have.
# The env var is the seam for pointing at a different port.
BASE_URL = os.environ.get("SECRETARY_URL", "http://127.0.0.1:8765")

# Two timeouts, because the endpoints are not alike. The reads are local
# SQLite queries and answer instantly; /chat runs a whole agent loop — a
# thinking pass plus tool round trips that each fetch live email.
#
# httpx defaults to 5s, which /chat exceeds every single time. Getting
# this wrong fails asymmetrically: the client gives up, the server keeps
# going and finishes the run, and you are billed for an answer nobody
# receives. Better to wait too long than to abandon paid work.
READ_TIMEOUT = httpx.Timeout(10.0)
CHAT_TIMEOUT = httpx.Timeout(10.0, read=300.0)

# A stable default, so restarting this REPL RESUMES yesterday's
# conversation. That is ADR-022 being useful rather than merely true.
# The cost is real though: every turn resends the whole thread, so a long
# one gets steadily more expensive. /new is how you stop paying for
# context you no longer need.
DEFAULT_THREAD = "cli"

CONSOLE = Console()


@dataclass
class Session:
    client: httpx.Client
    thread: str = DEFAULT_THREAD


class Quit(Exception):
    """Raised by /quit and by Ctrl-D to leave the loop."""


# --- talking to the service ------------------------------------------------

def _fail(message: str) -> None:
    print(f"! {message}", file=sys.stderr)


def _detail(response: httpx.Response) -> str:
    """FastAPI puts the human-readable reason in `detail`."""
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:                      # not JSON (e.g. a plain 500)
        return response.text[:200] or response.reason_phrase


def _explain(response: httpx.Response) -> str:
    """Turn the statuses server.py returns on purpose back into sentences.

    Each of these is a real state of the system rather than a bug, so the
    REPL should say what it means and what to do about it.
    """
    if response.status_code == 409:
        return "an ingest is already running — try again in a moment"
    if response.status_code == 503:
        # The server names which grant died — Gmail for /ingest, the
        # calendar write token for /confirm — and each needs a different
        # script run. A fixed sentence here would send you to the wrong one.
        return _detail(response)
    if response.status_code == 404:
        return _detail(response)
    return f"{response.status_code}: {_detail(response)}"


def _call(method, *args, **kwargs) -> httpx.Response | None:
    """One HTTP call, with every failure turned into a printed sentence.

    Returns None on failure so callers can simply `if resp is None: return`.
    A REPL that answers a typo with a traceback is a REPL you stop using.
    """
    try:
        response = method(*args, **kwargs)
        response.raise_for_status()
        return response
    except httpx.ConnectError:
        _fail(f"nothing answering on {BASE_URL} — start it with "
              f"`python server.py`")
    except httpx.ReadTimeout:
        _fail("no reply in time. The server is probably still working: the "
              "run continues there, and its answer is lost to this client. "
              "Watch the server window.")
    except httpx.HTTPStatusError as e:
        _fail(_explain(e.response))
    return None


def say(session: Session, message: str) -> None:
    """One chat turn. The blocking call this whole file is arranged around."""
    # A spinner because there is nothing to stream yet: /chat returns when
    # the graph is finished, so a minute of silence is normal and needs to
    # look intentional. Streaming is the next real decision.
    with CONSOLE.status("[dim]thinking…[/dim]", spinner="dots"):
        response = _call(session.client.post, "/chat",
                         json={"message": message, "thread_id": session.thread},
                         timeout=CHAT_TIMEOUT)
    if response is None:
        return

    # Printed as plain text, not rendered as markdown: the reply cites email
    # ids and quotes subjects, and a markdown renderer would eat the
    # underscores and asterisks inside them. /digest renders, because that
    # endpoint really does return markdown.
    print(response.json()["reply"])
    _nudge_pending(session)


def _nudge_pending(session: Session) -> None:
    """One line after a turn if the agent queued something.

    Worth the extra round trip (localhost, SQLite) because the failure it
    prevents is the quiet one: the agent says "I have proposed 3pm", you
    read that as done, and nothing is ever on your calendar. A proposal
    that nobody confirms expires in 48 hours (ADR-024).
    """
    response = _call(session.client.get, "/proposals",
                     params={"status": "pending"})
    if response is None:
        return
    pending = response.json()
    if pending:
        print(f"\n[{len(pending)} proposal(s) awaiting you — /proposals]")


# --- commands --------------------------------------------------------------

def cmd_thread(session: Session, args: str) -> None:
    """/thread [id] — show the current thread, or switch to another."""
    if not args:
        print(f"thread: {session.thread}")
        return
    session.thread = args.strip()
    print(f"thread: {session.thread} (history, if any, continues)")


def cmd_new(session: Session, args: str) -> None:
    """/new [id] — start a fresh conversation. Timestamped if unnamed."""
    session.thread = args.strip() or f"cli-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"thread: {session.thread} (new)")


def cmd_digest(session: Session, args: str) -> None:
    """/digest — the newest digest, rendered."""
    response = _call(session.client.get, "/digest/latest")
    if response is None:
        return
    CONSOLE.print(Markdown(response.text))


def cmd_runs(session: Session, args: str) -> None:
    """/runs [n] — recent ingest runs and their coverage."""
    limit = args.strip() or "10"
    response = _call(session.client.get, "/runs", params={"limit": limit})
    if response is None:
        return
    for run in response.json():
        missed = f"  MISSED {run['missed']}" if run["missed"] else ""
        print(f"  {run['id']}  {run['ok_count']:>3} ok "
              f"{run['failed_count']:>3} failed{missed}")


def cmd_search(session: Session, args: str) -> None:
    """/search <words> — keyword search over stored mail."""
    if not args.strip():
        _fail("usage: /search <words>")
        return
    response = _call(session.client.get, "/search", params={"q": args.strip()})
    if response is None:
        return
    for email in response.json():
        print(f"  {email['id']}  {email['sender'][:35]:<35}  "
              f"{email['subject'][:50]}")


def cmd_proposals(session: Session, args: str) -> None:
    """/proposals [status|all] — calendar events waiting for your yes."""
    status = args.strip() or "pending"
    response = _call(session.client.get, "/proposals",
                     params={"status": status})
    if response is None:
        return
    rows = response.json()
    if not rows:
        print(f"  no {status} proposals")
        return
    for row in rows:
        # reads_as carries the verbatim phrase from the email next to the
        # resolved time. Reading those two against each other IS the check
        # you are being asked to perform — a wrong timezone looks fine on
        # its own and obviously wrong beside the words it came from.
        print(f"  {row['id']}  {row['reads_as']}")
        verdict = ("eligible" if row["policy_ok"]
                   else "needs review: " + ", ".join(row["policy_failed"]))
        source = row["source_email_id"] or "no source email"
        print(f"{'':>12}{verdict}   [{source}]")


def cmd_confirm(session: Session, args: str) -> None:
    """/confirm <id> — put a proposal on the real calendar (tentative)."""
    proposal_id = args.strip()
    if not proposal_id:
        _fail("usage: /confirm <id> — see /proposals")
        return
    response = _call(session.client.post,
                     f"/proposals/{proposal_id}/confirm")
    if response is None:
        return
    result = response.json()
    print(f"  {result['status']}: {result['message']}")
    if result.get("event_id"):
        print(f"  event: {result['event_id']}")


def cmd_reject(session: Session, args: str) -> None:
    """/reject <id> [reason] — decline a proposal."""
    proposal_id, _, reason = args.strip().partition(" ")
    if not proposal_id:
        _fail("usage: /reject <id> [reason]")
        return
    response = _call(session.client.post, f"/proposals/{proposal_id}/reject",
                     json={"reason": reason.strip()})
    if response is None:
        return
    print(f"  {response.json()['message']}")


def cmd_health(session: Session, args: str) -> None:
    """/health — row counts, latest run, whether an ingest is in flight."""
    response = _call(session.client.get, "/health")
    if response is None:
        return
    info = response.json()
    print(f"  counts:      {info['counts']}")
    print(f"  latest run:  {info['latest_run']}")
    print(f"  ingesting:   {info['ingest_running']}")


def cmd_help(session: Session, args: str) -> None:
    """/help — this list."""
    print("Bare text goes to the agent. Commands:")
    # Deduplicated by handler, not by name: /quit and /exit are one command
    # under two names, and listing its line twice is just noise.
    for handler in dict.fromkeys(COMMANDS.values()):
        print(f"  {handler.__doc__}")
    print("\nTool calls appear in the SERVER window, not here.")


def cmd_quit(session: Session, args: str) -> None:
    """/quit — leave (also /exit, or Ctrl-D)."""
    raise Quit


COMMANDS = {
    "thread": cmd_thread,
    "new": cmd_new,
    "digest": cmd_digest,
    "runs": cmd_runs,
    "search": cmd_search,
    "proposals": cmd_proposals,
    "confirm": cmd_confirm,
    "reject": cmd_reject,
    "health": cmd_health,
    "help": cmd_help,
    "quit": cmd_quit,
    "exit": cmd_quit,
}


# --- the loop --------------------------------------------------------------

def dispatch(session: Session, line: str) -> None:
    """A leading / means command; everything else is for the agent.

    Written as a plain loop rather than with stdlib `cmd`: getting this
    split out of cmd.Cmd needs parseline, emptyline and its EOF sentinel
    all overridden, which costs more than the class saves.
    """
    if not line.startswith("/"):
        say(session, line)
        return

    name, _, args = line[1:].partition(" ")
    handler = COMMANDS.get(name.lower())
    if handler is None:
        _fail(f"no command /{name} — try /help")
        return
    handler(session, args)


def main() -> None:
    use_utf8()          # the model picks emoji for priority markers, and a
                        # cp1252 console cannot encode them (ADR-021)

    print(f"Secretary REPL -> {BASE_URL}   (/help, /quit)")
    session = Session(client=httpx.Client(base_url=BASE_URL,
                                          timeout=READ_TIMEOUT))
    try:
        while True:
            try:
                line = input(f"[{session.thread}] > ").strip()
            except EOFError:                 # Ctrl-D
                print()
                break
            except KeyboardInterrupt:        # Ctrl-C at the prompt
                print("\n(use /quit to leave)")
                continue

            if not line:
                continue
            try:
                dispatch(session, line)
            except Quit:
                break
            except KeyboardInterrupt:
                # Ctrl-C DURING a call. The HTTP request is abandoned here,
                # but the server keeps running the graph to completion —
                # this cancels the waiting, not the work.
                _fail("interrupted. The server may still be working; its "
                      "reply is saved to the thread either way.")
    finally:
        session.client.close()


if __name__ == "__main__":
    main()
