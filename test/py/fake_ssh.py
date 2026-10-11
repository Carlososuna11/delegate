#!/usr/bin/python3 -I
"""A stand-in for `ssh`, for test_remote: runs the command on this machine.

It parses its argv the way OpenSSH does (options, `--`, destination, then the
command words joined with spaces) and runs the command with `/bin/bash -c`,
like sshd does with the login shell, but against a fake "remote": the
environment is FAKE_REMOTE_ENV (a JSON object) and nothing else, and the cwd is
its HOME. So the laptop's environment never reaches the "remote", as with the
real ssh.

Knobs, all optional:
  FAKE_SSH_LOG    append one JSON line per call: {"argv", "host", "cmd", "t"}
  FAKE_SSH_FAIL   JSON {"match": regex, "times": N}: the first N calls whose
                  command matches exit 255 without running anything
  FAKE_SSH_DROP   JSON {"match": regex, "times": N, "after": seconds}: the
                  first N matching calls run the command, but after `after`
                  seconds (or when it ends, if sooner) the "connection" drops:
                  the output is discarded, ssh exits 255 and the remote
                  command, if still running, is left running with a closed
                  stdout
  FAKE_SSH_STATE  directory for the per-rule counters
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

_WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")


def parse(argv):
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        letter = arg[1:2]
        i += 2 if letter in _WITH_VALUE and len(arg) == 2 else 1
    if i >= len(argv):
        sys.stderr.write("fake_ssh: falta el destino\n")
        sys.exit(255)
    return argv[i], " ".join(argv[i + 1:]), argv[:i]


def _take(rule_env, cmd):
    """True if this call is one of the first N that match the rule."""
    raw = os.environ.get(rule_env)
    if not raw:
        return None
    rule = json.loads(raw)
    if not re.search(rule["match"], cmd):
        return None
    counter = os.path.join(os.environ["FAKE_SSH_STATE"], rule_env)
    fd = os.open(counter, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seen = int(os.read(fd, 32) or b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(seen + 1).encode())
    finally:
        os.close(fd)
    return rule if seen < rule.get("times", 1) else None


def main(argv):
    host, cmd, opts = parse(argv)
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "host": host, "cmd": cmd, "opts": opts,
                                "t": time.time()}) + "\n")
    if not cmd:
        sys.stderr.write("fake_ssh: sin comando (no hay shell interactivo)\n")
        return 255
    if _take("FAKE_SSH_FAIL", cmd):
        sys.stderr.write(f"ssh: connect to host {host}: Connection refused\n")
        return 255
    env = json.loads(os.environ["FAKE_REMOTE_ENV"])
    drop = _take("FAKE_SSH_DROP", cmd)
    if not drop:
        return subprocess.run(["/bin/bash", "-c", cmd], cwd=env["HOME"], env=env).returncode
    # stdout and stderr are both pipes of this process, never the caller's:
    # a remote command that outlives the "connection" must not keep the
    # caller's pipes open, or the caller would wait for it like it never
    # would for a real dropped ssh.
    proc = subprocess.Popen(["/bin/bash", "-c", cmd], cwd=env["HOME"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    deadline = time.monotonic() + float(drop.get("after", 0))
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.02)
    proc.stdout.close()  # the remote side now writes into a closed pipe
    proc.stderr.close()
    sys.stderr.write(f"Connection to {host} closed by remote host.\n")
    return 255


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
