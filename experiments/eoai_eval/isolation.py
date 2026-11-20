"""macOS Seatbelt boundary for held-out solvers and future improvement roles."""

import json
import platform
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace

from enterprise_ops.agent.harness import HarnessContent
from enterprise_ops.agent.model import ModelError, strict_json

from .execution import ROOT

ROLES = ("solver", "reviewer", "distiller", "candidate-generator")
SANDBOX = Path("/usr/bin/sandbox-exec")
TEMP_ROOT = Path("/private/tmp")


def _runtime() -> tuple[Path, Path, Path]:
    if platform.system() != "Darwin" or not SANDBOX.is_file():
        raise ValueError("Held-out evaluation requires the macOS process sandbox")
    return (
        Path(sys.executable).resolve(), Path(sys.base_prefix).resolve(),
        Path(sysconfig.get_path("purelib")).resolve(),
    )


def _literal(path: Path) -> str:
    return f"(literal {json.dumps(str(path), ensure_ascii=False)})"


def _subpath(path: Path) -> str:
    return f"(subpath {json.dumps(str(path), ensure_ascii=False)})"


def role_command(role: str, area: Path, code: str, *, model_port: int | None = None) -> list[str]:
    """Construct a default-deny command using an evaluator-created role directory."""
    if role not in ROLES or (model_port is not None and role != "solver"):
        raise ValueError("Invalid isolated role or network grant")
    python, base, packages = _runtime()
    area = area.resolve(strict=True)
    if area.parent != TEMP_ROOT or not area.name.startswith("eoai-role-"):
        raise ValueError("Invalid role directory")
    profile = [
        "(version 1)", "(deny default)",
        "(allow file-read-data (literal \"/\"))",  # dyld startup
        '(allow file-read-metadata (literal "/private") (literal "/private/tmp"))',  # SQLite path resolution
        f"(allow process-exec {_literal(python)})",
        f"(allow file-read* {_subpath(base)} {_subpath(packages)} {_subpath(area)} "
        '(subpath "/System/Library") (subpath "/usr/lib") (literal "/dev/urandom"))',
        f"(allow file-write* {_subpath(area / 'scratch')})",
    ]
    if model_port is not None:
        if type(model_port) is not int or not 1 <= model_port <= 65535:
            raise ValueError("Invalid model broker port")
        profile.append(f'(allow network-outbound (remote ip "localhost:{model_port}"))')
    bootstrap = f"import sys; sys.path.extend({[str(area / 'code'), str(area / 'code' / 'src'), str(packages)]!r})\n"
    return [str(SANDBOX), "-p", "\n".join(profile), str(python), "-I", "-S", "-B", "-c", bootstrap + code]


@contextmanager
def role_area():
    _runtime()
    with TemporaryDirectory(prefix="eoai-role-", dir=TEMP_ROOT) as directory:
        area = Path(directory)
        (area / "scratch").mkdir(mode=0o700)
        yield area


def _invoke(role, area, code, data, *, model_port=None, timeout=15):
    return subprocess.run(
        role_command(role, area, code, model_port=model_port), cwd=area, env={},
        input=json.dumps(data), capture_output=True, text=True, timeout=timeout, check=True,
    )


_PROBE = """
import errno
import fcntl
import json
import os
import socket
import sys
data = json.load(sys.stdin)

def denied(operation):
    try:
        operation()
    except OSError as error:
        return error.errno in {errno.EACCES, errno.EPERM}
    return False

result = {label: denied(lambda: open(path, 'rb').close()) for label, path in data["files"].items()}
result['private_listing'] = denied(lambda: os.listdir(data["private_directory"]))
result['private_write'] = denied(lambda: open(data['write_path'], 'xb').close())
result['credential_environment'] = not os.environ
def inherited(fd):
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError as error:
        return error.errno != errno.EBADF
    return True

result['extra_descriptors'] = not any(inherited(fd) for fd in range(3, 32))
result['direct_network'] = denied(lambda: socket.create_connection(('127.0.0.1', data['port']), 1))
blocked = denied(lambda: socket.create_connection(('127.0.0.1', data['broker_port']), 1))
result['model_broker_policy'] = blocked != data['solver']
result['evaluator_spawn'] = denied(lambda: os.posix_spawn('/usr/bin/true', ['/usr/bin/true'], {}))
result['public_input'] = open('public.txt', encoding='utf-8').read() == 'public'
print(json.dumps(result))
"""


