"""Main-graph bridge. Run identity is checkpointed before entering the child graph."""

import threading
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt

from mini.analysis.execution.docker import DockerExecutor
from mini.analysis.models import AnalysisConfig
from mini.analysis.store import EvidenceStore, encode
from mini.analysis.tools.runtime import AnalysisRuntime
from mini.analysis.validation import read_report, submit_report


class AnalysisService:
    def __init__(self, config: AnalysisConfig, model, resources_factory, *, store=None):
        self.config, self.model, self.resources_factory = (
            config,
            model,
            resources_factory,
        )
        self.store = store or EvidenceStore(config.evidence_dir)
        self.store.prune(config.retention_days)
        self.executor = DockerExecutor(config)
        self.cancellations = {}

    def cancel(self, session, run):
        self.store.get(session, run, "run")
        self.cancellations.setdefault(run, threading.Event()).set()
        return {"status": "cancellation_requested", "run_id": run}

    def start(self, session, turn, task, request=""):
        return self.store.create_run(session, turn, task.model_dump(), request)

    def invoke(self, session, run, config):
        record = self.store.get(session, run, "run")
        reports = self.store.list(session, run, "report")
        if record["status"] != "running" and reports:
            return read_report(self.store, session, reports[-1])
        started = time.monotonic()
        runtime = AnalysisRuntime(
            self.store, session, run, self.config, self.resources_factory, self.executor
        )
        runtime.cancel_event = self.cancellations.setdefault(run, threading.Event())
        runtime.remaining = lambda: (
            self.config.run_seconds
            - record["active_seconds"]
            - (time.monotonic() - started)
        )
        owner = self

        class Budget(BaseCallbackHandler):
            raise_error = True

            def on_chat_model_start(self, serialized, messages, **kwargs):
                if runtime.cancel_event.is_set():
                    raise ValueError("cancelled")
                if runtime.remaining() <= 0:
                    raise ValueError("budget_exhausted:wall_clock")
                owner.store.consume(
                    session, run, "models", owner.config.max_model_calls
                )

        child_config = dict(config)
        callbacks = config.get("callbacks")
        if callbacks is not None and not isinstance(callbacks, list):
            callbacks = callbacks.copy()
            callbacks.add_handler(Budget(), inherit=True)
        else:
            callbacks = [*(callbacks or []), Budget()]
        child_config["callbacks"] = callbacks
        child_config["recursion_limit"] = 100
        try:
            from mini.analysis.agent import build_data_analysis_agent

            graph = build_data_analysis_agent(self.model, runtime)
            result = graph.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": encode(
                                {
                                    "task": record["task"],
                                    "input_datasets": [
                                        self.store.get(session, identity, "dataset")
                                        for identity in record["task"][
                                            "input_dataset_ids"
                                        ]
                                    ],
                                }
                            ).decode(),
                        }
                    ]
                },
                config=child_config,
            )
            if result.get("__interrupt__"):
                raise GraphInterrupt(result["__interrupt__"])
            reports = self.store.list(session, run, "report")
            cancelled = runtime.cancel_event.is_set()
            report = (
                reports[-1]
                if reports
                else submit_report(
                    self.store,
                    session,
                    run,
                    [],
                    ["子 Agent 未提交报告"],
                    status="cancelled" if cancelled else "failed",
                )
            )
            if cancelled:
                report["status"] = "cancelled"
            record["status"] = report["status"]
            self.store.save("report", report["report_id"], session, run, report)
            return read_report(self.store, session, report)
        except GraphInterrupt:
            raise
        except Exception as exc:
            cancelled = str(exc) == "cancelled" or runtime.cancel_event.is_set()
            reports = self.store.list(session, run, "report")
            if reports:
                report = read_report(self.store, session, reports[-1])
                report["status"] = "partial" if report["claims"] else "failed"
                report["limitations"].append(str(exc)[:1000])
                self.store.save("report", report["report_id"], session, run, report)
            else:
                report = submit_report(
                    self.store,
                    session,
                    run,
                    [],
                    [str(exc)[:1000]],
                    status="cancelled" if cancelled else "failed",
                )
            if cancelled:
                report["status"] = "cancelled"
            record["status"] = report["status"]
            self.store.save("report", report["report_id"], session, run, report)
            return report
        finally:
            record["active_seconds"] += time.monotonic() - started
            self.store.save("run", run, session, run, record)


def load_analysis_service(model, resources_factory, config_path=None):
    from mini.runtime.resources import CONFIG_PATH, _read_config, create_llm

    path = Path(config_path or CONFIG_PATH)
    if not path.is_file():
        return None
    raw = _read_config(path)
    config = AnalysisConfig.model_validate(raw.get("analysis") or {})
    if not config.enabled:
        return None
    root = Path(config.evidence_dir)
    if not root.is_absolute():
        config = config.model_copy(update={"evidence_dir": str(path.parent / root)})
    if config.model:
        model = create_llm(path, config={**raw, "default_llm": config.model})
    return AnalysisService(config, model, resources_factory)


def load_analysis_store(config_path=None):
    """Open existing archives even when new analyses are disabled; no Docker/DeepAgents."""
    from mini.runtime.resources import CONFIG_PATH, _read_config

    path = Path(config_path or CONFIG_PATH)
    raw = _read_config(path) if path.is_file() else {}
    root = Path((raw.get("analysis") or {}).get("evidence_dir", "var/analysis"))
    if not root.is_absolute():
        root = path.parent / root
    return EvidenceStore(root) if (root / "evidence.sqlite").is_file() else None
