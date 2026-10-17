"""Standalone cross-platform helper process used by runner unit tests.

Uses only the Python standard library and imports nothing from gradle_healer or
tests, so it can be launched as `[sys.executable, str(FAKE_PROCESS), ...]` in
an arbitrary working directory. Writes raw UTF-8 bytes to stdout/stderr buffers
so Windows text-mode pipes do not translate `\\n` into `\\r\\n`.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

FAKE_PROCESS: Path = Path(__file__).with_name("fake_process.py")


def fake_cmd(*args: str) -> list[str]:
    """Build the command list to invoke fake_process.py with the current interpreter."""
    return [sys.executable, str(FAKE_PROCESS), *args]


def _write_stdout(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def _write_stderr(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()


def _report_pid(port: int, pid: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(f"{pid}\n".encode("ascii"))


def _cmd_emit(ns: argparse.Namespace) -> int:
    if ns.stdout:
        _write_stdout(str(ns.stdout))
    if ns.stderr:
        _write_stderr(str(ns.stderr))
    if ns.sleep > 0:
        time.sleep(float(ns.sleep))
    return int(ns.exit_code)


def _cmd_raw_sanitization_sample(_ns: argparse.Namespace) -> int:
    payload = b"\033[1;31mred\033[0m\r\nnext\rbar\033]0;title\007\n\xff\n"
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


def _cmd_lines(ns: argparse.Namespace) -> int:
    _write_stdout("".join(f"{i}\n" for i in range(1, int(ns.count) + 1)))
    return 0


def _cmd_sleep(ns: argparse.Namespace) -> int:
    if ns.with_zombie:
        # The sleeping grandchild itself spawns a short-lived child and never
        # waits on it, so the child stays an unreaped zombie in the process
        # group for the entire duration of the sleep (never reparented to
        # init/launchd while this sleep process is alive).
        subprocess.Popen(
            [sys.executable, str(FAKE_PROCESS), "emit"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(float(ns.seconds))
    return 0


def _cmd_cwd(ns: argparse.Namespace) -> int:
    _write_stdout(f"{os.getcwd()}\n")
    rest: list[str] = list(ns.rest)
    if rest:
        _write_stdout(f"{' '.join(rest)}\n")
    return 0


def _cmd_spawn_grandchild(ns: argparse.Namespace) -> int:
    if ns.ignore_term and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if ns.ignore_term:
        child_cmd = [sys.executable, str(FAKE_PROCESS), "ignore-term", "--seconds", str(ns.seconds)]
    else:
        child_cmd = [sys.executable, str(FAKE_PROCESS), "sleep", str(ns.seconds)]
        if ns.with_zombie:
            child_cmd.append("--with-zombie")
    grandchild = subprocess.Popen(child_cmd)
    if ns.report_port is not None:
        _report_pid(int(ns.report_port), grandchild.pid)
    else:
        _write_stdout(f"{grandchild.pid}\n")
    if ns.wait:
        grandchild.wait()
    return 0


def _cmd_ignore_term(ns: argparse.Namespace) -> int:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if ns.report_port is not None:
        _report_pid(int(ns.report_port), os.getpid())
    time.sleep(float(ns.seconds))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake_process.py")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    emit = subparsers.add_parser("emit")
    emit.add_argument("--stdout", default="")
    emit.add_argument("--stderr", default="")
    emit.add_argument("--exit-code", type=int, default=0)
    emit.add_argument("--sleep", type=float, default=0.0)

    subparsers.add_parser("raw-sanitization-sample")

    lines = subparsers.add_parser("lines")
    lines.add_argument("count", type=int)

    sleep = subparsers.add_parser("sleep")
    sleep.add_argument("seconds", type=float)
    sleep.add_argument("--with-zombie", action="store_true")

    cwd = subparsers.add_parser("cwd")
    cwd.add_argument("rest", nargs=argparse.REMAINDER)

    spawn = subparsers.add_parser("spawn-grandchild")
    spawn.add_argument("--seconds", type=float, default=30.0)
    spawn.add_argument("--report-port", type=int, default=None)
    spawn.add_argument("--wait", action="store_true")
    spawn.add_argument("--with-zombie", action="store_true")
    spawn.add_argument("--ignore-term", action="store_true")

    ignore_term = subparsers.add_parser("ignore-term")
    ignore_term.add_argument("--seconds", type=float, default=30.0)
    ignore_term.add_argument("--report-port", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    match ns.subcommand:
        case "emit":
            return _cmd_emit(ns)
        case "raw-sanitization-sample":
            return _cmd_raw_sanitization_sample(ns)
        case "lines":
            return _cmd_lines(ns)
        case "sleep":
            return _cmd_sleep(ns)
        case "cwd":
            return _cmd_cwd(ns)
        case "spawn-grandchild":
            return _cmd_spawn_grandchild(ns)
        case "ignore-term":
            return _cmd_ignore_term(ns)
        case _:
            raise ValueError(f"unknown subcommand: {ns.subcommand!r}")


if __name__ == "__main__":
    raise SystemExit(main())
