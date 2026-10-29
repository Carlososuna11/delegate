"""Tests for `crew launch` and `crew brief`: the real CLI against a fake ssh.

The "remote" is a HOME of this machine that fake_ssh.py enters with `bash -c`
and an environment of its own. Inside it, a fake `paseo` (written by setUp into
$HOME/bin) records its argv and stdin, keeps a list of agents and answers `ls`
with the shape of real `paseo ls --json`.

Run from the repo root:
    python3 -m unittest test.py.test_crew_launch -v
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "crew"
FAKE_SSH = Path(__file__).resolve().parent / "fake_ssh.py"
SLUG = "innovares.datago"
REMOTE_CWD = "~/innovares/innovares-agent"
UUID4 = r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
AGENT_ID = r"\A[0-9a-f-]{7,36}\Z"

# Written into the fake remote's $HOME/bin. It keeps state under
# $HOME/paseo-state so the `ls` of a later call sees the agents launched by an
# earlier one, and an append-only log under $HOME/paseo.log for the tests.
FAKE_PASEO = r'''#!/usr/bin/python3 -I
"""Stand-in for `paseo`: records argv and stdin, keeps agents, answers ls."""
import fcntl
import json
import os
import sys
import time
import uuid

HOME = os.environ["HOME"]
STATE = os.path.join(HOME, "paseo-state")
LOG = os.path.join(HOME, "paseo.log")
AGENTS = os.path.join(STATE, "agents.json")
LOCK = os.path.join(STATE, "lock")
INSPECT = os.path.join(HOME, "paseo-inspect.json")
LOGS = os.path.join(HOME, "paseo-logs")
PIDS = os.path.join(HOME, "paseo.pids")
KEYS = ("id", "shortId", "name", "provider", "thinking", "status", "cwd", "created")


def locked(fn):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        os.close(fd)


def load():
    try:
        with open(AGENTS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def store(agents):
    tmp = AGENTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(agents, f)
    os.replace(tmp, AGENTS)


def emit(text):
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except OSError:
        pass  # the "connection" may be gone


def notice(text):
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except OSError:
        pass


def main(argv):
    cmd = argv[0] if argv else ""
    with open(PIDS, "a", encoding="utf-8") as f:
        f.write(str(os.getpid()) + "\n")
    stdin = sys.stdin.read() if cmd in ("run", "send") else ""
    labels, options = {}, {}
    i = 1
    while i < len(argv):
        if argv[i] == "--label" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            labels[key] = value
            i += 2
        elif argv[i] in ("--title", "--cwd", "--provider", "--mode"):
            options[argv[i][2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            i += 1
    entry = {"cmd": cmd, "argv": argv, "stdin": stdin,
             "labels": labels, "at": time.time()}

    def act():
        agents = load()
        if cmd == "run":
            agent_id = str(uuid.uuid4())
            agents.append({
                "id": agent_id,
                "shortId": agent_id[:7],
                "name": options.get("title", ""),
                "provider": options.get("provider", "claude"),
                "thinking": options.get("mode", "auto"),
                "status": "running",
                "cwd": options.get("cwd", HOME),
                "created": int(time.time()),
                # Kept on disk only: real `ls --json` filters by label itself.
                "labels": labels,
            })
            store(agents)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return agents

    agents = locked(act)
    # Recorded before the delay so a dropped ssh can still be reconciled.
    delay = float(os.environ.get("FAKE_PASEO_DELAY", "0") or 0)
    if delay and cmd == os.environ.get("FAKE_PASEO_DELAY_CMD", "run"):
        time.sleep(delay)
    if cmd == "run":
        notice("Created workspace " + options.get("cwd", HOME))
        notice("Tip: revisa el avance con `paseo logs`")
        emit(agents[-1]["id"])
    elif cmd == "ls":
        public = [{k: a.get(k) for k in KEYS} for a in agents
                  if all(a.get("labels", {}).get(k) == v for k, v in labels.items())]
        emit(json.dumps(public))
    elif cmd == "inspect":
        target = argv[1] if len(argv) > 1 else ""
        try:
            with open(INSPECT, encoding="utf-8") as f:
                found = json.load(f).get(target)
        except (OSError, ValueError):
            found = None
        if found is None:
            notice("inspect: no such agent")
            return 1
        emit(found if isinstance(found, str) else json.dumps(found, indent=2))
    elif cmd == "logs":
        target = argv[1] if len(argv) > 1 else ""
        try:
            with open(os.path.join(LOGS, target + ".txt"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except OSError:
            pass
    elif cmd == "send":
        # Like the real thing, a send starts a turn: the agent is running now
        # and its UpdatedAt moves.
        target = argv[2] if len(argv) > 2 else ""

        def start_turn():
            current = load()
            for agent in current:
                if agent.get("id") == target:
                    agent["status"] = "running"
            store(current)
            try:
                with open(INSPECT, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            entry = data.get(target) if isinstance(data, dict) else None
            if isinstance(entry, dict):
                entry["Status"] = "running"
                entry["UpdatedAt"] = "sent-{:f}".format(time.time())
                tmp = INSPECT + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, INSPECT)
        locked(start_turn)
        emit("")
    elif cmd == "stop":
        target = argv[1] if len(argv) > 1 else ""

        def mark():
            current = load()
            for agent in current:
                if agent.get("id") == target:
                    agent["status"] = "idle"
            store(current)
        locked(mark)
        emit("")
    else:
        emit("")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


class CrewFixture(unittest.TestCase):
    """The sandbox shared by the crew suites: repo, config, state and remote.

    It has no tests of its own; test_crew_state builds on it so both suites use
    one fake remote and one set of helpers.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crew-launch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.set_origin("git@github.com:Innovares/datago.git")
        self.rhome = self.tmp / "remote-home"
        self.rbin = self.rhome / "bin"
        self.rbin.mkdir(parents=True)
        paseo = self.rbin / "paseo"
        paseo.write_text(FAKE_PASEO, encoding="utf-8")
        paseo.chmod(0o755)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({
            "host": "caracas",
            "orgs": {"Innovares": REMOTE_CWD, "Kontigo-Inc": "~/kontigo/review-config"},
        }), encoding="utf-8")
        self.state = self.tmp / "state"
        self.ssh_state = self.tmp / "ssh-state"
        self.ssh_state.mkdir()
        self.ssh_log = self.tmp / "ssh.log"
        laptop_home = self.tmp / "laptop-home"
        laptop_home.mkdir()
        self.remote_env = {
            "HOME": str(self.rhome),
            "PATH": f"{self.rbin}:/usr/local/bin:/usr/bin:/bin",
        }
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(laptop_home),
            "CREW_CONFIG": str(self.config),
            "CREW_STATE_DIR": str(self.state),
            "CREW_SSH": str(FAKE_SSH),
            "FAKE_SSH_LOG": str(self.ssh_log),
            "FAKE_SSH_STATE": str(self.ssh_state),
        }

    # -- helpers -------------------------------------------------------------

    def git(self, *args, cwd=None, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True, env=env, check=check)

    def set_origin(self, url, repo=None):
        target = repo or self.repo
        if self.git("remote", "get-url", "origin", cwd=target, check=False).returncode == 0:
            self.git("remote", "set-url", "origin", url, cwd=target)
        else:
            self.git("remote", "add", "origin", url, cwd=target)

    def environment(self, remote_env=None, env=None):
        renv = dict(self.remote_env, **(remote_env or {}))
        return dict(self.env, FAKE_REMOTE_ENV=json.dumps(renv), **(env or {}))

    def crew(self, args, cwd=None, remote_env=None, env=None, timeout=60):
        return subprocess.run([str(BIN), *args], cwd=str(cwd or self.repo),
                              capture_output=True, text=True, timeout=timeout,
                              env=self.environment(remote_env, env))

    def brief_file(self, brief="# Encargo\n\n> agrega el botón X\n", name="brief.md"):
        path = self.tmp / name
        path.write_text(brief, encoding="utf-8")
        return path

    def launch(self, title="Botón X", brief=None, remote_env=None, env=None,
               cwd=None, extra=(), timeout=60):
        path = self.brief_file(brief if brief is not None
                               else "# Encargo\n\n> agrega el botón X\n")
        return self.crew(["launch", "--title", title, "--brief-file", str(path),
                          *extra], cwd=cwd, remote_env=remote_env, env=env,
                         timeout=timeout)

    def seed_agents(self, entries):
        """Pre-populate the fake remote with agents, as a previous session would."""
        state = self.rhome / "paseo-state"
        state.mkdir(parents=True, exist_ok=True)
        agents = []
        for i, entry in enumerate(entries):
            agent_id = f"{i + 1:08d}-aaaa-4bbb-8ccc-{i + 1:012d}"
            agents.append({
                "id": agent_id, "shortId": agent_id[:7],
                "name": entry["name"], "provider": "claude", "thinking": "auto",
                "status": entry.get("status", "running"),
                "cwd": str(self.rhome / "x"), "created": int(time.time()),
                "labels": {"crew": "1",
                           "crew-project": entry.get("slug", SLUG)},
            })
        (state / "agents.json").write_text(json.dumps(agents), encoding="utf-8")
        return agents

    def calls(self):
        if not self.ssh_log.exists():
            return []
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()]

    def paseo_calls(self, cmd=None):
        log = self.rhome / "paseo.log"
        if not log.exists():
            return []
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        return [e for e in entries if cmd is None or e["cmd"] == cmd]

    def agents_on_remote(self):
        path = self.rhome / "paseo-state" / "agents.json"
        return json.loads(path.read_text()) if path.exists() else []

    def pwned(self):
        return sorted(p.name for p in self.rhome.glob("pwned*"))


