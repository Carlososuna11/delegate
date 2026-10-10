"""Read-only views over the run directories, plus gc.

Spec: docs/specs/2026-10-10-delegate-v2-design.md, "Fase B".

Every run lives in ``contract.run_dir(run_id)`` with a ``request.json`` and,
once finished, a ``result.json`` (shape: ``contract.Result.to_json()``). While
the run is alive ``result.json`` carries ``status == contract.RUNNING``.

Price estimates come from ``prices.tsv`` shipped next to this module. The table
ships with its header and no invented rows: ``cost`` reports ``None`` (a lower
bound) for any run whose model has no row, and says so in ``note``.
"""
from __future__ import annotations

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

def _runs_root() -> Path:
    return contract.state_root() / "runs"


def _run_ids() -> list[str]:
    """Existing run ids, sorted oldest first (the id is time-sortable)."""
    root = _runs_root()
    if not root.is_dir():
        return []
    ids = []
    for entry in os.scandir(root):
        if entry.is_dir() and not entry.name.startswith("."):
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


# --------------------------------------------------------------------------- #
# status / list
# --------------------------------------------------------------------------- #

def status(run_id: str) -> dict:
    """Parsed result.json for a run. Raises FileNotFoundError when it is absent."""
    result = _read_result(run_id)
    if result is None:
        raise FileNotFoundError(
            f"no existe la corrida {run_id!r} o su result.json no se puede leer")
    return result


def list_runs(limit: int = 20) -> list[dict]:
    """Runs newest first, one summary dict each; unreadable dirs are skipped."""
    out: list[dict] = []
    if limit is not None and limit <= 0:
        return out
    for run_id, result in _read_results():
        out.append({
            "run_id": run_id,
            "status": result.get("status"),
            "provider": result.get("provider"),
            "base": result.get("base"),
            "branch": result.get("branch"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
        })
        if limit is not None and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #

def load_prices(path: str | Path | None = None) -> dict[tuple[str, str], dict]:
    """Load prices.tsv into {(provider, model): {price...}}.

    Comments (#) and blank lines are ignored. The first non-comment line is the
    header. Rejects malformed rows with a clear ValueError.
    """
    path = Path(path) if path is not None else PRICES_PATH
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
    return b


def cost(days: int = 7, *, prices_path: str | Path | None = None) -> dict:
    """Aggregate usage and estimated cost over runs started within `days`.

    Unknown usage (None, never 0) is counted in ``runs_with_unknown_usage``.
    A run's cost comes from its result when present, else from prices.tsv when
    the model has a row, else None; with any unknown cost the total is a lower
    bound and ``note`` says so.
    """
    prices = load_prices(prices_path)
    cutoff = time.time() - days * 86400
    buckets: dict[str, dict] = {}
    total_runs = 0
    total_cost = 0.0
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
            total_cost += float(estimate)

    note = "estimación"
    if lower_bound:
        note += ("; el total es una cota inferior: hay corridas sin costo conocido "
                 "(uso ausente o modelo sin precio en la tabla)")
    return {
        "runs": total_runs,
        "by_provider_model": buckets,
        "total_cost_usd_estimate": total_cost,
        "price_table": str(Path(prices_path) if prices_path is not None else PRICES_PATH),
        "note": note,
    }


# --------------------------------------------------------------------------- #
# gc
# --------------------------------------------------------------------------- #

def _git(repo, *args) -> tuple[int, str]:
    """Run git, returning (returncode, message). Never raises for a bad repo."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    if p.returncode != 0:
        return p.returncode, (p.stderr.strip() or p.stdout.strip()
                              or f"git salió con {p.returncode}")
    return 0, ""


def _branch_exists(repo, branch: str) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                            f"refs/heads/{branch}"], capture_output=True, text=True,
                           timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _gc_one(run_id: str, result: dict) -> dict:
    errors: list[str] = []
    request = _read_request(run_id) or {}
    repo = request.get("repo")
    if repo is not None and not isinstance(repo, str):
        repo = None

    # gc only ever removes what the runner itself creates: the worktree at
    # state_root()/worktrees/<run_id> and the branch delegate/<run_id>. A
    # result.json naming anything else is not followed, so an edited or
    # corrupt file cannot point gc at the user's own checkout or `main`.
    expected_wt = contract.state_root() / "worktrees" / run_id
    worktree = result.get("worktree")
    if worktree and Path(worktree) != expected_wt:
        return {"run_id": run_id, "removed": False,
                "error": f"el worktree {worktree!r} no es el que crea delegate-run; no se toca"}
    branch = f"delegate/{run_id}"
    if result.get("branch") not in (None, branch):
        return {"run_id": run_id, "removed": False,
                "error": f"la rama {result.get('branch')!r} no es {branch}; no se toca"}

    if worktree and Path(worktree).exists():
        if not repo:
            errors.append("no se pudo leer 'repo' de request.json para quitar el worktree")
        else:
            rc, msg = _git(repo, "worktree", "remove", "--force", worktree)
            if rc != 0:
                errors.append(f"git worktree remove falló: {msg}")

    if repo and _branch_exists(repo, branch):
        rc, msg = _git(repo, "branch", "-D", branch)
        if rc != 0:
            errors.append(f"git branch -D falló: {msg}")

    if errors:
        return {"run_id": run_id, "removed": False, "error": "; ".join(errors)}
    try:
        shutil.rmtree(contract.run_dir(run_id))
    except OSError as e:
        return {"run_id": run_id, "removed": False,
                "error": f"no se pudo borrar el directorio de la corrida: {e}"}
    return {"run_id": run_id, "removed": True, "error": None}


def gc(days: int = 14, dry_run: bool = False) -> list[dict]:
    """Garbage-collect terminal runs older than `days`.

    Returns one dict per candidate: ``{"run_id", "removed", "error"}``. Only
    runs with a terminal status (contract.STATUSES) and a ``finished_at`` older
    than the cutoff are touched; a running run or one without result.json is
    never touched. With ``dry_run`` nothing is changed and every candidate comes
    back with ``removed`` False. Git failures are reported in ``error`` and keep
    the run dir in place; they never abort the whole gc.
    """
    cutoff = time.time() - days * 86400
    out: list[dict] = []
    for run_id in _run_ids():
        result = _read_result(run_id)
        if result is None:
            continue
        if result.get("status") not in contract.STATUSES:
            continue
        finished = result.get("finished_at")
        if not isinstance(finished, (int, float)) or finished >= cutoff:
            continue
        if dry_run:
            out.append({"run_id": run_id, "removed": False, "error": None})
        else:
            out.append(_gc_one(run_id, result))
    return out
