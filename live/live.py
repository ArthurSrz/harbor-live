"""Live view of a harbor job as tau ~ pi(x; S) -> y_hat -> f(y_hat, y).

Run from the dir you launch `harbor run` in:  python3 /path/to/live/live.py [port]
(The `harbor` zsh function in ~/.zshrc does this automatically on `harbor run`.)
Serves live.html and /state (JSON of the newest job in jobs/, or ?job=<name>).
"""

import ast
import html
import json
import re
import sys
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = (
    Path.cwd()
)  # where `harbor run` was launched; jobs/ and task paths are relative to it
JOBS = ROOT / "jobs"
PAGE = Path(__file__).with_name("live.html")
CACHE = Path.home() / ".cache" / "harbor" / "tasks" / "packages"
MAX_STR = 2000
MAX_JUDGE_INPUT = 40000  # the file the judge reads is shown in full-ish, not clipped to MAX_STR


def clip(v):
    """Truncate long strings anywhere in a JSON value so the page stays light."""
    if isinstance(v, str):
        return (
            v if len(v) <= MAX_STR else v[:MAX_STR] + f"… [+{len(v) - MAX_STR} chars]"
        )
    if isinstance(v, list):
        return [clip(x) for x in v]
    if isinstance(v, dict):
        return {k: clip(x) for k, x in v.items()}
    return v


def read_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_text(p):
    try:
        return p.read_text().strip()
    except OSError:
        return None


def stream_events(p):
    events = []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # partial line still being written, or non-JSON noise
    return events


def docx_text(p):
    """Paragraph text of a .docx (stdlib stand-in for the markitdown conversion the judge sees)."""
    try:
        xml = zipfile.ZipFile(p).read("word/document.xml").decode()
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    paras = (re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para) for para in xml.split("</w:p>"))
    return "\n".join(html.unescape("".join(runs)) for runs in paras if runs)


def artifact_text(t, path):
    """A file from the agent's sandbox, as harbor copied it into <trial>/artifacts/."""
    p = t / "artifacts" / path.lstrip("/")
    if not p.is_file():
        return None
    return docx_text(p) if p.suffix == ".docx" else read_text(p)


