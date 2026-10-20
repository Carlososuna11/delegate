"""Read-only views over the run directories (status, list, cost, stats), plus gc.

Spec: docs/specs/2026-10-10-delegate-v2-design.md, "Fase B".

Every run lives in ``contract.run_dir(run_id)`` with a ``request.json`` and,
once finished, a ``result.json`` (shape: ``contract.Result.to_json()``). While
the run is alive ``result.json`` carries ``status == contract.RUNNING``.

Price estimates come from ``prices.tsv``: ``<state_root>/prices.tsv`` when it
exists (the machine's own, kept out of the repo), else the one shipped next to
this module. The shipped table carries only prices copied from the providers'
official pages, each row with its source URL and date: a run whose model has
no row has no cost, and ``cost``/``stats`` say their total is a lower bound.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from . import contract

PRICES_PATH = Path(__file__).with_name("prices.tsv")

# The original header. Still accepted, so a machine's own prices.tsv written
# before `source` and the peak columns existed keeps loading.
PRICE_FIELDS = (
    "provider", "model",
    "input_per_mtok", "output_per_mtok",
    "cache_read_per_mtok", "cache_write_per_mtok", "as_of",
)
# The current header: every row also says where its numbers come from and,
# when the provider bills by time of day, which UTC hours are peak and what
# fraction of the peak price the rest of the week costs. The four prices are
# always the PEAK ones: a run whose time is unknown is never under-priced.
PRICE_FIELDS_V2 = PRICE_FIELDS + ("source", "peak_utc", "offpeak_factor")
_PRICE_COLUMNS = PRICE_FIELDS[2:6]
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
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
    """Every run dir, newest first, including running and unreadable ones.

    Newest by started_at: the run_id only orders to the second, and two runs
    in the same second sort by their random suffix.
    """
    if limit is not None and limit <= 0:
        return []
    rows: list[dict] = []
    for run_id in _run_ids():
        result, alive = _observed(run_id)
        started = result.get("started_at")
        if not isinstance(started, (int, float)):
            try:
                started = contract.run_dir(run_id).stat().st_mtime
            except OSError:
                started = 0.0
        rows.append(({
            "run_id": run_id,
            "status": result.get("status"),
            "alive": alive,
            "provider": result.get("provider"),
            "base": result.get("base"),
            "branch": result.get("branch"),
            # Set on the local copy of a Fase C run; None for a local run.
            "host": result.get("host"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
        }, (started, run_id)))
    rows.sort(key=lambda r: r[1], reverse=True)
    rows = [r[0] for r in rows]
    return rows[:limit] if limit is not None else rows


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #

def default_prices_path() -> Path:
    local = contract.state_root() / "prices.tsv"
    return local if local.is_file() else PRICES_PATH


def estimate_cost(provider: str, model: str | None, usage, model_usage,
                  prices: dict, at: float | None = None) -> float | None:
    """USD estimate for one run, or None. Never fills a gap with 0.

    With the harness's per-model breakdown every model that used tokens must
    have a row and all four counts; otherwise the top-level usage is priced
    with the main model's row. Anything missing gives None. `at` (the run's
    start, epoch seconds) picks the peak or off-peak price of a row that has
    a schedule; without it the peak price applies.
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
            total += _compute_cost(price_at(price, at), counts)
        return total
    if model and _usage_known(usage):
        price = prices.get((provider, model))
        if price is not None:
            return _compute_cost(price_at(price, at), usage)
    return None


def price_at(row: dict, at: float | None) -> dict:
    """The row's per-million prices in force at epoch `at`.

    Off-peak only when the row has a schedule, `at` is known and falls outside
    every peak window. Holidays a provider treats as off-peak are not modeled:
    they are priced as peak, which over-estimates and never under-estimates.
    """
    peak = row.get("peak_utc")
    if peak is None or not isinstance(at, (int, float)) or isinstance(at, bool):
        return row
    if _in_peak(peak, at):
        return row
    factor = row["offpeak_factor"]
    out = dict(row)
    for name in _PRICE_COLUMNS:
        out[name] = row[name] * factor
    return out


