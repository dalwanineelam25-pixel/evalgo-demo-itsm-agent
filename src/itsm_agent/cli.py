"""Command line entry point: seed data, run the diagnostic mock, chat, inspect incidents, serve the API."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import ConfigError, Settings
from .db import Repository
from .logging_setup import configure_logging


def _cmd_seed(settings: Settings, args: argparse.Namespace) -> int:
    from .seed import seed_database

    counts = seed_database(Repository(settings.db_path), settings.seed_path, reset=True)
    print(f"Seeded {settings.db_path}: {counts}")
    return 0


def _cmd_diag_service(settings: Settings, args: argparse.Namespace) -> int:
    from .diagnostics.service import DEFAULT_PROFILES, DiagnosticBackend, DiagnosticServer

    backend = DiagnosticBackend.from_file(args.profiles or DEFAULT_PROFILES, timeout_delay_s=args.timeout_delay)
    server = DiagnosticServer(backend, host=args.host, port=args.port)
    print(f"VPN diagnostic mock listening on {server.url} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    repo = Repository(settings.db_path)
    try:
        snap = repo.incident_snapshot(args.incident)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(snap, indent=2, default=str))
    return 0


def _print_turn(result, verbose: bool) -> None:
    if verbose:
        for call in result.tool_calls:
            print(f"  [tool] {call['name']}({json.dumps(call['args'])})")
            if call["result"]:
                print(f"         -> {call['result'][:300]}")
    pending = result.pending_confirmation
    if result.reply and not (pending and result.reply == pending["question"]):
        print(f"\nAgent: {result.reply}\n")


def _ask_confirmation(service, thread_id: str, user_id: str, pending: dict, verbose: bool):
    print(f"--- Confirmation requested for {pending['incident_id']} ---")
    print(pending["question"])
    while True:
        answer = input("Confirm the issue is fixed? [y/n]: ").strip().lower()
        if answer in ("y", "yes", "n", "no"):
            break
    comment = input("Optional comment: ").strip()
    result = service.respond_to_confirmation(thread_id, user_id, answer.startswith("y"), comment)
    _print_turn(result, verbose)
    return result


def _cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    from .service import ConversationError, ITSMAgentService

    service = ITSMAgentService(settings)
    try:
        thread_id = args.thread or service.start_conversation(args.user, args.incident)
    except ConversationError as exc:
        print(f"Cannot start conversation: {exc.message}", file=sys.stderr)
        return 2
    print(f"Conversation {thread_id} as {args.user} (mode: {settings.model_mode}). Type /quit to exit.")
    try:
        pending = service.pending_confirmation(thread_id)
        while True:
            if pending:
                pending = _ask_confirmation(service, thread_id, args.user, pending, args.verbose).pending_confirmation
                continue
            try:
                text = input("You: ").strip()
            except EOFError:
                break
            if text in ("/quit", "/exit"):
                break
            if not text:
                continue
            try:
                result = service.send_message(thread_id, args.user, text)
            except ConversationError as exc:
                print(f"Error: {exc.message}")
                continue
            _print_turn(result, args.verbose)
            pending = result.pending_confirmation
    finally:
        service.close()
    print(f"Conversation saved as {thread_id}. Resume with --thread {thread_id}.")
    return 0


def _cmd_api(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="itsm-agent", description="IT incident resolution agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="(Re)create the incident database with fictional seed data")

    p = sub.add_parser("diag-service", help="Run the local VPN diagnostic mock service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--profiles", default=None, help="Path to diagnostic_profiles.json")
    p.add_argument("--timeout-delay", type=float, default=float(os.environ.get("DIAG_MOCK_TIMEOUT_DELAY_S", "30")),
                   help="Seconds the mock sleeps for 'timeout' behaviours")

    p = sub.add_parser("chat", help="Interactive chat with the agent")
    p.add_argument("--user", required=True, help="Authenticated user id, e.g. u-1001")
    p.add_argument("--incident", default=None, help="Incident number this conversation is about")
    p.add_argument("--thread", default=None, help="Resume an existing conversation")
    p.add_argument("-v", "--verbose", action="store_true", help="Show tool calls and results")

    p = sub.add_parser("show", help="Print an incident with notes, diagnostic runs, confirmations and audit")
    p.add_argument("incident")

    p = sub.add_parser("api", help="Serve the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_path, settings.log_level, console=args.command in ("diag-service", "api"))

    handlers = {
        "seed": _cmd_seed,
        "diag-service": _cmd_diag_service,
        "chat": _cmd_chat,
        "show": _cmd_show,
        "api": _cmd_api,
    }
    try:
        return handlers[args.command](settings, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
