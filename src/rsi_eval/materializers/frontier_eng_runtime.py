"""Isolated Frontier feedback broker, copied into the grader container.

This module uses only the standard library. Official scorers are fetched at a
pinned revision, verified, and loaded inside an isolated evaluator process.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MAX_SOURCE_BYTES = 80_000


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_files(root, files):
    for name, expected in files.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("missing frozen source: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("frozen source checksum mismatch: " + name)


def check_metrics(metrics):
    if not isinstance(metrics, dict):
        raise RuntimeError("native metrics must be an object")
    for key in ("combined_score", "valid"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError("native metric missing or nonfinite: " + key)
    if metrics["valid"] not in (0, 1):
        raise RuntimeError("native valid metric must be 0 or 1")
    return metrics


def evaluator(root, task_id, candidate, output):
    """Run the unmodified native scorer with a stricter candidate namespace."""
    os.environ["FRONTIER_ENGINEERING_ROOT"] = str(root)
    os.environ["FRONTIER_EVAL_EVALUATOR_TIMEOUT_S"] = "300"
    shared = root / "benchmarks/_shared"
    sys.path.insert(0, str(shared))
    sandbox = load_module("candidate_sandbox", shared / "candidate_sandbox.py")
    # The upstream PID wrapper leaves readonly_paths=None. Preserve its grader
    # and strengthen that compatibility mode to the restricted namespace used
    # by the Inventory task. Only staged inputs and the Python runtime remain.
    original_namespace = sandbox.namespace_command

    def restricted(command, workdir, readonly_paths=None, writable_paths=(), gpu=False):
        return original_namespace(command, workdir,
                                  () if readonly_paths is None else readonly_paths,
                                  writable_paths=writable_paths, gpu=gpu)

    sandbox.namespace_command = restricted
    task = root / "benchmarks" / task_id
    if task_id == "Robotics/PIDTuning":
        native = load_module("frontier_pid_native", task / "frontier_eval/evaluator.py")
        result = native.evaluate(str(candidate), repo_root=root)
        metrics = result["metrics"] if isinstance(result, dict) else result.metrics
        artifacts = result["artifacts"] if isinstance(result, dict) else result.artifacts
        message = str(artifacts.get("error_message", ""))
        if message.startswith(("failed to prepare trusted", "trusted scorer", "pid config not found", "trusted evaluator not found", "trusted config unreadable")):
            raise RuntimeError(message)
    else:
        # This native entrypoint launches its own evaluator, which already
        # requests readonly_paths=() in run_inventory_candidate.
        proc = subprocess.run([sys.executable, str(task / "frontier_eval/run_eval.py")],
                              cwd=task, capture_output=True, text=True, timeout=310)
        if proc.returncode:
            raise RuntimeError("native inventory evaluator failed: " + proc.stderr[-4000:])
        metrics = json.loads((task / "metrics.json").read_text())
        artifacts = json.loads((task / "artifacts.json").read_text())
        if metrics.get("eval_returncode") != 0:
            raise RuntimeError("native inventory environment failure: " + str(artifacts))
        artifacts["native_stdout"] = (task / "eval.stdout.txt").read_text()
        artifacts["native_stderr"] = (task / "eval.stderr.txt").read_text()
        artifacts["outputs"] = {p.name: json.loads(p.read_text()) for p in (task / "output").glob("*.json")}
    write_json(output, {"metrics": check_metrics(metrics), "artifacts": artifacts})


class NativeEvaluator:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["source_root"])
        self.files = config["files"]
        self.task_id = config["task_id"]
        self.relative_candidate = Path("benchmarks") / self.task_id / config["candidate_path"]
        self.baseline = (self.root / self.relative_candidate).read_text()
        self.preflight_done = False

    def preflight(self):
        if self.preflight_done:
            return
        verify_files(self.root, self.files)
        sandbox = load_module("frontier_preflight_sandbox", self.root / "benchmarks/_shared/candidate_sandbox.py")
        with tempfile.TemporaryDirectory(prefix="frontier-preflight-") as directory:
            command = sandbox.namespace_command([sys.executable, "-c", "import sys; print('frontier-isolation-ok')"], Path(directory), readonly_paths=())
            run = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if run.returncode or run.stdout.strip() != "frontier-isolation-ok":
                raise RuntimeError("native bubblewrap preflight failed: " + run.stderr[-2000:])
        # Dependencies are checked outside candidate execution. Failure has no reward.
        needed = ["numpy"] + (["stockpyl.supply_uncertainty"] if self.task_id.startswith("Inventory") else [])
        for module in needed:
            __import__(module)
        self.preflight_done = True

    def __call__(self, source):
        self.preflight()
        verify_files(self.root, self.files)
        with tempfile.TemporaryDirectory(prefix="frontier-evaluation-") as directory:
            root = Path(directory) / "source"
            shutil.copytree(self.root, root)
            candidate = root / self.relative_candidate
            candidate.write_text(source)
            output = Path(directory) / "result.json"
            started = time.monotonic()
            run = subprocess.run([sys.executable, "-I", __file__, "evaluate", str(root), self.task_id,
                                  str(candidate), str(output)], capture_output=True, text=True, timeout=330,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            # A candidate must not change the grader, config or reference files.
            unchanged = {key: value for key, value in self.files.items() if key != str(self.relative_candidate)}
            verify_files(root, unchanged)
            if run.returncode or not output.is_file():
                raise RuntimeError("native evaluator process failed: " + run.stderr[-4000:])
            result = json.loads(output.read_text())
            check_metrics(result.get("metrics"))
            result.update(wall_time_s=time.monotonic() - started, stdout=run.stdout, stderr=run.stderr)
            return result


class Broker:
    """Count evaluations in the grader sidecar, outside the agent filesystem."""
    def __init__(self, config, evaluator_fn=None):
        self.config = config
        if evaluator_fn is None and config.get("engine") == "native-unified":
            full = load_module("frontier_full_runtime", Path(__file__).with_name("frontier_full_runtime.py"))
            evaluator_fn = full.NativeEvaluator(config)
        self.evaluate = evaluator_fn or NativeEvaluator(config)
        self.baseline_source = config.get("baseline_source")
        if self.baseline_source is None:
            self.baseline_source = self.evaluate.baseline
        self.budget = int(config["candidate_budget"])
        self.max_source_bytes = int(config.get('candidate_max_bytes', MAX_SOURCE_BYTES))
        self.records = []
        self.calls = 0
        self.closed = False
        self.error = None

    def _run(self, source, kind):
        if not isinstance(source, str) or len(source.encode()) > self.max_source_bytes:
            raise ValueError("candidate source must be UTF-8 text of at most " + str(self.max_source_bytes) + " bytes")
        record = {"index": len(self.records), "kind": kind, "source": source,
                  "candidate_sha256": hashlib.sha256(source.encode()).hexdigest()}
        try:
            record.update(self.evaluate(source))
            if record.get('infrastructure_error'):
                raise RuntimeError(record['infrastructure_error'])
            check_metrics(record.get("metrics"))
            record["status"] = "graded"
        except Exception as error:
            record.update(status="verifier_error", error=type(error).__name__ + ": " + str(error))
            self.error = record["error"]
        self.records.append(record)
        if self.config.get("state_dir"):
            write_json(Path(self.config["state_dir"]) / "history.json", self.records)
        return record

    def initialize(self):
        if not self.records:
            self._run(self.baseline_source, "baseline")

    def best(self):
        records = [r for r in self.records if r.get("status") == "graded"]
        valid = [r for r in records if r["metrics"]["valid"] == 1]
        return max(valid or records, key=lambda r: r["metrics"]["combined_score"], default=None)

    def status(self):
        self.initialize()
        best = self.best()
        return {"candidate_calls": self.calls, "candidate_budget": self.budget,
                "feedback_remaining": max(0, self.budget - 1 - self.calls),
                "closed": self.closed, "error": self.error,
                "best": None if best is None else {k: best[k] for k in ("index", "candidate_sha256", "metrics")}}

    def feedback(self, source):
        self.initialize()
        if self.closed or self.error:
            raise ValueError("feedback session is closed or has a verifier error")
        if self.calls >= self.budget - 1:
            raise ValueError("feedback budget exhausted; one final candidate slot remains")
        if not isinstance(source, str) or len(source.encode()) > self.max_source_bytes:
            raise ValueError("candidate source exceeds the transport limit")
        self.calls += 1  # Failures consume a call, and are never silently retried.
        record = self._run(source, "feedback")
        public = {k: v for k, v in record.items() if k not in ("source", "artifacts")}
        public["artifacts"] = {k: v for k, v in record.get("artifacts", {}).items()
                               if k in ("candidate_stdout", "candidate_stderr", "error_message", "native_stdout", "native_stderr")}
        return {"evaluation": public, **self.status()}

    def final(self, source):
        if self.closed:
            raise ValueError("session already finalized")
        self.initialize()
        self.closed = True
        if not self.error:
            self.calls += 1
            self._run(source, "final")
        best = self.best()
        # Recompute the selected design using the same frozen grader. This is a
        # verifier check, not another candidate proposal or feedback interaction.
        confirmation = None
        if not self.error and best is not None:
            try:
                confirmation = self.evaluate(best["source"])
                if confirmation.get('infrastructure_error'):
                    raise RuntimeError(confirmation['infrastructure_error'])
                check_metrics(confirmation["metrics"])
                confirmation["score_delta_from_selected"] = confirmation["metrics"]["combined_score"] - best["metrics"]["combined_score"]
                confirmation["valid_changed"] = confirmation["metrics"]["valid"] != best["metrics"]["valid"]
            except Exception as error:
                self.error = type(error).__name__ + ": " + str(error)
        return {"task_id": self.config["task_id"], "protocol": self.config["protocol"],
                "candidate_budget": self.budget, "candidate_calls": self.calls,
                "feedback_calls": sum(r["kind"] == "feedback" for r in self.records),
                "baseline_evaluations": 1, "final_verification_evaluations": int(confirmation is not None),
                "best_index": None if best is None else best["index"],
                "best_metrics": None if best is None else best["metrics"],
                "records": self.records, "confirmation": confirmation, "error": self.error,
                "runtime_evidence": getattr(self.evaluate, "evidence", None)}


def serve(config):
    broker = Broker(config)
    if config.get('engine') == 'native-unified':
        # Dependency and nested isolation checks precede health readiness and
        # model startup; this does not run the potentially expensive baseline.
        broker.evaluate.preflight()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, payload):
            data = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send(200, {"ready": True})
            elif self.path == "/status":
                self.send(200, broker.status())
            else:
                self.send(404, {"error": "unknown endpoint"})

        def do_POST(self):
            try:
                if self.path == "/final" and self.headers.get("X-Frontier-Verifier") != config["verifier_token"]:
                    self.send(403, {"error": "verifier authorization required"})
                    return
                if self.path not in ("/evaluate", "/final"):
                    self.send(404, {"error": "unknown endpoint"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= broker.max_source_bytes * 6 + 1024:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                source = body.get("source")
                result = broker.final(source) if self.path == "/final" else broker.feedback(source)
                self.send(200, result)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send(400, {"error": str(error)})
            except Exception as error:
                broker.error = type(error).__name__ + ": " + str(error)
                self.send(500, {"error": broker.error})

    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


def request(endpoint, payload=None, token=None, timeout=4000):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Frontier-Verifier"] = token
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("http://frontier-grader:8000" + endpoint, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def final_verify(config_path, candidate, output):
    output.mkdir(parents=True, exist_ok=True)
    for name in ("reward.json", "reward.txt", "frontier.json"):
        (output / name).unlink(missing_ok=True)
    config = json.loads(config_path.read_text())
    try:
        try:
            source = candidate.read_text() if candidate.is_file() and candidate.stat().st_size <= config.get('candidate_max_bytes', MAX_SOURCE_BYTES) else None
        except (UnicodeError, OSError):
            source = None
        if source is None:
            source = "# Missing, oversized or unreadable candidate\nraise RuntimeError('invalid candidate file')\n"
        result = request("/final", {"source": source}, config["verifier_token"], timeout=config.get("request_timeout_sec", 1050))
        write_json(output / "frontier.json", result)
        if result.get("error") or not result.get("best_metrics"):
            return 1
        metrics = check_metrics(result["best_metrics"])
        # Harbor's reward channel accepts numeric values only. Raw auxiliary
        # null/non-finite annotations remain preserved in frontier.json.
        numeric_metrics = {k: v for k, v in metrics.items() if type(v) in (int, float) and math.isfinite(v)}
        reward = {**numeric_metrics, "reward": metrics["combined_score"], "native_score": metrics["combined_score"],
                  "candidate_calls": result["candidate_calls"], "feedback_calls": result["feedback_calls"],
                  "candidate_budget": result["candidate_budget"]}
        write_json(output / "reward.json", reward)
        return 0
    except Exception as error:
        write_json(output / "frontier.json", {"status": "verifier_error", "error": type(error).__name__ + ": " + str(error)})
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("serve", "evaluate", "feedback", "verify"))
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    if args.mode == "serve":
        serve(json.loads(Path(args.args[0]).read_text()))
    elif args.mode == "evaluate":
        evaluator(Path(args.args[0]), args.args[1], Path(args.args[2]), Path(args.args[3]))
    elif args.mode == "feedback":
        result = request("/status") if not args.args else request("/evaluate", {"source": Path(args.args[0]).read_text()})
        print(json.dumps(result, indent=2, allow_nan=False))
        return int(bool(result.get("error")))
    else:
        return final_verify(Path(args.args[0]), Path(args.args[1]), Path(args.args[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
