"""Actual HTTP contracts for persisted reports, safe artifact downloads and session checks."""

from fastapi.testclient import TestClient

from mini.analysis.delegation import AnalysisService
from mini.analysis.models import AnalysisConfig
from mini.analysis.validation import submit_report
from mini.web.api import create_app
from mini.web.service import WebSessionService
from tests.test_agent_graph import FakeRoutingLLM
from tests.test_analysis import contribution, fact
from tests.test_analysis import runtime as runtime


def test_report_refresh_evidence_pagination_download_and_cross_session(
    runtime, tmp_path
):
    analysis = AnalysisService(
        AnalysisConfig(), None, lambda: None, store=runtime.store
    )
    service = WebSessionService(
        tmp_path / "web.sqlite",
        llm=FakeRoutingLLM([]),
        query_resources_factory=lambda: None,
        analysis_service=analysis,
    )
    session = service.create_session()["id"]
    runtime.session = session
    # Use a new run owned by this actual web session.
    from mini.analysis.models import AnalysisTask

    runtime.run = runtime.store.create_run(
        session, 1, AnalysisTask(question="synthetic").model_dump()
    )
    out = contribution(runtime)
    report = submit_report(
        runtime.store, session, runtime.run, [fact(out["evidence_id"])]
    )
    with service._db() as db:
        service._add_message(db, session, 1, "assistant", "analysis", report)
    other = service.create_session()["id"]
    with TestClient(create_app(lambda: service)) as client:
        prefix = f"/api/sessions/{session}"
        assert (
            client.get(f"{prefix}/analysis/{runtime.run}").json()[0]["report_id"]
            == report["report_id"]
        )
        assert client.get(f"{prefix}/evidence/{out['evidence_id']}").status_code == 200
        assert (
            client.get(
                f"/api/sessions/{other}/evidence/{out['evidence_id']}"
            ).status_code
            == 404
        )
        ev = runtime.store.get(session, out["evidence_id"])
        ds = runtime.store.get(session, ev["input_dataset_ids"][0])
        endpoint = f"{prefix}/datasets/{ds['dataset_id']}"
        assert client.get(endpoint + "?offset=1&limit=1").json()["rows"] == [["B", 440]]
        assert client.get(endpoint + "?limit=101").status_code == 422
        artifact = f"{prefix}/artifacts/{ds['data_artifact_id']}"
        response = client.get(artifact)
        assert (
            response.status_code == 200
            and response.headers["x-content-type-options"] == "nosniff"
        )
        assert response.headers["content-disposition"].startswith("attachment;")
        assert (
            client.get(
                f"/api/sessions/{other}/artifacts/{ds['data_artifact_id']}"
            ).status_code
            == 404
        )
        # A page refresh reads the persisted report, and missing evidence revokes the visible fact.
        assert client.get(prefix).json()["messages"][0]["payload"]["claims"]
        (runtime.store.objects / ds["data_artifact_id"]).unlink()
        assert not client.get(prefix).json()["messages"][0]["payload"]["claims"]


def test_delete_session_cleans_artifacts_and_checkpoints(runtime, tmp_path):
    analysis = AnalysisService(
        AnalysisConfig(), None, lambda: None, store=runtime.store
    )
    service = WebSessionService(
        tmp_path / "delete-web.sqlite",
        llm=FakeRoutingLLM([]),
        query_resources_factory=lambda: None,
        analysis_service=analysis,
    )
    session = service.create_session()["id"]
    runtime.session = session
    from mini.analysis.models import AnalysisTask

    runtime.run = runtime.store.create_run(
        session, 1, AnalysisTask(question="delete synthetic").model_dump()
    )
    out = contribution(runtime)
    ev = runtime.store.get(session, out["evidence_id"])
    ds = runtime.store.get(session, ev["input_dataset_ids"][0])
    path = runtime.store.objects / ds["data_artifact_id"]
    with TestClient(create_app(lambda: service)) as client:
        assert client.delete(f"/api/sessions/{session}").status_code == 200
        assert client.get(f"/api/sessions/{session}").status_code == 404
        assert not path.exists()


def test_archives_readable_and_deleted_when_new_analysis_disabled(
    runtime, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mini.analysis.delegation.load_analysis_service", lambda *args, **kwargs: None
    )
    service = WebSessionService(
        tmp_path / "disabled-web.sqlite",
        llm=FakeRoutingLLM([]),
        query_resources_factory=lambda: None,
        analysis_store=runtime.store,
    )
    session = service.create_session()["id"]
    runtime.session = session
    from mini.analysis.models import AnalysisTask

    runtime.run = runtime.store.create_run(
        session, 1, AnalysisTask(question="archive").model_dump()
    )
    out = contribution(runtime)
    report = submit_report(
        runtime.store, session, runtime.run, [fact(out["evidence_id"])]
    )
    with service._db() as db:
        service._add_message(db, session, 1, "assistant", "analysis", report)
    with TestClient(create_app(lambda: service)) as client:
        assert service.analysis is None
        assert client.get(f"/api/sessions/{session}").json()["messages"][0]["payload"][
            "claims"
        ]
        assert (
            client.get(
                f"/api/sessions/{session}/evidence/{out['evidence_id']}"
            ).status_code
            == 200
        )
        assert client.delete(f"/api/sessions/{session}").status_code == 200
        assert not runtime.store.list(session, runtime.run, "evidence")