def verify_roles(vault: Path, answer_file: Path, credential_file: Path) -> dict:
    """Exercise the actual OS boundary with file and network access attempts."""
    checks = {}
    with socket.socket() as server, socket.socket() as broker:
        for endpoint in (server, broker):
            endpoint.bind(("127.0.0.1", 0))
            endpoint.listen()
        port = server.getsockname()[1]
        broker_port = broker.getsockname()[1]
        data = {
            "files": {"private_answers": str(answer_file), "credential_file": str(credential_file)},
            "private_directory": str(vault), "write_path": str(vault / ".role-write-probe"),
            "port": port, "broker_port": broker_port,
        }
        with TemporaryDirectory(prefix=".access-probe-", dir=vault) as directory:
            for label in ("private_traces", "private_scores"):
                path = Path(directory) / label
                path.write_text("disposable access canary", encoding="utf-8")
                data["files"][label] = str(path)
            for role in ROLES:
                with role_area() as area:
                    (area / "public.txt").write_text("public", encoding="utf-8")
                    solver = role == "solver"
                    checks[role] = json.loads(_invoke(
                        role, area, _PROBE, data | {"solver": solver},
                        model_port=broker_port if solver else None,
                    ).stdout)
    if not all(all(row.values()) for row in checks.values()):
        raise ValueError("An isolated role failed its access probe")
    return checks


@contextmanager
def model_broker(model, deadline):
    """Offer only inference and model identity to the solver process."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self._forward()

        def do_POST(self):
            self._forward()

        def _forward(self):
            operation = self.command, self.path
            if operation not in {("GET", "/api/tags"), ("POST", "/api/chat")}:
                self.send_error(403)
                return
            try:
                body = None
                if self.command == "POST":
                    size = int(self.headers["Content-Length"])
                    if not 0 < size <= 2 * 1024 * 1024:
                        raise ValueError
                    body = self.rfile.read(size)
                    if strict_json(body).get("model") != model.model_id:
                        raise ValueError
                data = model._request(self.command, self.path, deadline, body)
                if operation == ("GET", "/api/tags"):
                    data = {"models": [
                        row for row in data["models"]
                        if row.get("name") == model.model_id or row.get("model") == model.model_id
                    ]}
                result = json.dumps(data).encode()
            except (AttributeError, KeyError, ValueError, TypeError, ModelError):
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def _copy_output(source: Path, destination: Path) -> None:
    if source.resolve(strict=True) != source or not source.is_dir():
        raise ValueError("Unexpected isolated output")
    destination.mkdir(mode=0o700)
    for item in source.iterdir():
        if item.is_symlink() or not item.is_file() or item.stat().st_nlink != 1:
            raise ValueError("Unexpected isolated artifact")
        shutil.copyfile(item, destination / item.name)


def isolated_trial(config, model, harness, snapshot, task, faults, *, run_id, root, scenario):
    """Send a clean seed and allowlisted inputs to a fresh solver process."""

    with role_area() as area, model_broker(model, config.limits.deadline_seconds) as port:
        code = area / "code"
        shutil.copytree(ROOT / "src" / "enterprise_ops", code / "src" / "enterprise_ops",
                        ignore=shutil.ignore_patterns("__pycache__"))
        packages = code / "experiments" / "eoai_eval"
        packages.mkdir(parents=True)
        for name in ("execution.py", "worker.py"):
            shutil.copyfile(Path(__file__).with_name(name), packages / name)
        shutil.copyfile(snapshot.path, area / "seed.sqlite3")
        data = {
            "config": asdict(config), "model_id": model.model_id, "port": port,
            "harness": harness.as_dict() if isinstance(harness, HarnessContent) else None,
            "snapshot": {
                "id": snapshot.id, "digest": snapshot.digest, "profile": snapshot.profile,
                "time": snapshot.clock.instant.isoformat(),
            },
            "task": {
                "id": task.id, "request": task.request, "principal": asdict(task.principal),
                "operation": task.operation, "customer_query": task.customer_query,
                "customer_id": task.customer_id,
                "invoice_ids": sorted(task.invoice_ids) if task.invoice_ids is not None else None,
                "scope": None if task.write_scope is None else {
                    "id": task.write_scope.scope_id,
                    "customer_id": task.write_scope.customer_id,
                    "capabilities": sorted(task.write_scope.capabilities),
                    "invoice_ids": (
                        sorted(task.write_scope.invoice_ids) if task.write_scope.invoice_ids is not None else None
                    ),
                    "restrictions": asdict(task.write_scope.restrictions),
                },
            },
            "faults": [asdict(fault) for fault in faults.steps],
            "scenario": scenario, "run_id": run_id,
        }
        command = "from experiments.eoai_eval.worker import main; main()"
        key = _invoke("solver", area, command, data, model_port=port,
                      timeout=config.limits.deadline_seconds + 20).stdout.strip()
        if not re.fullmatch(re.escape(run_id) + r"-sandbox-[0-9a-f]{32}", key):
            raise ValueError("Invalid isolated trial identity")
        for kind in ("traces", "sandboxes"):
            parent = root / kind
            parent.mkdir(mode=0o700, exist_ok=True)
            _copy_output(area / "scratch" / kind / key, parent / key)
        return SimpleNamespace(
            trace_path=root / "traces" / key,
            sandbox=SimpleNamespace(path=root / "sandboxes" / key / "run.sqlite3"),
        )
