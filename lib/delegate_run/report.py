"""Read-only views over the run directories, plus gc.

Spec: docs/specs/2026-10-10-delegate-v2-design.md, "Fase B".

Every run lives in ``contract.run_dir(run_id)`` with a ``request.json`` and,
once finished, a ``result.json`` (shape: ``contract.Result.to_json()``). While
the run is alive ``result.json`` carries ``status == contract.RUNNING``.

Price estimates come from ``prices.tsv``: ``<state_root>/prices.tsv`` when it
exists (the machine's own, kept out of the repo), else the one shipped next to
this module. The shipped table has its header and no invented rows: a run whose
model has no row has no cost, and ``cost`` says its total is a lower bound.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import contract

PRICES_PATH = Path(__file__).with_name("prices.tsv")

PRICE_FIELDS = (
    "provider", "model",
    "input_per_mtok", "output_per_mtok",
    "cache_read_per_mtok", "cache_write_per_mtok", "as_of",
)
_USAGE_KEYS = ("input", "output", "cache_read", "cache_write")
_GIT_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# run-directory helpers
# --------------------------------------------------------------------------- #

# Abandoned runs are collected only once delegate's own timeout has surely
# stopped whatever the dead runner left behind: the longest request timeout
# plus an hour.
ABANDONED_MIN_AGE = 14400 + 3600


def _runs_root() -> Path:
    return contract.state_root() / "runs"


def _run_ids() -> list[str]:
    """Run ids with a well-formed name, oldest first (the id is time-sortable)."""
    root = _runs_root()
    if not root.is_dir():
        return []
    ids = []
    for entry in os.scandir(root):
        if contract.RUN_ID_RE.match(entry.name) and entry.is_dir(follow_symlinks=False):
            ids.append(entry.name)
    ids.sort()
    return ids


def _read_results() -> list[tuple[str, dict]]:
    """(run_id, result) for every run with a readable result.json; newest first."""
    out = []
    for run_id in reversed(_run_ids()):
        result = _read_result(run_id)
        if result is not None:
            out.append((run_id, result))
    return out


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_result(run_id: str) -> dict | None:
    obj = _read_json(contract.run_dir(run_id) / "result.json")
    return obj if isinstance(obj, dict) else None


def _read_request(run_id: str) -> dict | None:
    obj = _read_json(contract.run_dir(run_id) / "request.json")
    return obj if isinstance(obj, dict) else None


def _observed(run_id: str) -> tuple[dict, bool]:
    """(result as a caller should see it, alive).

    The recorded status is trusted only if it is terminal or the runner is
    alive. A RUNNING record with no runner holding the lock is ABANDONED; a
    missing or broken result.json is RUNNING while the runner lives (it is
    about to write it) and UNREADABLE otherwise.
    """
    rd = contract.run_dir(run_id)
    alive = contract.runner_alive(rd)
    result = _read_result(run_id)
    if result is None:
        result = {"v": contract.SCHEMA_VERSION, "run_id": run_id,
                  "status": contract.RUNNING if alive else contract.UNREADABLE}
    elif result.get("status") == contract.RUNNING and not alive:
        result = dict(result, status=contract.ABANDONED,
                      error="el runner murió sin escribir un status final")
    result["alive"] = alive
    return result, alive


# --------------------------------------------------------------------------- #
# status / list
# --------------------------------------------------------------------------- #

def status(run_id: str) -> dict:
    """result.json as observed now (see _observed). FileNotFoundError if no run."""
    if not contract.run_dir(run_id).is_dir():
        raise FileNotFoundError(f"no existe la corrida {run_id!r}")
    return _observed(run_id)[0]


def list_runs(limit: int = 20) -> list[dict]:
    """Every run dir, newest first, including running and unreadable ones."""
    out: list[dict] = []
    if limit is not None and limit <= 0:
        return out
    for run_id in reversed(_run_ids()):
        result, alive = _observed(run_id)
        out.append({
            "run_id": run_id,
            "status": result.get("status"),
            "alive": alive,
            "provider": result.get("provider"),
            "base": result.get("base"),
            "branch": result.get("branch"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
        })
        if limit is not None and len(out) >= limit:
            break
    return out

# prices
# --------------------------------------------------------------------------- #

def default_prices_path() -> Path:
    local = contract.state_root() / "prices.tsv"
    return local if local.is_file() else PRICES_PATH


def estimate_cost(provider: str, model: str | None, usage, model_usage,
                  prices: dict) -> float | None:
    """USD estimate for one run, or None. Never fills a gap with 0.

    With the harness's per-model breakdown every model that used tokens must
    have a row and all four counts; otherwise the top-level usage is priced
    with the main model's row. Anything missing gives None.
    """
    keys = {"input": "inputTokens", "output": "outputTokens",
            "cache_read": "cacheReadInputTokens", "cache_write": "cacheCreationInputTokens"}
    if isinstance(model_usage, dict) and model_usage:
        total = 0.0
        for name, per in model_usage.items():
            if not isinstance(per, dict):
                return None
            counts = {k: per.get(v) for k, v in keys.items()}
            if not all(_count(c) for c in counts.values()):
                return None
            price = prices.get((provider, name))
            if price is None:
                return None
            total += _compute_cost(price, counts)
        return total
    if model and _usage_known(usage):
        price = prices.get((provider, model))
        if price is not None:
            return _compute_cost(price, usage)
    return None


def load_prices(path: str | Path | None = None) -> dict[tuple[str, str], dict]:
    """Load prices.tsv into {(provider, model): {price...}}.

    Comments (#) and blank lines are ignored. The first non-comment line is the
    header. Rejects malformed rows with a clear ValueError.
    """
    path = Path(path) if path is not None else default_prices_path()
    expected = "\t".join(PRICE_FIELDS)
    rows: dict[tuple[str, str], dict] = {}
    header_seen = False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"no se pudo leer la tabla de precios {path}: {e}") from e

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cols = raw.split("\t")
        if not header_seen:
            if [c.strip() for c in cols] != list(PRICE_FIELDS):
                raise ValueError(
                    f"precios: encabezado inválido en {path} (línea {lineno}); "
                    f"se esperaba: {expected}")
            header_seen = True
            continue
        if len(cols) != len(PRICE_FIELDS):
            raise ValueError(
                f"precios: la línea {lineno} de {path} tiene {len(cols)} columnas, "
                f"se esperaban {len(PRICE_FIELDS)}")
        provider, model = cols[0].strip(), cols[1].strip()
        if not provider or not model:
            raise ValueError(
                f"precios: la línea {lineno} de {path} no tiene provider o model")
        if (provider, model) in rows:
            raise ValueError(
                f"precios: la línea {lineno} de {path} repite {provider}/{model}")
        row: dict = {}
        for name, cell in zip(PRICE_FIELDS[2:6], cols[2:6]):
            try:
                value = float(cell)
            except ValueError:
                value = math.nan
            # float() accepts "nan", "inf" and "-1": none of them is a price.
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"precios: la línea {lineno} de {path} tiene en {name} un valor no "
                    f"numérico, no finito o negativo: {cell!r}")
            row[name] = value
        row["as_of"] = cols[6].strip()
        if not row["as_of"]:
            raise ValueError(
                f"precios: la línea {lineno} de {path} no dice de qué fecha es la tarifa")
        rows[(provider, model)] = row

    if not header_seen:
        raise ValueError(f"precios: {path} no tiene encabezado")
    return rows


def _count(value) -> bool:
    """A token count: a non-negative int, never a bool, a float or a string."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage_known(usage) -> bool:
    return isinstance(usage, dict) and all(_count(usage.get(k)) for k in _USAGE_KEYS)


def _compute_cost(price: dict, usage: dict) -> float:
    return sum(usage[k] / 1_000_000 * price[f"{k}_per_mtok"] for k in _USAGE_KEYS)


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #

def _empty_bucket() -> dict:
    b = {k: 0 for k in _USAGE_KEYS}
    b["runs"] = 0
    b["runs_with_unknown_usage"] = 0
    b["cost_usd_estimate"] = 0.0
    b["_cost_known"] = 0
    return b


def cost(days: int = 7, *, prices_path: str | Path | None = None) -> dict:
    """Aggregate usage and estimated cost over runs started within `days`.

    Unknown usage (None, never 0) is counted in ``runs_with_unknown_usage``.
    A run's cost comes from its result when present, else from prices.tsv when
    the model has a row, else None; with any unknown cost the total is a lower
    bound and ``note`` says so.
    """
    table = Path(prices_path) if prices_path is not None else default_prices_path()
    prices = load_prices(table)
    cutoff = time.time() - days * 86400
    buckets: dict[str, dict] = {}
    total_runs = 0
    total_cost = 0.0
    known_costs = 0
    lower_bound = False

    for _run_id, result in _read_results():
        started = result.get("started_at")
        if not isinstance(started, (int, float)) or started < cutoff:
            continue
        total_runs += 1
        provider = result.get("provider") or "unknown"
        model = result.get("model") or "unknown"
        bucket = buckets.setdefault(f"{provider}/{model}", _empty_bucket())
        bucket["runs"] += 1

        usage = result.get("usage")
        if _usage_known(usage):
            for k in _USAGE_KEYS:
                bucket[k] += usage[k]
        else:
            bucket["runs_with_unknown_usage"] += 1

        estimate = result.get("cost_usd_estimate")
        if not (isinstance(estimate, (int, float)) and not isinstance(estimate, bool)
                and math.isfinite(estimate) and estimate >= 0):
            estimate = None
        if estimate is None:
            price = prices.get((provider, model))
            if price is not None and _usage_known(usage):
                estimate = _compute_cost(price, usage)
        if estimate is None:
            lower_bound = True
        else:
            bucket["cost_usd_estimate"] += float(estimate)
            bucket["_cost_known"] += 1
            total_cost += float(estimate)
            known_costs += 1

    for bucket in buckets.values():
        if bucket.pop("_cost_known") == 0:
            bucket["cost_usd_estimate"] = None
        if bucket["runs_with_unknown_usage"] == bucket["runs"]:
            for k in _USAGE_KEYS:
                bucket[k] = None
    note = "estimación"
    if lower_bound:
        note += ("; el total es una cota inferior: hay corridas sin costo conocido "
                 "(uso ausente o modelo sin precio en la tabla)")
    return {
        "runs": total_runs,
        "by_provider_model": buckets,
        # None, not 0.0, when no run had a known cost: an unknown is not free.
        "total_cost_usd_estimate": None if total_runs and not known_costs else total_cost,
        "price_table": str(table),
        "note": note,
    }


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# gc
# --------------------------------------------------------------------------- #

def _git(repo, *args) -> tuple[int, str]:
    """Run git, returning (returncode, message). Never raises for a bad repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                           stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    if p.returncode != 0:
        return p.returncode, (p.stderr.strip() or p.stdout.strip()
                              or f"git salió con {p.returncode}")
    return 0, ""


def _ref_exists(repo, ref: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", ref)[0] == 0


def _gc_one(run_id: str, result: dict) -> dict:
    errors: list[str] = []
    request = _read_request(run_id) or {}
    repo = request.get("repo")
    if not isinstance(repo, str) or not os.path.isdir(repo):
        repo = None

    # gc only ever removes what the runner itself creates: the worktree at
    # state_root()/worktrees/<run_id>, the branch delegate/<run_id> and the ref
    # refs/delegate/snapshot/<run_id>. A result.json naming anything else is
    # not followed, so an edited or corrupt file cannot point gc at the
    # user's own checkout or `main`.
    expected_wt = contract.state_root() / "worktrees" / run_id
    worktree = result.get("worktree")
    if worktree and Path(worktree) != expected_wt:
        return {"run_id": run_id, "removed": False,
                "error": f"el worktree {worktree!r} no es el que crea delegate-run; no se toca"}
    branch = f"delegate/{run_id}"
    if result.get("branch") not in (None, branch):
        return {"run_id": run_id, "removed": False,
                "error": f"la rama {result.get('branch')!r} no es {branch}; no se toca"}

    if expected_wt.exists():
        if repo:
            rc, msg = _git(repo, "worktree", "remove", "--force", str(expected_wt))
            if rc != 0 and expected_wt.exists():
                errors.append(f"git worktree remove falló: {msg}")
        else:
            # The repo is gone or unknown: the directory is ours, under the
            # state dir, and nothing else can unregister it.
            try:
                shutil.rmtree(expected_wt)
            except OSError as e:
                errors.append(f"no se pudo borrar el worktree: {e}")
    if repo:
        _git(repo, "worktree", "prune")
        for ref in (f"refs/heads/{branch}", f"refs/delegate/snapshot/{run_id}"):
            if _ref_exists(repo, ref):
                rc, msg = _git(repo, "update-ref", "-d", ref)
                if rc != 0:
                    errors.append(f"no se pudo borrar {ref}: {msg}")

    if errors:
        return {"run_id": run_id, "removed": False, "error": "; ".join(errors)}
    try:
        shutil.rmtree(contract.run_dir(run_id))
    except OSError as e:
        return {"run_id": run_id, "removed": False,
                "error": f"no se pudo borrar el directorio de la corrida: {e}"}
    return {"run_id": run_id, "removed": True, "error": None}


def _collectable(run_id: str, cutoff: float, now: float) -> bool:
    result = _read_result(run_id)
    status_ = result.get("status") if result else None
    if status_ in contract.STATUSES:
        finished = result.get("finished_at")
        return isinstance(finished, (int, float)) and finished < cutoff
    # RUNNING with no runner, or no readable result at all: abandoned. Only
    # once delegate's own timeout has certainly ended what the runner left.
    started = result.get("started_at") if result else None
    if not isinstance(started, (int, float)):
        try:
            started = contract.run_dir(run_id).stat().st_mtime
        except OSError:
            return False
    return started < cutoff and started < now - ABANDONED_MIN_AGE


def gc(days: int = 14, dry_run: bool = False) -> list[dict]:
    """Garbage-collect finished runs older than `days`; never a live one.

    Returns one dict per candidate: ``{"run_id", "removed", "error"}``.
    Candidates are runs with a terminal status and ``finished_at`` older than
    the cutoff, plus abandoned runs (RUNNING or unreadable with no runner
    alive) started before the cutoff and more than ABANDONED_MIN_AGE ago.
    Liveness is the run lock: gc takes it exclusively before deciding and holds
    it while deleting, so a live runner, which holds it from its first write
    to its last, is never touched. With ``dry_run`` nothing is changed. Git
    failures are reported in ``error`` and keep the run dir in place.
    """
    now = time.time()
    cutoff = now - days * 86400
    out: list[dict] = []
    for run_id in _run_ids():
        rd = contract.run_dir(run_id)
        try:
            fd = os.open(contract.lock_path(rd),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # a runner holds it: live
            if not _collectable(run_id, cutoff, now):
                continue
            if dry_run:
                out.append({"run_id": run_id, "removed": False, "error": None})
            else:
                out.append(_gc_one(run_id, _read_result(run_id) or {}))
        finally:
            os.close(fd)
    return out