def judge_state(t, tests_dir):
    """LLM-as-a-judge (harbor-rewardkit): rubric from tests/judge.toml, verdicts from reward-details.json."""
    try:
        cfg = tomllib.loads((tests_dir / "judge.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    details = read_json(t / "verifier" / "reward-details.json") or {}
    verdicts = {
        c.get("name"): c
        for r in details.values()
        if isinstance(r, dict)
        for c in r.get("criteria", [])
    }
    criteria = []
    for c in cfg.get("criterion", []):
        v = verdicts.get(c.get("name"), {})
        criteria.append({
            "name": c.get("name"),
            "description": c.get("description", ""),
            "type": c.get("type", "binary"),
            "files": c.get("files", []),
            "raw": v.get("raw"),
            "value": v.get("value"),
            "weight": v.get("weight", c.get("weight", 1.0)),
            "reasoning": v.get("reasoning"),
        })
    files = sorted({f for c in criteria for f in c["files"]})
    return {
        "model": cfg.get("judge", {}).get("judge"),
        "mode": cfg.get("judge", {}).get("mode", "batch"),
        "aggregation": cfg.get("scoring", {}).get("aggregation", "weighted_mean"),
        "threshold": cfg.get("scoring", {}).get("threshold"),
        "criteria": criteria,
        "files": files,
    }


def pytest_checks(tests_dir, ctrf_tests):
    """Each pytest function with the assertions it checks, matched to its ctrf.json verdict."""
    results = {r.get("name", "").rsplit("::", 1)[-1]: r for r in ctrf_tests}
    checks = []
    for f in sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []:
        src = read_text(f) or ""
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in tree.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_")):
                continue
            r = results.get(fn.name, {})
            checks.append({
                "name": fn.name,
                "file": f.name,
                "doc": ast.get_docstring(fn),
                "asserts": [ast.get_source_segment(src, n) for n in ast.walk(fn) if isinstance(n, ast.Assert)],
                "source": ast.get_source_segment(src, fn),
                "status": r.get("status"),
                "message": r.get("message") or r.get("trace"),
                "duration": r.get("duration"),
            })
    return checks


def verifier_state(t, task_dir, result):
    """Evidence for each step of harbor's Verifier.verify() (harbor/verifier/verifier.py)."""
    v = t / "verifier"
    try:
        timeout = (
            tomllib.loads((task_dir / "task.toml").read_text())
            .get("verifier", {})
            .get("timeout_sec")
        )
    except (OSError, tomllib.TOMLDecodeError):
        timeout = None
    stdout = read_text(v / "test-stdout.txt")
    tail = MAX_STR - 50  # keep the end of the log: that's where pytest's verdict is
    return {
        "mode": result.get("verifier_environment_mode"),
        "timeout": timeout,
        "started": v.is_dir() and any(v.iterdir()),
        "stdout": stdout
        if stdout is None or len(stdout) <= tail
        else "…" + stdout[-tail:],
        "ctrf": (v / "ctrf.json").exists(),
        "rubric": (v / "reward-details.json").exists(),
        "reward_file": next(
            (p.name for p in (v / "reward.json", v / "reward.txt") if p.exists()), None
        ),
        "rewards": (result.get("verifier_result") or {}).get("rewards"),
    }


def trial_state(t):
    config = read_json(t / "config.json") or {}
    result = read_json(t / "result.json") or {}
    task = config.get("task", {})
    if task.get("path"):
        task_dir = ROOT / task["path"]
    else:  # registry task (org/name@ref): harbor caches it under packages/<org>/<name>/<hash>/
        # ponytail: newest cached version, not necessarily the one this trial ran
        versions = sorted(
            CACHE.joinpath(task.get("name", "")).glob("*/task.toml"),
            key=lambda p: p.stat().st_mtime,
        )
        task_dir = versions[-1].parent if versions else ROOT / ".missing-task"
    agent = config.get("agent", {})

    events = stream_events(t / "agent" / "claude-code.txt")
    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
        {},
    )
    final = next((e for e in events if e.get("type") == "result"), None)
    steps = [e for e in events if e.get("type") in ("assistant", "user")]

    reward = read_text(t / "verifier" / "reward.txt")
    ctrf = read_json(t / "verifier" / "ctrf.json") or {}
    tests_dir = task_dir / "tests"
    y_files = [
        {"name": p.name, "content": read_text(p)}
        for p in (sorted(tests_dir.iterdir()) if tests_dir.is_dir() else [])
        if p.is_file()
    ]

    judge = judge_state(t, tests_dir)
    checks = pytest_checks(tests_dir, ctrf.get("results", {}).get("tests", []))

    if result.get("finished_at"):
        phase = "done"
    elif (t / "verifier").is_dir() and any((t / "verifier").iterdir()):
        phase = "scoring"
    elif events:
        phase = "rollout"
    elif (t / "agent" / "setup").exists():
        phase = "agent_setup"
    else:
        phase = "env_setup"

    state = clip(
        {
            "name": t.name,
            "phase": phase,
            "timings": {
                k: result.get(k)
                for k in (
                    "environment_setup",
                    "agent_setup",
                    "agent_execution",
                    "verifier",
                )
            },
            "x": read_text(task_dir / "instruction.md"),
            "task": task.get("path") or task.get("name"),
            "pi": {
                "agent": agent.get("name"),
                "model": agent.get("model_name") or init.get("model"),
            },
            "U": init.get("tools", []),
            "S": init.get("skills", []),
            "tau": steps,
            "y_hat": final
            and {
                k: final.get(k)
                for k in (
                    "result",
                    "num_turns",
                    "total_cost_usd",
                    "duration_ms",
                    "is_error",
                )
            },
            "y": {"files": y_files, "judge": judge, "checks": checks,
                  "kind": "judge" if judge else "tests" if checks else None},
            "f": float(reward)
            if reward not in (None, "")
            else ((result.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward"
            ),
            "verifier": verifier_state(t, task_dir, result),
            "error": result.get("exception_info"),
        }
    )
    if judge:  # what the judge actually read: the agent's file, as text
        state["y"]["judge"]["input"] = {
            f: (txt[:MAX_JUDGE_INPUT] if txt else None) for f in judge["files"] for txt in [artifact_text(t, f)]
        }
    return state


def job_state(name=None):
    jobs = sorted(p for p in JOBS.iterdir() if p.is_dir()) if JOBS.is_dir() else []
    job = JOBS / name if name else (jobs[-1] if jobs else None)
    if not job or not job.is_dir():
        return {"job": None, "jobs": [p.name for p in jobs], "trials": []}
    trials = [
        trial_state(t) for t in sorted(job.iterdir()) if (t / "config.json").exists()
    ]
    scores = [t["f"] for t in trials if t["f"] is not None]
    return {
        "job": job.name,
        "jobs": [p.name for p in jobs],
        "trials": trials,
        "R": sum(scores) / len(scores) if scores else None,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/state":
            job = parse_qs(url.query).get("job", [None])[0]
            if job and ("/" in job or job.startswith(".")):
                return self.send_error(400)
            body, ctype = json.dumps(job_state(job)).encode(), "application/json"
        elif url.path == "/":
            body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"harbor live view: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