def _in_peak(peak: tuple, at: float) -> bool:
    days, windows = peak
    when = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    if when.weekday() not in days:
        return False
    hour = when.hour + when.minute / 60 + when.second / 3600
    return any(start <= hour < end for start, end in windows)


def _parse_peak(cell: str):
    """`mon-fri 01-04,06-10` -> (frozenset of weekday ints, ((1, 4), (6, 10))).

    Days: comma-separated names or `a-b` ranges (mon..sun, no wraparound).
    Hours: comma-separated `HH-HH` UTC windows, start inclusive, end exclusive,
    0..24. ValueError on anything else.
    """
    parts = cell.split()
    if len(parts) != 2:
        raise ValueError("se esperaba '<días> <horas>', p. ej. 'mon-fri 01-04,06-10'")
    days: set[int] = set()
    for item in parts[0].split(","):
        lo, _, hi = item.partition("-")
        if lo not in _DAYS or (hi and hi not in _DAYS):
            raise ValueError(f"día inválido: {item!r}")
        a, b = _DAYS.index(lo), _DAYS.index(hi or lo)
        if b < a:
            raise ValueError(f"rango de días al revés: {item!r}")
        days.update(range(a, b + 1))
    windows = []
    for item in parts[1].split(","):
        lo, sep, hi = item.partition("-")
        if not sep or not (lo.isdigit() and hi.isdigit()):
            raise ValueError(f"ventana de horas inválida: {item!r}")
        a, b = int(lo), int(hi)
        if not 0 <= a < b <= 24:
            raise ValueError(f"ventana de horas inválida: {item!r}")
        windows.append((a, b))
    return frozenset(days), tuple(windows)


def load_prices(path: str | Path | None = None) -> dict[tuple[str, str], dict]:
    """Load prices.tsv into {(provider, model): {price...}}.

    Comments (#) and blank lines are ignored. The first non-comment line is the
    header, PRICE_FIELDS_V2 or the older PRICE_FIELDS. Rejects malformed rows
    with a clear ValueError.
    """
    path = Path(path) if path is not None else default_prices_path()
    expected = "\t".join(PRICE_FIELDS_V2)
    rows: dict[tuple[str, str], dict] = {}
    fields: tuple[str, ...] | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"no se pudo leer la tabla de precios {path}: {e}") from e

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cols = raw.split("\t")
        if fields is None:
            header = tuple(c.strip() for c in cols)
            if header not in (PRICE_FIELDS, PRICE_FIELDS_V2):
                raise ValueError(
                    f"precios: encabezado inválido en {path} (línea {lineno}); "
                    f"se esperaba: {expected}")
            fields = header
            continue
        if len(cols) != len(fields):
            raise ValueError(
                f"precios: la línea {lineno} de {path} tiene {len(cols)} columnas, "
                f"se esperaban {len(fields)}")
        provider, model = cols[0].strip(), cols[1].strip()
        if not provider or not model:
            raise ValueError(
                f"precios: la línea {lineno} de {path} no tiene provider o model")
        if (provider, model) in rows:
            raise ValueError(
                f"precios: la línea {lineno} de {path} repite {provider}/{model}")
        row: dict = {}
        for name, cell in zip(_PRICE_COLUMNS, cols[2:6]):
            value = _number(cell)
            # float() accepts "nan", "inf" and "-1": none of them is a price.
            if value is None or value < 0:
                raise ValueError(
                    f"precios: la línea {lineno} de {path} tiene en {name} un valor no "
                    f"numérico, no finito o negativo: {cell!r}")
            row[name] = value
        row["as_of"] = cols[6].strip()
        if not row["as_of"]:
            raise ValueError(
                f"precios: la línea {lineno} de {path} no dice de qué fecha es la tarifa")
        row.update(source=None, peak_utc=None, offpeak_factor=None)
        if fields == PRICE_FIELDS_V2:
            _parse_v2_columns(row, cols[7:], path, lineno)
        rows[(provider, model)] = row

    if fields is None:
        raise ValueError(f"precios: {path} no tiene encabezado")
    return rows


