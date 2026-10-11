#!/usr/bin/python3 -I
"""A stand-in for `delegate`, driven by FAKE_MODE, for test_runner.

It only does what the runner observes: parse the flags the runner passes,
optionally write a file inside --dir, and print a Claude Code `--json` object on
stdout. It never touches a key or the network.

Modes (FAKE_MODE):
  ok_noop            print a valid JSON result, change nothing (default)
  ok_edit            write edited.txt in --dir, then the valid JSON result
  outside_scope      write outside.txt in --dir, then the valid JSON result
  fail               exit 1
  reject             exit 2
  badjson            print something that is not JSON, exit 0
  sleep_ignore_term  record its pid in --dir/child.pid, ignore SIGTERM, hang
  sleep              record its pid, hang, die on SIGTERM like a normal process
  leak_ok            start a grandchild in its own session that ignores SIGTERM
                     (pid in --dir/grandchild.pid), then exit 0 with valid JSON
  leak_hang          same grandchild, but the fake itself ignores TERM and hangs
  timeout_exit       exit 124, as delegate does when its own --timeout fires
  is_error           valid JSON with is_error=true, exit 0
  rename             rename README.md to src/README.md, valid JSON
  self_commit        write edited.txt and `git commit` it in --dir, valid JSON
  odd_names          create a file whose name has a newline, outside src/
  plant_bin          write an executable `mycheck` in --dir that exits 0

If FAKE_ARGV_FILE is set, the full argv is also dumped there as JSON so a test
can assert the exact command line the runner built.
"""
import json
import os
import signal
import sys
import time

RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "fake delegate finished",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    },
    "total_cost_usd": 0.0123,
    "modelUsage": {
        "small-model": {"inputTokens": 2, "outputTokens": 1,
                        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
        "deepseek-chat": {"inputTokens": 11, "outputTokens": 7,
                          "cacheReadInputTokens": 3, "cacheCreationInputTokens": 2},
    },
}


def parse(argv):
    """Return the --dir value and the task text from the child argv."""
    workdir = None
    task = ""
    args = argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dir":
            workdir = args[i + 1]
            i += 2
        elif arg in ("--account", "--timeout", "--mcp"):
            i += 2
        elif arg == "--":
            task = " ".join(args[i + 1:])
            break
        else:
            i += 1
    return workdir, task


def _grandchild(workdir):
    """Fork a process that escapes the group and ignores TERM: what a leak is."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(os.path.join(workdir, "grandchild.pid"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        # Do not hold the parent's stdout open: the runner must not depend on
        # EOF to notice the leader exited.
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        while True:
            time.sleep(1)
    for _ in range(200):
        if os.path.exists(os.path.join(workdir, "grandchild.pid")):
            break
        time.sleep(0.01)


def _git(workdir, *args):
    import subprocess
    subprocess.run(["git", "-C", workdir, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@x",
                            GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@x"))


def main(argv):
    mode = os.environ.get("FAKE_MODE", "ok_noop")
    workdir, _task = parse(argv)

    dump = os.environ.get("FAKE_ARGV_FILE")
    if dump:
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump(argv, fh)

    if mode in ("sleep_ignore_term", "sleep", "leak_hang"):
        if mode != "sleep":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "leak_hang":
            _grandchild(workdir)
        if workdir:
            with open(os.path.join(workdir, "child.pid"), "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        while True:
            time.sleep(1)

    if mode == "leak_ok":
        _grandchild(workdir)
    if mode == "timeout_exit":
        return 124
    if mode == "rename":
        os.makedirs(os.path.join(workdir, "src"), exist_ok=True)
        os.rename(os.path.join(workdir, "README.md"),
                  os.path.join(workdir, "src", "README.md"))
    if mode == "self_commit":
        with open(os.path.join(workdir, "edited.txt"), "w", encoding="utf-8") as fh:
            fh.write("committed by the delegate itself\n")
        _git(workdir, "add", "edited.txt")
        _git(workdir, "commit", "-q", "-m", "delegate commit")
    if mode == "odd_names":
        # Split on lines, this would read as "evil" and "src/ok.txt", the
        # second one in scope.
        os.makedirs(os.path.join(workdir, "evil\nsrc"), exist_ok=True)
        with open(os.path.join(workdir, "evil\nsrc/ok.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
    if mode == "plant_bin":
        path = os.path.join(workdir, "mycheck")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    if mode == "is_error":
        json.dump(dict(RESULT, is_error=True, subtype="error_during_execution"),
                  sys.stdout)
        sys.stdout.write("\n")
        return 0

    if mode == "fail":
        return 1
    if mode == "reject":
        return 2
    if mode == "badjson":
        sys.stdout.write("this is not json\n")
        return 0

    if mode in ("ok_edit", "outside_scope") and workdir:
        name = "edited.txt" if mode == "ok_edit" else "outside.txt"
        with open(os.path.join(workdir, name), "w", encoding="utf-8") as fh:
            fh.write("written by the fake delegate\n")

    json.dump(RESULT, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