class CrewTest(CrewFixture):
    # -- launch: transport and labels -----------------------------------------

    def test_launch_sends_brief_on_ssh_stdin_with_labels(self):
        brief = "# Encargo\n\n> agrega el botón X\n"
        proc = self.launch(brief=brief)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["n"], out["title"]), (1, "#1 · Botón X"))
        self.assertRegex(out["id"], AGENT_ID)

        runs = self.paseo_calls("run")
        self.assertEqual(len(runs), 1)
        argv = runs[0]["argv"]
        self.assertEqual(argv[:6], ["run", "--no-wait", "-q", "--provider",
                                    "claude", "--mode"])
        self.assertEqual(argv[6:8], ["auto", "--title"])
        self.assertEqual(argv[8], "#1 · Botón X")
        self.assertEqual(argv[9:11], ["--cwd", str(self.rhome / "innovares" / "innovares-agent")])
        self.assertEqual(argv[11:16], ["--label", "crew=1", "--label",
                                       "crew-project=innovares.datago", "--label"])
        self.assertTrue(argv[16].startswith("crew-req="))
        self.assertRegex(argv[16][len("crew-req="):], UUID4)
        # The brief is the last argument of the remote command, behind `--` and
        # read with "$(cat)" from ssh's stdin; command substitution took the
        # trailing newlines. (The fake paseo sees stdin at EOF because that
        # `cat` already consumed it, as with the real thing.)
        self.assertEqual(argv[17:], ["--", brief.rstrip("\n")])
        self.assertEqual(runs[0]["labels"]["crew-project"], "innovares.datago")

        # It travelled on stdin and never on the local ssh command line.
        for call in self.calls():
            self.assertNotIn("agrega el botón X", " ".join(call["argv"]))
            self.assertNotIn("agrega el botón X", call["cmd"])
        run_calls = [c for c in self.calls() if c["cmd"].startswith("paseo run")]
        self.assertEqual(len(run_calls), 1)
        self.assertTrue(run_calls[0]["cmd"].endswith('-- "$(cat)"'))

    def test_ssh_options_disable_forwarding(self):
        self.assertEqual(self.launch().returncode, 0)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            argv = call["argv"]
            for option in ("BatchMode=yes", "ForwardAgent=no",
                           "ClearAllForwardings=yes", "ConnectTimeout=5",
                           "ForwardX11=no", "-a", "-x"):
                self.assertIn(option, argv)

    def test_number_increments_per_project(self):
        self.seed_agents([{"name": "#4 · algo"},
                          {"name": "#9 · otro tema", "slug": "otra.cosa"}])
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["n"], out["title"]), (5, "#5 · Botón X"))
        self.assertEqual(self.paseo_calls("run")[0]["argv"][8], "#5 · Botón X")

    def test_concurrent_launches_get_distinct_numbers(self):
        path = self.brief_file()
        env = self.environment({"FAKE_PASEO_DELAY": "1"})
        procs = [subprocess.Popen(
            [str(BIN), "launch", "--title", f"tarea {i}", "--brief-file", str(path)],
            cwd=str(self.repo), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        results = [proc.communicate(timeout=60) for proc in procs]
        for proc, (stdout, stderr) in zip(procs, results):
            self.assertEqual(proc.returncode, 0, stderr)
        numbers = sorted(json.loads(stdout)["n"] for stdout, _ in results)
        self.assertEqual(numbers, [1, 2])
        self.assertEqual(len(self.paseo_calls("run")), 2)

    def test_lost_connection_after_send_is_reconciled(self):
        drop = {"match": r"\Apaseo run", "times": 1, "after": 0}
        proc = self.launch(env={"FAKE_SSH_DROP": json.dumps(drop)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["n"], 1)
        # The agent exists once, found by its crew-req label; the launch was
        # not sent again.
        self.assertEqual(len(self.paseo_calls("run")), 1)
        self.assertEqual([a["id"] for a in self.agents_on_remote()], [out["id"]])

    def test_brief_starting_with_dash_arrives_literal_after_double_dash(self):
        brief = "- revisa el botón X\n"
        proc = self.launch(brief=brief)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.paseo_calls("run")[0]["argv"]
        # `--` closes paseo's own options, so the brief cannot be read as one.
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-1], "- revisa el botón X")

    def test_shell_metacharacters_stay_literal(self):
        title = 'x"; touch "$HOME/pwned-title"; echo "'
        brief = '$(touch "$HOME/pwned-brief")\n'
        proc = self.launch(title=title, brief=brief)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.pwned(), [])
        run = self.paseo_calls("run")[0]
        self.assertEqual(run["argv"][8], "#1 · " + title)
        self.assertEqual(run["argv"][-1], '$(touch "$HOME/pwned-brief")')

    # -- launch: refusals before any ssh ---------------------------------------

    def test_secret_in_title_refuses(self):
        proc = self.launch(title="revisa con ghp_" + "a" * 36)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("secreto", proc.stderr)
        self.assertNotIn("ghp_", proc.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.paseo_calls(), [])

    def test_non_utf8_brief_file_refuses(self):
        path = self.tmp / "brief-latin1.md"
        path.write_bytes(b"# Encargo\n\n> ca\xf1a\xf1a\n")
        proc = self.crew(["launch", "--title", "Botón X",
                          "--brief-file", str(path)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no pude leer el brief", proc.stderr)
        self.assertIn("UTF-8", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_empty_brief_refuses(self):
        for brief in ("", " \n\t\n"):
            with self.subTest(brief=brief):
                proc = self.launch(brief=brief)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("brief está vacío", proc.stderr)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.paseo_calls(), [])

    def test_secret_in_brief_file_refuses(self):
        proc = self.launch(brief="contexto\nDATABASE_PASSWORD=hunter2\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("secreto", proc.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.paseo_calls(), [])

    def test_org_not_in_config_refuses(self):
        self.set_origin("git@github.com:Carlososuna11/x.git")
        proc = self.launch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("este proyecto no tiene destino en caracas", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_owner_match_is_case_insensitive(self):
        # GitHub owners are case-insensitive, and so is the config lookup.
        cases = [("git@github.com:innovares/datago.git", "Innovares"),
                 ("git@github.com:INNOVARES/datago.git", "innovares")]
        for origin, owner in cases:
            with self.subTest(origin=origin, owner=owner):
                self.set_origin(origin)
                self.config.write_text(json.dumps({
                    "host": "caracas", "orgs": {owner: REMOTE_CWD},
                }), encoding="utf-8")
                proc = self.launch()
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_origin_refuses(self):
        self.git("remote", "remove", "origin")
        proc = self.launch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no encuentro el repo de esta carpeta", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_secret_in_brief_refuses(self):
        proc = self.launch(brief="revisa esto\nghp_" + "a" * 36 + "\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("secreto", proc.stderr)
        self.assertNotIn("ghp_", proc.stderr)
        self.assertEqual(self.calls(), [])

    # -- brief -----------------------------------------------------------------

    def test_brief_command_prints_contract_last(self):
        sys.path.insert(0, str(ROOT / "lib"))
        from crew.brief import load_contract
        words = self.tmp / "words.md"
        words.write_text("arregla el botón X\n", encoding="utf-8")
        proc = self.crew(["brief", "--words-file", str(words)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("> arregla el botón X", proc.stdout)
        self.assertIn("Innovares/datago", proc.stdout)
        # --base defaults to the current branch.
        self.assertIn("- Rama base: main", proc.stdout)
        self.assertTrue(proc.stdout.rstrip().endswith(load_contract().rstrip()))

    def test_brief_command_forwards_base_and_constraints(self):
        words = self.tmp / "words.md"
        words.write_text("arregla el botón X\n", encoding="utf-8")
        proc = self.crew(["brief", "--words-file", str(words),
                          "--base", "feat/revision",
                          "--decision", "usa la tabla A",
                          "--decision", "nada de migraciones",
                          "--constraint", "no toques el módulo de exports"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("- Rama base: feat/revision", proc.stdout)
        self.assertIn("- usa la tabla A", proc.stdout)
        self.assertIn("- nada de migraciones", proc.stdout)
        self.assertIn("- no toques el módulo de exports", proc.stdout)

    def test_brief_values_may_start_with_a_dash(self):
        words = self.tmp / "words.md"
        words.write_text("arregla el botón X\n", encoding="utf-8")
        proc = self.crew(["brief", "--words-file", str(words), "--base", "main",
                          "--decision", "- no tocar migraciones",
                          "--constraint", "--force nunca"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("- - no tocar migraciones", proc.stdout)
        self.assertIn("- --force nunca", proc.stdout)

    def test_an_option_name_is_never_taken_as_a_value(self):
        words = self.tmp / "words.md"
        words.write_text("x\n", encoding="utf-8")
        proc = self.crew(["brief", "--decision", "--words-file", str(words)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--decision necesita un valor", proc.stderr)

    def test_brief_command_refuses_a_secret(self):
        words = self.tmp / "words.md"
        words.write_text("usa la key ghp_" + "a" * 36 + "\n", encoding="utf-8")
        proc = self.crew(["brief", "--words-file", str(words)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("secreto", proc.stderr)
        self.assertNotIn("ghp_", proc.stderr)


class NumberingAndResolveTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "lib"))

    def test_next_number(self):
        from crew.paseo import next_number
        self.assertEqual(next_number([]), 1)
        self.assertEqual(next_number([{"name": "#4 · algo"}]), 5)
        self.assertEqual(next_number([{"name": "#2 · a"}, {"name": "#10 · b"},
                                      {"name": "sin número"}]), 11)
        self.assertEqual(next_number([{"name": None}, {}]), 1)

    def test_resolve_by_number(self):
        from crew.paseo import CrewError, resolve
        one = {"id": "a", "name": "#5 · uno"}
        self.assertEqual(resolve([one], 5), one)
        with self.assertRaises(CrewError) as cm:
            resolve([], 5)
        self.assertIn("no hay encargo #5 en este proyecto", str(cm.exception))
        two = {"id": "b", "name": "#5 · dos"}
        with self.assertRaises(CrewError) as cm:
            resolve([one, two], 5)
        self.assertIn("hay dos encargos #5; usa el id", str(cm.exception))
        # An entry without an id is not an encargo crew can act on.
        with self.assertRaises(CrewError):
            resolve([{"name": "#7 · sin id"}], 7)


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crew-project-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, env=env, check=check)

    def origin(self, url):
        if self.git("remote", "get-url", "origin", check=False).returncode == 0:
            self.git("remote", "set-url", "origin", url)
        else:
            self.git("remote", "add", "origin", url)

    def test_project_of_reads_ssh_and_https_origins(self):
        from crew.project import project_of
        for url in ("git@github.com:Innovares/datago.git",
                    "git@github.com:Innovares/datago",
                    "ssh://git@github.com/Innovares/datago.git",
                    "https://github.com/Innovares/datago.git",
                    "https://github.com/Innovares/datago"):
            with self.subTest(url=url):
                self.origin(url)
                project = project_of(str(self.repo))
                self.assertIsNotNone(project)
                self.assertEqual((project.owner, project.repo, project.slug),
                                 ("Innovares", "datago", "innovares.datago"))

    def test_no_origin_and_non_github_are_none(self):
        from crew.project import project_of
        self.assertIsNone(project_of(str(self.repo)))
        for url in ("git@gitlab.com:o/r.git", "https://example.com/o/r.git",
                    "git@github.com:o/r/../x.git"):
            with self.subTest(url=url):
                self.origin(url)
                self.assertIsNone(project_of(str(self.repo)))


class IdPatternTest(unittest.TestCase):
    """Agent ids never start with `-`: a dash there reads as an option."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "lib"))

    def test_ids_start_with_a_hex_digit(self):
        from crew.paseo import ID_RE
        for value in ("abc1234", "0" * 36, "1234567-abc", "deadbeef-0001"):
            with self.subTest(id=value):
                self.assertIsNotNone(ID_RE.match(value))
        for value in ("", "-", "-abcdef", "-deadbeef", " abc1234", "xyz1234"):
            with self.subTest(id=value):
                self.assertIsNone(ID_RE.match(value))


class MainErrorTest(unittest.TestCase):
    """main reports local failures as exit codes, never as a traceback."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "lib"))

    def run_main(self, error: BaseException) -> tuple[int, str]:
        from crew import cli
        stderr = io.StringIO()
        with mock.patch.object(cli, "cmd_refresh", side_effect=error), \
                redirect_stderr(stderr):
            code = cli.main(["refresh"])
        return code, stderr.getvalue()

    def test_keyboard_interrupt_exits_130_without_output(self):
        code, err = self.run_main(KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)

    def test_oserror_exits_2_with_a_short_line(self):
        code, err = self.run_main(OSError("boom"))
        self.assertEqual(code, 2)
        self.assertIn("directorio de estado", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("boom", err)


class PaseoLaunchTest(unittest.TestCase):
    """Unit tests for Paseo.launch: what happens before (and after) ssh runs."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "lib"))
        self.tmp = Path(tempfile.mkdtemp(prefix="crew-paseo-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_brief_refuses_before_ssh(self):
        from crew.paseo import CrewError, Paseo
        # The executable does not exist: reaching ssh at all would surface the
        # exec error instead.
        paseo = Paseo("caracas", [str(self.tmp / "no-such-ssh")])
        for brief in ("", " ", "\n\t \n"):
            with self.subTest(brief=brief):
                with self.assertRaises(CrewError) as cm:
                    paseo.launch("t", "~/x", "a.b", brief)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("brief está vacío", str(cm.exception))

    def test_local_ssh_oserror_fails_without_reconcile(self):
        from crew.paseo import CrewError, Paseo

        class Recorder(Paseo):
            def __init__(self, host, ssh):
                super().__init__(host, ssh)
                self.commands = []

            def run(self, cmd, stdin=None, timeout=20):
                self.commands.append(cmd)
                return super().run(cmd, stdin=stdin, timeout=timeout)

        paseo = Recorder("caracas", [str(self.tmp / "no-such-ssh")])
        started = time.monotonic()
        with self.assertRaises(CrewError) as cm:
            paseo.launch("t", "~/x", "a.b", "brief")
        elapsed = time.monotonic() - started
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("no se pudo ejecutar ssh", str(cm.exception))
        # ssh never started, so the request cannot be on the host: exactly one
        # attempt, and no `ls` by crew-req afterwards (nor its 5 s wait).
        self.assertEqual(len(paseo.commands), 1)
        self.assertLess(elapsed, 2.0)


class QuoteCwdTest(unittest.TestCase):
    """quote_cwd refuses what would not stay one safe word on the host."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "lib"))

    def test_refuses_tilde_path_with_dotdot(self):
        from crew.paseo import CrewError, quote_cwd
        for cwd in ("~/a/../b", "~/..", "~/innovares/../secret"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(CrewError) as cm:
                    quote_cwd(cwd)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("destino inválido en la config", str(cm.exception))

    def test_refuses_metacharacters_in_tilde_path(self):
        from crew.paseo import CrewError, quote_cwd
        for cwd in ("~/a;b", "~/a$(id)", '~/a"b', "~/a b"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(CrewError) as cm:
                    quote_cwd(cwd)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("destino inválido en la config", str(cm.exception))

    def test_refuses_relative_path(self):
        from crew.paseo import CrewError, quote_cwd
        with self.assertRaises(CrewError) as cm:
            quote_cwd("innovares/x")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("ruta absoluta", str(cm.exception))

    def test_quotes_absolute_path(self):
        from crew.paseo import quote_cwd
        for cwd in ("/opt/innovares/agent", "/opt/innovares/agent b"):
            with self.subTest(cwd=cwd):
                self.assertEqual(quote_cwd(cwd), shlex.quote(cwd))

    def test_keeps_tilde_expandable(self):
        from crew.paseo import quote_cwd
        self.assertEqual(quote_cwd("~/innovares/innovares-agent"),
                         "~/innovares/innovares-agent")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