def _number(cell: str) -> float | None:
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _parse_v2_columns(row: dict, cols: list[str], path, lineno: int) -> None:
    source, peak, factor = (c.strip() for c in cols)
    if not source.startswith("https://"):
        raise ValueError(
            f"precios: la línea {lineno} de {path} necesita en source la URL https "
            f"de la página oficial de precios")
    row["source"] = source
    if (peak == "-") != (factor == "-"):
        raise ValueError(
            f"precios: la línea {lineno} de {path} tiene peak_utc y offpeak_factor "
            f"a medias: van los dos o ninguno ('-')")
    if peak == "-":
        return
    try:
        row["peak_utc"] = _parse_peak(peak)
    except ValueError as e:
        raise ValueError(f"precios: la línea {lineno} de {path}, peak_utc: {e}") from None
    value = _number(factor)
    if value is None or not 0 < value <= 1:
        raise ValueError(
            f"precios: la línea {lineno} de {path} tiene un offpeak_factor fuera de "
            f"(0, 1]: {factor!r}")
    row["offpeak_factor"] = value


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


def _harness_json(run_id: str) -> dict | None:
    """The harness's --json object from the run's stdout.log, or None."""
    try:
        raw = (contract.run_dir(run_id) / "stdout.log").read_text(encoding="utf-8",
                                                                 errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _harness_model_usage(run_id: str):
    """`modelUsage` from the run's stdout.log, or None if absent or unreadable."""
    data = _harness_json(run_id)
    return data.get("modelUsage") if data is not None else None


def _run_cost(run_id: str, result: dict, prices: dict) -> float | None:
    """A run's USD estimate: the recorded one when valid, else re-priced.

    Re-priced with the same rule the run used (estimate_cost), from the
    harness's own per-model breakdown and at the run's start time: a table
    filled in later prices old runs, and a run whose models are not all
    priced stays unknown. Pricing its top-level usage as the main model alone
    would invent a number the run itself refused to give.
    """
    estimate = result.get("cost_usd_estimate")
    if (isinstance(estimate, (int, float)) and not isinstance(estimate, bool)
            and math.isfinite(estimate) and estimate >= 0):
        return float(estimate)
    return estimate_cost(result.get("provider") or "unknown",
                         result.get("model") or "unknown", result.get("usage"),
                         _harness_model_usage(run_id), prices,
                         at=result.get("started_at"))


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

        estimate = _run_cost(_run_id, result, prices)
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
# stats
# --------------------------------------------------------------------------- #

def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 1]); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _run_duration(result: dict) -> float | None:
    started, finished = result.get("started_at"), result.get("finished_at")
    if all(isinstance(t, (int, float)) and not isinstance(t, bool)
           for t in (started, finished)) and finished >= started:
        return float(finished - started)
    return None


def _num_turns(run_id: str, result: dict) -> int | None:
    """num_turns from the result, else from the harness JSON (older runs)."""
    turns = result.get("num_turns")
    if not _count(turns):
        data = _harness_json(run_id)
        turns = data.get("num_turns") if data is not None else None
    return turns if _count(turns) else None


