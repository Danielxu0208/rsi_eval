"""Full EngDesign native grading in a credential-free verifier sidecar.

The upstream code-execution graders share an interpreter with submitted code.
This adapter preserves that behavior; it is not an anti-reward-hacking boundary.
The worker UID and filesystem are separate from the agent and broker credentials.
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
import signal
import subprocess
import sys
import tempfile
import urllib.request

LIMIT = 4 * 1024 * 1024


def strict_json(text):
    def invalid_constant(value):
        raise ValueError("nonfinite JSON number: " + value)
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number: " + value)
        return result
    return json.loads(text, parse_constant=invalid_constant, parse_float=finite_float)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    if hasattr(value, "item"):
        return plain(value.item())
    if isinstance(value, complex):
        return {"real": plain(value.real), "imag": plain(value.imag)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"native_nonfinite": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"native_repr": repr(value)}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def finite_scalar(value, name):
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("native " + name + " is not a finite numeric scalar")
    return float(value)


def validate_source(source, metadata):
    for name, digest in metadata["files"].items():
        p = source / name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise RuntimeError("native source checksum mismatch: " + name)
    for name, digest in metadata.get("runtime_files", {}).items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("simulator asset checksum mismatch: " + name)


def native_probe(source, output, metadata):
    """Check dependencies before the agent starts, without a candidate or model."""
    report = {"status": "verifier_error", "record_id": metadata["record_id"]}
    try:
        validate_source(source, metadata)
        os.chdir(source)
        sys.path.insert(0, str(source))
        schema = load("output_structure", source / "output_structure.py")
        evaluator = load("engdesign_native_evaluator", source / "evaluate.py")
        if not hasattr(schema.Response_structure, "model_validate") or not callable(evaluator.evaluate_llm_response):
            raise RuntimeError("native schema or evaluation entry point is unavailable")
        required = {"iverilog": ["iverilog", "vvp"], "octave": ["octave"],
                    "webots": ["xvfb-run", "/usr/local/webots/webots"]}.get(metadata["runtime_family"], [])
        if metadata["record_id"] in ("AM_02", "AM_03"):
            required.append("ffmpeg")
        for binary in required:
            if shutil.which(binary) is None:
                raise RuntimeError("required native executable is missing: " + binary)
        if metadata["runtime_family"] == "webots":
            version = subprocess.run(["/usr/local/webots/webots", "--version"], capture_output=True,
                                     text=True, timeout=30, check=True)
            report["simulator_version"] = (version.stdout + version.stderr).strip()
        report.update(status="ready", checked_executables=required, source_files=metadata["files"])
        write(output, report)
        return 0
    except Exception as exc:
        report.update(error=type(exc).__name__ + ": " + str(exc))
        write(output, report)
        return 1


def native_grade(source, submission, output, metadata):
    """One isolated worker invocation; every imported scoring file is pinned."""
    report = {"status": "verifier_error", "record_id": metadata["record_id"],
              "source_files": metadata["files"], "schema_validated": False,
              "native_called": False, "native_completed": False}
    report["compatibility_patches"] = metadata.get("compatibility_patches", [])
    report["original_source_files"] = metadata.get("original_files", metadata["files"])
    try:
        validate_source(source, metadata)
        os.chdir(source)
        sys.path.insert(0, str(source))
        schema = load("output_structure", source / "output_structure.py")
        evaluator = load("engdesign_native_evaluator", source / "evaluate.py")
        from pydantic import ValidationError
        try:
            if submission.stat().st_size > LIMIT:
                raise ValueError("submission exceeds 4 MiB")
            response = schema.Response_structure.model_validate(strict_json(submission.read_text(encoding="utf-8")))
        except (ValueError, UnicodeError, FileNotFoundError, IsADirectoryError, ValidationError) as exc:
            report.update(status="invalid_submission", passed=False, native_score=None,
                          confidence=None, details=str(exc), rewards={"reward": 0.0})
            write(output, report)
            return 0
        report.update(schema_validated=True, submission_sha256=hashlib.sha256(submission.read_bytes()).hexdigest())
        commands = []
        def audit(event, args):
            if event == "subprocess.Popen" and len(commands) < 64:
                commands.append({"executable": str(args[0]), "arguments": plain(args[1]),
                                 "cwd": str(args[2]) if args[2] is not None else None})
        sys.addaudithook(audit)
        report.update(native_called=True, subprocesses=commands)
        native = evaluator.evaluate_llm_response(response)
        report["native_completed"] = True
        if not isinstance(native, (tuple, list)) or len(native) != 4:
            raise ValueError("native grader did not return its four-field verdict")
        passed, details, score, confidence = native
        if hasattr(passed, "item"):
            passed = passed.item()
        # Oct2Py transports MATLAB logical values as numeric 0/1 scalars.
        if type(passed) is not bool:
            if metadata["runtime_family"] == "octave" and passed in (0, 1):
                passed = bool(passed)
            else:
                raise ValueError("native pass result is not boolean")
        report["native_return"] = plain(native)
        if metadata["runtime_family"] == "webots" and isinstance(details, dict) and details.get("error") == "Simulation failed to run":
            raise RuntimeError("native Webots simulation could not run")
        # Several official graders return unknown score/confidence when a
        # schema-valid candidate fails execution. Preserve that failed verdict.
        score = None if score is None and passed is False else finite_scalar(score, "score")
        confidence = None if confidence is None and passed is False else finite_scalar(confidence, "confidence")
        rewards = {"reward": float(passed)}
        if score is not None:
            rewards["native_score"] = score
        if confidence is not None:
            rewards["confidence"] = confidence
        report.update(status="graded", passed=passed, native_score=score, confidence=confidence,
                      details=plain(details), rewards=rewards)
        write(output, report)
        return 0
    except Exception as exc:
        report.update(error=type(exc).__name__ + ": " + str(exc))
        write(output, report)
        return 1


def worker(config, payload, *, probe=False):
    frozen = Path(config["source_dir"])
    for name, digest in config["files"].items():
        p = frozen / name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise RuntimeError("frozen source checksum mismatch: " + name)
    with tempfile.TemporaryDirectory(prefix="engdesign-grade-") as directory:
        work = Path(directory)
        source = work / "source"
        shutil.copytree(frozen, source)
        write(work / "submission.json", payload)
        write(work / "manifest.json", {key: config[key] for key in
              ("record_id", "runtime_family", "files", "original_files", "runtime_files", "compatibility_patches") if key in config})
        shutil.copyfile(__file__, work / "runtime.py")
        # Only this invocation's staging tree is writable by the worker.
        for p in (work, *work.rglob("*")):
            os.chown(p, 65534, 65534)
            p.chmod(0o700 if p.is_dir() else 0o600)
        env = {"PATH": os.environ["PATH"], "HOME": str(work), "USER": "nobody", "MPLBACKEND": "Agg",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "QT_QPA_PLATFORM": "offscreen", "LIBGL_ALWAYS_SOFTWARE": "1",
               "XDG_CACHE_HOME": str(work / ".cache")}
        # Webots' image may require a display server. Its native invocation and
        # simulation remain unchanged; Xvfb supplies a headless display.
        command = [sys.executable, "-I", str(work / "runtime.py"), "probe" if probe else "grade", str(source),
                   str(work / "submission.json"), str(work / "result.json"), str(work / "manifest.json")]
        if config["runtime_family"] == "webots":
            command = ["xvfb-run", "-a", *command]
        def isolate():
            os.setsid()
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
        def tail(path):
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 16000))
                return stream.read().decode("utf-8", errors="replace")
        stdout_path, stderr_path = work / "stdout.log", work / "stderr.log"
        timed_out = False
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(command, cwd=source, env=env, stdout=stdout_file,
                                    stderr=stderr_file, preexec_fn=isolate)
            try:
                proc.wait(timeout=min(config["timeout_sec"], 60) if probe else config["timeout_sec"])
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        stdout, stderr = tail(stdout_path), tail(stderr_path)
        if timed_out:
            return {"status": "verifier_error", "error": "native grading exceeded the verifier time budget",
                    "stdout": stdout, "stderr": stderr}
        path = work / "result.json"
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            result = {"status": "verifier_error", "error": "native grader produced no readable verdict"}
        else:
            result = json.loads(path.read_text())
        result.update(stdout=stdout, stderr=stderr, worker_returncode=proc.returncode)
        if proc.returncode != 0:
            result.pop("rewards", None)
            result["status"] = "verifier_error"
        return result


def serve(config):
    finished = False
    try:
        readiness = worker(config, None, probe=True)
    except Exception as exc:
        readiness = {"status": "verifier_error", "error": type(exc).__name__ + ": " + str(exc)}
    ready = readiness.get("status") == "ready"
    print(json.dumps({"engdesign_preflight": readiness}, allow_nan=False), flush=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def send(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self.send((200 if ready else 503) if self.path == "/health" else 404,
                      {"ready": ready and self.path == "/health", "error": readiness.get("error")})
        def do_POST(self):
            nonlocal finished
            if self.path != "/grade" or self.headers.get("X-EngDesign-Verifier") != config["verifier_token"]:
                self.send(403, {"error": "verifier authorization required"})
                return
            if not ready:
                self.send(503, {"error": "native dependency preflight failed"})
                return
            if finished:
                self.send(409, {"error": "verifier already used"})
                return
            finished = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= LIMIT * 6 + 1024:
                    raise ValueError("invalid request size")
                payload = strict_json(self.rfile.read(length))
                self.send(200, worker(config, payload))
            except Exception as exc:
                self.send(200, {"status": "verifier_error", "error": type(exc).__name__ + ": " + str(exc)})
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


def verify(config_path, submission, output):
    output.mkdir(parents=True, exist_ok=True)
    for name in ("reward.json", "reward.txt", "verdict.json"):
        (output / name).unlink(missing_ok=True)
    try:
        config = json.loads(config_path.read_text())
        try:
            if submission.stat().st_size > LIMIT:
                raise ValueError("submission exceeds 4 MiB")
            payload = strict_json(submission.read_text())
        except (ValueError, UnicodeError, OSError):
            payload = None  # Transport invalidity is scored by the schema branch.
        request = urllib.request.Request("http://engdesign-grader:8000/grade", data=json.dumps(payload, allow_nan=False).encode(),
                  headers={"Content-Type": "application/json", "X-EngDesign-Verifier": config["verifier_token"]})
        with urllib.request.urlopen(request, timeout=870) as response:
            result = json.load(response)
        write(output / "verdict.json", result)
        if result.get("status") not in ("graded", "invalid_submission") or not isinstance(result.get("rewards"), dict):
            return 1
        write(output / "reward.json", result["rewards"])
        return 0
    except Exception as exc:
        write(output / "verdict.json", {"status": "verifier_error", "error": type(exc).__name__ + ": " + str(exc)})
        return 1


def main():
    mode, *args = sys.argv[1:]
    if mode == "serve":
        return serve(json.loads(Path(args[0]).read_text()))
    if mode == "grade":
        return native_grade(Path(args[0]), Path(args[1]), Path(args[2]), json.loads(Path(args[3]).read_text()))
    if mode == "probe":
        return native_probe(Path(args[0]), Path(args[2]), json.loads(Path(args[3]).read_text()))
    if mode == "verify":
        return verify(*(Path(a) for a in args))
    raise ValueError("unknown runtime mode")


if __name__ == "__main__":
    raise SystemExit(main())
