"""Docker-only executor: no host exec fallback, fixed argv, bounded tmpfs and logs."""

import base64
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from pathlib import Path

from mini.analysis.store import encode


def valid_png(data):
    """Validate PNG chunk boundaries/CRC and cap decoded image dimensions before import."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, image_data = 8, False
    try:
        while offset < len(data):
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            kind = data[offset + 4 : offset + 8]
            end = offset + 12 + length
            if end > len(data):
                return False
            body = data[offset + 8 : end - 4]
            if zlib.crc32(kind + body) != struct.unpack(">I", data[end - 4 : end])[0]:
                return False
            if offset == 8:
                if kind != b"IHDR" or length != 13:
                    return False
                width, height = struct.unpack(">II", body[:8])
                if not width or not height or width * height > 16_000_000:
                    return False
            image_data = image_data or kind == b"IDAT"
            if kind == b"IEND":
                return length == 0 and end == len(data) and image_data
            offset = end
    except (ValueError, struct.error):
        return False
    return False


class DockerExecutor:
    def __init__(self, config):
        self.config = config
        self._active = set()
        self._lock = threading.Lock()

    def cancel_all(self):
        with self._lock:
            active = list(self._active)
        for cid in active:
            self._cmd(["kill", cid], check=False)

    def recover(self, cid):
        """A restarted host explicitly interrupts an orphan; an in-process duplicate stays running."""
        if not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{64}", cid):
            return "interrupted"
        with self._lock:
            if cid in self._active:
                return "active"
        state = self._cmd(["inspect", cid], check=False)
        if not state.returncode:
            self._cmd(["kill", cid], check=False)
            self._cmd(["rm", "-f", cid], check=False)
        return "interrupted"

    def _cmd(self, args, *, check=True, timeout=15):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
        return result

    def run(
        self,
        source: str,
        datasets: dict,
        cancel=None,
        max_seconds=None,
        on_started=None,
    ):
        c = self.config
        execution_seconds = (
            min(c.timeout_seconds, max_seconds)
            if max_seconds is not None
            else c.timeout_seconds
        )
        logs = [bytearray(), bytearray()]
        exceeded = threading.Event()
        cid = None
        output = dict(
            status="error",
            error="executor_unavailable",
            result={},
            files=[],
            stdout="",
            stderr="",
            exit_code=None,
            image_digest=None,
            dependencies={},
            log_truncated=False,
            seed=0,
            input_hashes={
                k: hashlib.sha256(encode(v)).hexdigest() for k, v in datasets.items()
            },
        )
        if not shutil.which("docker"):
            return output
        try:
            self._cmd(["info", "--format", "{{.ServerVersion}}"], timeout=5)
            image = json.loads(self._cmd(["image", "inspect", c.image]).stdout)[0]
            output["image_digest"] = image["Id"]
            with tempfile.TemporaryDirectory(prefix="betterchatbi-analysis-") as temp:
                root = Path(temp)
                root.chmod(0o755)
                manifest = {}
                for i, (identity, snapshot) in enumerate(datasets.items()):
                    name = f"dataset-{i}.json"
                    (root / name).write_bytes(encode(snapshot))
                    manifest[identity] = name
                (root / "manifest.json").write_bytes(encode(manifest))
                (root / "program.py").write_text(source)
                for path in root.iterdir():
                    path.chmod(0o644)
                args = [
                    "create",
                    "--network=none",
                    "--read-only",
                    "--user=65532:65532",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    f"--env=ANALYSIS_TIMEOUT={execution_seconds}",
                    f"--cpus={c.cpus}",
                    f"--memory={c.memory_mib}m",
                    f"--memory-swap={c.memory_mib}m",
                    f"--pids-limit={c.pids}",
                    f"--ulimit=fsize={c.file_bytes}:{c.file_bytes}",
                    "--log-driver=none",
                    "--tmpfs",
                    f"/output:rw,noexec,nosuid,nodev,size={c.output_bytes},uid=65533,gid=65533,mode=755",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
                    "--mount",
                    f"type=bind,src={root},dst=/input,readonly",
                    image["Id"],
                ]
                cid = self._cmd(args).stdout.decode().strip()
                with self._lock:
                    self._active.add(cid)
                self._cmd(["start", cid])
                if on_started is not None:
                    on_started(cid, output["image_digest"], output["input_hashes"])
                program = "import random,numpy as np,runpy; random.seed(0); np.random.seed(0); runpy.run_path('/input/program.py',run_name='__main__')"
                process = subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        "--user=65533:65533",
                        "-e",
                        "PYTHONPATH=/opt/analysis",
                        "-e",
                        "MPLBACKEND=Agg",
                        "-e",
                        "MPLCONFIGDIR=/tmp/mpl",
                        "-e",
                        "OPENBLAS_NUM_THREADS=1",
                        "-e",
                        "OMP_NUM_THREADS=1",
                        "-e",
                        "PYTHONHASHSEED=0",
                        cid,
                        "python",
                        "-c",
                        program,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                def drain(pipe, target):
                    while chunk := pipe.read(4096):
                        room = max(0, c.log_bytes - len(target))
                        target.extend(chunk[:room])
                        if len(chunk) > room:
                            exceeded.set()
                    pipe.close()

                threads = [
                    threading.Thread(target=drain, args=(pipe, buf), daemon=True)
                    for pipe, buf in zip((process.stdout, process.stderr), logs)
                ]
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + execution_seconds
                # Inspector runs trusted code; limits are also enforced by Docker's tmpfs/fsize/pids controls.
                inspect = """import pathlib,json