def stats(days: int = 7, provider: str | None = None, *,
          prices_path: str | Path | None = None) -> dict:
    """Per provider/model metrics over runs started within `days`.

    For deciding whether delegating pays off: how often a run lands on the
    first try, how long it takes, what it consumes and what it costs. A run
    still alive is left out (counted in ``running``). Unknown is never 0:
    usage and cost averages are over the runs where they are known, and the
    runs where they are not are counted next to them.
    """
    table = Path(prices_path) if prices_path is not None else default_prices_path()
    prices = load_prices(table)
    cutoff = time.time() - days * 86400
    raw: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    running = 0
    hosts: dict[str, int] = {}

    for run_id in reversed(_run_ids()):
        result, alive = _observed(run_id)
        started = result.get("started_at")
        if not isinstance(started, (int, float)) or started < cutoff:
            continue
        if provider is not None and result.get("provider") != provider:
            continue
        if alive:
            running += 1
            continue
        host = result.get("host")
        host = host if isinstance(host, str) and host else "local"
        hosts[host] = hosts.get(host, 0) + 1
        prov = result.get("provider") or "unknown"
        model = result.get("model") or "unknown"
        b = raw.setdefault(f"{prov}/{model}", _raw_bucket(prov, model))
        _add_run(b, run_id, result, prices)
        # A run that died before the harness answered has no model: without
        # the per-provider rollup its failure would vanish from the success
        # rate of the model it was meant to run.
        _add_run(by_provider.setdefault(prov, _raw_bucket(prov, None)),
                 run_id, result, prices)

    groups = {key: _summarize(b) for key, b in sorted(raw.items())}
    providers = {key: _summarize(b) for key, b in sorted(by_provider.items())}
    known = [c for b in raw.values() for c in b["costs"]]
    unknown = sum(b["unknown_cost"] for b in raw.values())
    return {
        "days": days,
        "provider": provider,
        "runs": sum(g["runs"] for g in groups.values()),
        "running": running,
        # Where the runs ran: "local", or the host of a Fase C run whose
        # result was fetched (its local copy carries `host`).
        "by_host": dict(sorted(hosts.items())),
        "by_provider_model": groups,
        "by_provider": providers,
        "cost_usd_estimate": {
            # None, not 0.0, when no run had a known cost.
            "known_total": sum(known) if known else None,
            "known_runs": len(known),
            "unknown_runs": unknown,
        },
        "price_table": str(table),
        "note": ("estimación; costo conocido solo de corridas con uso y precio; "
                 "las demás se cuentan en unknown_runs, nunca como 0"),
    }


def _raw_bucket(provider: str, model: str | None) -> dict:
    return {"provider": provider, "model": model, "statuses": {}, "durations": [],
            "turns": [], "usage": [], "costs": [], "unknown_cost": 0,
            "files": [], "scope_violation_runs": 0}


def _add_run(b: dict, run_id: str, result: dict, prices: dict) -> None:
    st = result.get("status") or contract.UNREADABLE
    b["statuses"][st] = b["statuses"].get(st, 0) + 1
    duration = _run_duration(result)
    if duration is not None:
        b["durations"].append(duration)
    turns = _num_turns(run_id, result)
    if turns is not None:
        b["turns"].append(turns)
    usage = result.get("usage")
    if _usage_known(usage):
        b["usage"].append(usage)
    estimate = _run_cost(run_id, result, prices)
    if estimate is None:
        b["unknown_cost"] += 1
    else:
        b["costs"].append(estimate)
    changed = result.get("changed_files")
    if isinstance(changed, list):
        b["files"].append(len(changed))
    if result.get("scope_violation"):
        b["scope_violation_runs"] += 1


def _summarize(b: dict) -> dict:
    runs = sum(b["statuses"].values())
    usage = b["usage"]
    tokens = {}
    for k in _USAGE_KEYS:
        total = sum(u[k] for u in usage)
        tokens[k] = {"total": total if usage else None,
                     "per_run": total / len(usage) if usage else None}
    costs = b["costs"]
    durations, turns = b["durations"], b["turns"]
    return {
        "provider": b["provider"],
        "model": b["model"],
        "runs": runs,
        "status": dict(sorted(b["statuses"].items())),
        "first_try_success_rate": b["statuses"].get(contract.COMPLETED, 0) / runs,
        "duration_seconds": {"median": statistics.median(durations) if durations else None,
                             "p90": _percentile(durations, 0.9),
                             "known_runs": len(durations)},
        "num_turns": {"median": statistics.median(turns) if turns else None,
                      "known_runs": len(turns)},
        "tokens": tokens,
        "runs_with_unknown_usage": runs - len(usage),
        "cost_usd_estimate": {"known_total": sum(costs) if costs else None,
                              "known_runs": len(costs),
                              "per_run": sum(costs) / len(costs) if costs else None,
                              "unknown_runs": b["unknown_cost"]},
        "files_changed_per_run": (sum(b["files"]) / len(b["files"])) if b["files"] else None,
        "scope_violation_runs": b["scope_violation_runs"],
    }


