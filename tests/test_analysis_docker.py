"""Opt-in actual Docker tests. Missing Docker/image is a skip, never a pass."""

import os
import shutil
import subprocess
import threading
import time

import pytest

from mini.analysis.execution.docker import DockerExecutor
from mini.analysis.models import AnalysisConfig
from tests.test_analysis import PARAMS, dataset
from tests.test_analysis import runtime as runtime

pytestmark = pytest.mark.docker


@pytest.fixture(autouse=True)
def require_docker():
    if os.environ.get("RUN_ANALYSIS_DOCKER") != "1":
        pytest.skip("set RUN_ANALYSIS_DOCKER=1 to run real container tests")
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    try:
        for args in (["info"], ["image", "inspect", "betterchatbi-analysis:1"]):
            if subprocess.run(
                ["docker", *args], capture_output=True, timeout=10
            ).returncode:
                pytest.skip("Docker daemon or analysis image unavailable")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker daemon unavailable")


def test_actual_python_contribution_and_png(runtime):
    ids = [dataset(runtime), dataset(runtime, "baseline")]
    source = f"""from analysis_sdk import load_dataset, emit_result, save_figure, save_dataset
import matplotlib.pyplot as plt
import numpy as np
np.random.seed(0)
c=load_dataset({ids[0]!r}).set_index('region')['sales']
b=load_dataset({ids[1]!r}).set_index('region')['sales']
d=c-b
total=float(d.sum())
emit_result({{'delta':total,'change_rate':total/float(b.sum()),'A':float(d['A'])/total,'B':float(d['B'])/total}})
fig,ax=plt.subplots(); ax.bar(d.index,d.values); save_figure(fig,'contribution.png')
save_dataset(d.rename('delta').reset_index(),'deltas',units={{'delta':'元'}})
"""
    output = runtime.execute(
        "run_python_analysis",
        ids,
        {"result_units": {"/result/change_rate": "ratio"}},
        "python",
        source,
    )
    assert output["status"] == "success", output
    assert output["result"] == dict(delta=-200, change_rate=-0.2, A=1.2, B=-0.2)
    ev = runtime.store.evidence("session", output["evidence_id"])
    assert ev["parameters"]["image_digest"].startswith("sha256:")
    assert ev["parameters"]["dependencies"]["pandas"] == "2.2.3"
    assert any(
        runtime.store.get("session", a, "artifact")["media_type"] == "image/png"
        for a in ev["artifact_ids"]
    )
    assert output["output_dataset_ids"]
    assert runtime.store.get("session", output["output_dataset_ids"][0])["units"] == {
        "delta": "元"
    }
    from mini.analysis.validation import submit_report
    from tests.test_analysis import fact

    report = submit_report(
        runtime.store, "session", runtime.run, [fact(output["evidence_id"])]
    )
    assert report["claims"][0]["verification_status"] == "supported"
    fixed = runtime.execute("contribution_analysis", ids, PARAMS, "fixed")
    assert fixed["result"]["change_rate"] == output["result"]["change_rate"]


@pytest.mark.parametrize(
    "source,error",
    [
        ("this is invalid python !", "python_execution_error"),
        ('print("no emit")', "protocol_error"),
        (
            'from analysis_sdk import emit_result; emit_result({"x":float("nan")})',
            "python_execution_error",
        ),
        ('open("/output/result.json","w").write("{bad json")', "protocol_error"),
        (
            'import pickle; from analysis_sdk import emit_result; emit_result({}); open("/output/bad.pkl","wb").write(pickle.dumps({}))',
            "invalid_output_type",
        ),
        (
            'import os; os.symlink("/etc/passwd","/output/escape"); from analysis_sdk import emit_result; emit_result({})',
            "invalid_file",
        ),
    ],
)
def test_protocol_failures(source, error):
    output = DockerExecutor(AnalysisConfig()).run(source, {})
    assert output["status"] == "error" and error in output["error"], output


def test_timeout_and_logs_are_bounded():
    out = DockerExecutor(AnalysisConfig(timeout_seconds=1)).run("while True: pass", {})
    assert out["status"] == "timeout", out
    out = DockerExecutor(AnalysisConfig(log_bytes=1024)).run(
        'while True: print("x"*4096)', {}
    )
    assert out["status"] == "error" and "output_limit" in out["error"]
    assert len(out["stdout"].encode()) <= 1024