p=pathlib.Path('/output'); f=list(p.iterdir())
print(json.dumps({'count':len(f),'invalid':any(x.is_symlink() or not x.is_file() for x in f),'sizes':[x.lstat().st_size for x in f]}))
"""
                while True:
                    if cancel and cancel.is_set():
                        output.update(status="cancelled", error="cancelled")
                        break
                    if exceeded.is_set():
                        output.update(error="output_limit", log_truncated=True)
                        break
                    if time.monotonic() >= deadline:
                        output.update(status="timeout", error="execution_timeout")
                        break
                    result = self._cmd(
                        ["exec", cid, "python", "-c", inspect], check=False, timeout=5
                    )
                    if result.returncode:
                        state = json.loads(self._cmd(["inspect", cid]).stdout)[0][
                            "State"
                        ]
                        output["container_state"] = {
                            k: state.get(k)
                            for k in ("Status", "Running", "ExitCode", "Error")
                        }
                        output["inspector_error"] = result.stderr.decode(
                            errors="replace"
                        )[-2000:]
                        if state.get("Status") in ("created", "running"):
                            time.sleep(0.05)
                            continue
                        if state.get("ExitCode") == 124:
                            output.update(status="timeout", error="execution_timeout")
                        else:
                            output["error"] = (
                                "out_of_memory"
                                if state.get("OOMKilled")
                                else "container_stopped"
                            )
                        break
                    state = json.loads(result.stdout)
                    if (
                        state["invalid"]
                        or state["count"] > c.file_count
                        or sum(state["sizes"]) > c.output_bytes
                        or any(s > c.file_bytes for s in state["sizes"])
                    ):
                        output["error"] = "output_limit_or_invalid_file"
                        break
                    if process.poll() is not None:
                        # This exit code comes from Docker's exec process, never a user-writable file.
                        output["exit_code"] = process.returncode
                        if output["exit_code"]:
                            output["error"] = (
                                "out_of_memory"
                                if process.returncode == 137
                                else "python_execution_error"
                            )
                            break
                        # Stop generated descendants before importing a stable snapshot. The sentinel has a different UID.
                        cleanup = """import os,signal,pathlib,time
for _ in range(8):
 for p in pathlib.Path('/proc').iterdir():
  try:
   if p.name.isdigit() and int(p.name)!=os.getpid() and p.stat().st_uid==65533: os.kill(int(p.name),signal.SIGKILL)
  except (OSError,ValueError): pass
 time.sleep(.005)
"""
                        self._cmd(
                            ["exec", "--user=65533:65533", cid, "python", "-c", cleanup]
                        )
                        # Serialize only files already inspected as regular, bounded files.
                        collect = """import pathlib,json,base64
p=pathlib.Path('/output'); print(json.dumps({f.name:base64.b64encode(f.read_bytes()).decode() for f in p.iterdir() if f.is_file() and not f.is_symlink()}))
"""
                        files = json.loads(
                            self._cmd(["exec", cid, "python", "-c", collect]).stdout
                        )
                        files = {
                            k: base64.b64decode(v, validate=True)
                            for k, v in files.items()
                        }
                        if (
                            len(files) > c.file_count
                            or sum(map(len, files.values())) > c.output_bytes
                        ):
                            raise ValueError("output_limit")
                        structured = json.loads(
                            files.pop("result.json"),
                            parse_constant=lambda s: (_ for _ in ()).throw(
                                ValueError(s)
                            ),
                        )
                        if not isinstance(structured, dict):
                            raise ValueError("result_must_be_dict")
                        encode(structured)
                        for name, data in files.items():
                            if name.endswith(".png") and valid_png(data):
                                continue
                            if name.endswith(".dataset.json"):
                                snapshot = json.loads(data)
                                encode(snapshot)
                                if (
                                    {"columns", "rows"}
                                    <= set(snapshot)
                                    <= {"columns", "rows", "units"}
                                    and isinstance(snapshot.get("units", {}), dict)
                                    and all(
                                        isinstance(k, str)
                                        and isinstance(v, str)
                                        and k in snapshot["columns"]
                                        for k, v in snapshot.get("units", {}).items()
                                    )
                                    and isinstance(snapshot["columns"], list)
                                    and isinstance(snapshot["rows"], list)
                                ):
                                    continue
                            raise ValueError("invalid_output_type")
                        versions = "import importlib.metadata as m,json;print(json.dumps({k:m.version(k) for k in ['pandas','numpy','scipy','matplotlib']}))"
                        output.pop("inspector_error", None)
                        output.update(
                            status="success",
                            error=None,
                            result=structured,
                            files=list(files.items()),
                            dependencies=json.loads(
                                self._cmd(
                                    ["exec", cid, "python", "-c", versions]
                                ).stdout
                            ),
                        )
                        break
                    time.sleep(0.05)
                self._cmd(["kill", cid], check=False)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                for thread in threads:
                    thread.join(timeout=2)
                if exceeded.is_set() and output["status"] == "success":
                    output.update(
                        status="error",
                        error="output_limit",
                        log_truncated=True,
                        result={},
                        files=[],
                    )
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
            KeyError,
        ) as exc:
            output.update(
                status="error",
                error=f"{('protocol_error' if output['image_digest'] else 'executor_unavailable')}: {str(exc)[:1000]}",
            )
        finally:
            if cid:
                self._cmd(["rm", "-f", cid], check=False)
                with self._lock:
                    self._active.discard(cid)
        output["stdout"], output["stderr"] = [
            bytes(b).decode(errors="replace") for b in logs
        ]
        return output