def _fmt_secs(value) -> str:
    if value is None:
        return "?"
    value = int(round(value))
    if value < 60:
        return f"{value}s"
    if value < 3600:
        return f"{value // 60}m{value % 60:02d}s"
    return f"{value // 3600}h{value % 3600 // 60:02d}m"


def _fmt_tokens(value) -> str:
    if value is None:
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def _fmt_usd(value) -> str:
    if value is None:
        return "?"
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return [line(header)] + [line(r) for r in rows]


def format_stats(out: dict) -> str:
    """The stats dict as a human-readable report, in Spanish."""
    scope = f", proveedor {out['provider']}" if out["provider"] else ""
    lines = [f"delegate-run stats: últimos {out['days']} días{scope}, "
             f"{out['runs']} corridas terminadas"
             + (f" ({out['running']} en curso, no cuentan)" if out["running"] else "")]
    hosts = out.get("by_host") or {}
    if set(hosts) - {"local"}:
        lines.append("Dónde corrieron: " + ", ".join(f"{h} {n}" for h, n in hosts.items()))
    groups = out["by_provider_model"]
    if not groups:
        lines.append("sin corridas en el período")
        return "\n".join(lines) + "\n"

    lines.append("")
    rows = []
    rollup = {f"{k}/* (todas)": g for k, g in out["by_provider"].items()}
    for key, g in list(groups.items()) + list(rollup.items()):
        files = g["files_changed_per_run"]
        rows.append([key, str(g["runs"]), f"{g['first_try_success_rate'] * 100:.0f} %",
                     _fmt_secs(g["duration_seconds"]["median"]),
                     _fmt_secs(g["duration_seconds"]["p90"]),
                     "?" if g["num_turns"]["median"] is None else f"{g['num_turns']['median']:g}",
                     "?" if files is None else f"{files:.1f}",
                     str(g["scope_violation_runs"])])
    lines += _table(["proveedor/modelo", "corridas", "éxito 1.er intento", "duración p50",
                     "p90", "turnos p50", "archivos/corrida", "fuera de scope"], rows)

    lines += ["", "Estados:"]
    for key, g in list(groups.items()) + list(rollup.items()):
        lines.append(f"  {key}: " + ", ".join(f"{s} {n}" for s, n in g["status"].items()))

    lines += ["", "Tokens por corrida (promedio sobre corridas con uso conocido):"]
    rows = []
    for key, g in groups.items():
        t = g["tokens"]
        rows.append([key] + [_fmt_tokens(t[k]["per_run"]) for k in _USAGE_KEYS]
                    + [str(g["runs_with_unknown_usage"])])
    lines += ["  " + r for r in _table(
        ["proveedor/modelo", "entrada", "salida", "caché leída", "caché escrita",
         "sin uso conocido"], rows)]

    lines += ["", "Costo estimado en USD (solo corridas con costo conocido):"]
    rows = []
    for key, g in groups.items():
        c = g["cost_usd_estimate"]
        rows.append([key, _fmt_usd(c["known_total"]), str(c["known_runs"]),
                     _fmt_usd(c["per_run"]), str(c["unknown_runs"])])
    lines += ["  " + r for r in _table(
        ["proveedor/modelo", "total", "corridas", "por corrida", "sin costo conocido"], rows)]

    total = out["cost_usd_estimate"]
    summary = f"Total conocido: {_fmt_usd(total['known_total'])} en {total['known_runs']} corridas"
    if total["unknown_runs"]:
        summary += (f"; {total['unknown_runs']} sin costo conocido (sin uso o sin precio "
                    f"en la tabla): el total es una cota inferior")
    lines += ["", summary, f"Tabla de precios: {out['price_table']}"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# gc
# --------------------------------------------------------------------------- #

def _git(repo, *args) -> tuple[int, str]:
    """Run git, returning (returncode, message). Never raises for a bad repo.

    gc meets repositories delegates have written to: git is found by absolute
    path (a relative PATH entry would find one planted in a worktree) and runs
    with the hardened environment, never the caller's.
    """
    git = contract.which("git")
    if git is None:
        return 127, "no encuentro git en el PATH"
    try:
        p = subprocess.run([git, "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                           stdin=subprocess.DEVNULL, env=contract.hardened_git_env(),
                           cwd="/")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    if p.returncode != 0:
        return p.returncode, (p.stderr.strip() or p.stdout.strip()
                              or f"git salió con {p.returncode}")
    return 0, p.stdout


def _ref_exists(repo, ref: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", ref)[0] == 0


def _remove_worktree(repo, expected_wt: Path, errors: list[str]) -> None:
    """Unregister and delete state_root()/worktrees/<run_id>, and only that.

    A symlink there (or one in place of worktrees/) is never followed: git
    would resolve it and remove whatever worktree it points at, a live run's
    included. The link itself is removed; its target is not touched.
    """
    parent = expected_wt.parent
    if parent.is_symlink():
        errors.append(f"{parent} es un enlace simbólico; no se toca")
        return
    if expected_wt.is_symlink():
        try:
            expected_wt.unlink()
        except OSError as e:
            errors.append(f"no se pudo borrar el enlace {expected_wt}: {e}")
            return
    if repo:
        # Works with the directory gone too: it then only unregisters it. No
        # `git worktree prune`: that sweeps every registration in the repo,
        # including an unrelated worktree that is only moved or unmounted.
        rc, msg = _git(repo, "worktree", "remove", "--force", str(expected_wt))
        if rc != 0 and expected_wt.exists() and "is not a working tree" not in msg:
            errors.append(f"git worktree remove falló: {msg}")
    if expected_wt.exists() and not expected_wt.is_symlink():
        # The repo is gone or does not know it: the directory is ours, under
        # the state dir, and nothing else can unregister it.
        try:
            shutil.rmtree(expected_wt)
        except OSError as e:
            errors.append(f"no se pudo borrar el worktree: {e}")


def _gc_one(run_id: str, result: dict) -> dict:
    errors: list[str] = []
    request = _read_request(run_id) or {}
    repo = request.get("repo")
    if not isinstance(repo, str) or not os.path.isdir(repo):
        repo = None

    # gc only ever removes what the runner itself creates: the worktree at
    # state_root()/worktrees/<run_id>, the branch delegate/<run_id> and the refs
    # refs/delegate/snapshot/<run_id> and refs/delegate/base/<run_id>. A result.json naming anything else is
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

    _remove_worktree(repo, expected_wt, errors)
    if repo:
        # refs/delegate/base/<run_id> exists only in a Fase C mirror: the
        # client pushed the base there so the remote run could resolve it.
        for ref in (f"refs/heads/{branch}", f"refs/delegate/snapshot/{run_id}",
                    f"refs/delegate/base/{run_id}"):
            # A symbolic ref here (a delegate with bash can write one) would
            # make `update-ref -d` delete its target, `main` included.
            # --no-deref deletes the name itself, whatever it holds.
            rc, _ = _git(repo, "symbolic-ref", "-q", ref)
            if rc != 0 and not _ref_exists(repo, ref):
                continue
            rc, msg = _git(repo, "update-ref", "--no-deref", "-d", ref)
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