def test_isolation_files_network_and_credentials():
    source = """from analysis_sdk import emit_result
import os,socket
assert not os.path.exists('/var/run/docker.sock')
assert 'OPENAI_API_KEY' not in os.environ and 'AWS_SECRET_ACCESS_KEY' not in os.environ
assert os.getuid()==65533
try: socket.create_connection(('1.1.1.1',443),timeout=.3); raise AssertionError('network available')
except OSError: pass
try: open('/etc/forbidden-write','w'); raise AssertionError('root writable')
except OSError: pass
try: open('/input/manifest.json','w'); raise AssertionError('input writable')
except OSError: pass
try: open('/Users/liuyuchen/.codex/auth.json'); raise AssertionError('host visible')
except OSError: pass
emit_result({'isolation_checks':True})
"""
    out = DockerExecutor(AnalysisConfig()).run(source, {})
    assert out["status"] == "success", out


def test_syntax_failure_then_repair_keeps_both(runtime):
    ids = [dataset(runtime)]
    fail = runtime.execute("run_python_analysis", ids, {}, "bad", "invalid !!!")
    good = runtime.execute(
        "run_python_analysis",
        ids,
        {"result_units": {"/result/change_rate": "ratio"}},
        "fixed",
        'from analysis_sdk import emit_result; emit_result({"change_rate":-.2})',
    )
    assert fail["status"] == "error" and good["status"] == "success"
    ev = runtime.store.evidence("session", good["evidence_id"])
    assert fail["evidence_id"] in ev["parent_evidence_ids"]
    assert runtime.store.get("session", fail["evidence_id"])["status"] == "error"


def test_output_files_and_memory_limits():
    out = DockerExecutor(AnalysisConfig(output_bytes=16384, file_bytes=8192)).run(
        'open("/output/large","wb").write(b"x"*100000)', {}
    )
    assert out["status"] == "error"
    out = DockerExecutor(AnalysisConfig(memory_mib=64)).run(
        "x = bytearray(1024*1024*512)", {}
    )
    assert out["status"] == "error" and out["error"] in (
        "out_of_memory",
        "python_execution_error",
    ), out


def test_cancellation_kills_container():
    event = threading.Event()
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            DockerExecutor(AnalysisConfig()).run("while True: pass", {}, event)
        )
    )
    worker.start()
    time.sleep(1)
    event.set()
    worker.join(timeout=15)
    assert not worker.is_alive() and result["status"] == "cancelled", result


def test_program_cannot_forge_a_successful_exit():
    source = 'from analysis_sdk import emit_result; emit_result({"x":1}); open("/output/.exit","w").write("0"); exec("while True: pass")'
    out = DockerExecutor(AnalysisConfig(timeout_seconds=1)).run(source, {})
    assert out["status"] == "timeout" and not out["result"], out


def test_fake_png_is_not_imported():
    source = 'from analysis_sdk import emit_result; emit_result({}); open("/output/fake.png","wb").write(b"\\x89PNG\\r\\n\\x1a\\n")'
    out = DockerExecutor(AnalysisConfig()).run(source, {})
    assert out["status"] == "error" and "invalid_output_type" in out["error"]


def test_output_file_count_and_sdk_path_traversal():
    out = DockerExecutor(AnalysisConfig(file_count=1)).run(
        'from analysis_sdk import emit_result; emit_result({}); open("/output/extra.json","w").write("{}")',
        {},
    )
    assert out["status"] == "error" and "output_limit" in out["error"]
    out = DockerExecutor(AnalysisConfig()).run(
        'from analysis_sdk import save_dataset; import pandas as pd; save_dataset(pd.DataFrame({"x":[1]}),"../escape")',
        {},
    )
    assert out["status"] == "error" and out["error"] == "python_execution_error"


def test_host_restart_interrupts_orphan_and_preserves_source(runtime, monkeypatch):
    ids = [dataset(runtime)]

    class Crash(BaseException):
        pass

    class SimulatedCrash:
        def run(self, source, data, cancel, max_seconds, on_started):
            on_started(
                "a" * 64,
                "sha256:test",
                {i: runtime.store.get("session", i)["content_sha256"] for i in ids},
            )
            raise Crash()

    runtime.executor = SimulatedCrash()
    with pytest.raises(Crash):
        runtime.execute(
            "run_python_analysis", ids, {}, "crash-source", 'print("attempted program")'
        )
    ev = next(
        e
        for e in runtime.store.list("session", runtime.run, "evidence")
        if e["operation_id"] == "crash-source"
    )
    assert ev["status"] == "running" and ev["parameters"]["source_artifact_id"]
    recovered = []
    runtime.executor = DockerExecutor(AnalysisConfig())
    monkeypatch.setattr(
        runtime.executor, "recover", lambda cid: recovered.append(cid) or "interrupted"
    )
    out = runtime.execute(
        "run_python_analysis", ids, {}, "crash-source", 'print("attempted program")'
    )
    assert out["status"] == "error" and recovered == ["a" * 64]
    assert "execution_interrupted" in out["error"]
