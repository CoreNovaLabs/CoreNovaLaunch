"""Production release gate regressions. All backends/AWS/network are local fakes."""
from __future__ import annotations

import copy
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from corenova import candidates, prodcheck, publish
from corenova.appspec import AppSpec
from corenova.backend import DirBackend
from corenova.util import file_sha, utcnow
from tests.test_publish_two_phase import sample_manifest


class RecordingBackend(DirBackend):
    def __init__(self, root):
        super().__init__(root)
        self.writes = []
        self.fail = ""

    def put(self, key, data, content_type="application/json"):
        if self.fail and self.fail in key:
            raise RuntimeError("injected upload failure")
        self.writes.append(key)
        super().put(key, data, content_type)


@pytest.fixture
def setup(tmp_path):
    backend = RecordingBackend(tmp_path / "objects")
    cfg = SimpleNamespace(region="us-east-1", template_bucket="", template_s3_region="",
                          r2_public_base_url="", root=tmp_path, output_dir=tmp_path / "data")
    path = tmp_path / "ghost.yaml"
    raw = "deployment:\n  production_contract:\n    checks: [data_dir_write]\n"
    path.write_text(raw)
    spec = AppSpec(name="ghost", path=path, raw=raw, data=yaml.safe_load(raw))
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "home.png").write_bytes(b"new screenshot")
    m = sample_manifest()
    m["verified_at"] = m["website"]["verified_at"] = utcnow()
    m["container"]["digest"] = "sha256:" + "a" * 64
    m["config"] = {"template_revision": "b" * 40, "app_config_revision": file_sha(path)}
    m["website"]["deploy"].update(production_contract={"checks": ["data_dir_write"]},
                                  template={"url": "https://example.com/template.yaml", "revision": "b" * 40})
    return backend, cfg, spec, shots, m


def stable_seed(backend):
    m = sample_manifest(verification_run_id="99")
    m["website"]["verification_run_id"] = "99"
    backend.put("verified/ghost/current.json", candidates.encode(m["website"]))
    for key in ("verified/ghost/versions/v6.61.0.json", "verified/ghost/versions/index.json",
                "verified/index.json", "screenshots/ghost/v6.61.0/home.png"):
        backend.put(key, b"previous stable bytes")
    backend.writes.clear()


def snapshot(backend):
    if not backend.root.exists():
        return {}
    return {str(p.relative_to(backend.root)): p.read_bytes()
            for p in backend.root.rglob("*") if p.is_file()
            and not str(p.relative_to(backend.root)).startswith("candidates/")}


def ready(setup):
    backend, cfg, _, shots, m = setup
    result = publish.publish(backend, cfg, m, shots, "<html/>")
    assert result.candidate_ready and not result.current_written
    envelope, stored = candidates.load(backend, result.candidate)
    return result.candidate, envelope, stored


def evidence(cfg, spec, ref, m):
    names = ["stack_created", "template_match", "public_access_denied", "health_external", "data_dir_write"]
    return {**ref, "production_run_id": "200", "production_run_attempt": "1",
            "image_reference": prodcheck.pinned_image(m), "template_revision": "b" * 40,
            "all_passed": True, "cleanup_confirmed": True, "session_id": "real-session-fixture",
            "started_at": utcnow(), "finished_at": utcnow(),
            "parameters": prodcheck.plan(cfg, spec, m, run_id="200").parameters,
            "checks": [{"name": n, "passed": True} for n in names]}


def promote(setup, ref, report, **kwargs):
    b, cfg, spec, _, _ = setup
    return candidates.promote(b, cfg, ref, report, production_run_id="200", spec=spec,
                              check_template=kwargs.get("check_template", lambda m: None))


def test_candidate_same_version_has_zero_stable_writes(setup):
    backend, *_ = setup
    stable_seed(backend)
    before = snapshot(backend)
    ref, _, m = ready(setup)
    assert snapshot(backend) == before
    assert all(k.startswith("candidates/") for k in backend.writes)
    assert ref["manifest_sha256"] == candidates.digest(m)
    assert "/candidates/ghost/100/1/" in m["website"]["screenshots"][0]["url"]
    assert "verification_run_attempt" in m


@pytest.mark.parametrize("failed_key", ["/screenshots/", "/report.html", "/manifest.json", "/candidate.json"])
def test_failed_upload_never_writes_any_stable_object(setup, failed_key):
    backend, cfg, _, shots, m = setup
    stable_seed(backend)
    before = snapshot(backend)
    backend.fail = failed_key
    with pytest.raises(RuntimeError):
        publish.publish(backend, cfg, m, shots, "report")
    assert snapshot(backend) == before
    assert all(k.startswith("candidates/") for k in backend.writes)


def test_failed_local_check_is_zero_write(setup):
    backend, cfg, _, shots, m = setup
    m["checks"]["tests_passed"] = False
    assert not publish.publish(backend, cfg, m, shots, "report").candidate_ready
    assert backend.writes == []


def test_same_run_attempt_is_immutable_new_attempt_is_separate(setup, monkeypatch):
    b, cfg, _, shots, m = setup
    first, _, _ = ready(setup)
    old = b.get(first["key"])
    with pytest.raises(ValueError, match="stale"):
        publish.publish(b, cfg, m, shots, "changed")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    second = publish.publish(b, cfg, m, shots, "new attempt")
    assert second.candidate_ready and second.candidate["key"] != first["key"]
    assert b.get(first["key"]) == old


@pytest.mark.parametrize("field,value", [
    ("verification_id", "other"), ("app_version", "v9.0.0"),
    ("verification_run_id", "99"), ("verification_run_attempt", "9"),
    ("manifest_sha256", "0" * 64), ("production_run_id", "199"),
    ("production_run_attempt", "2"), ("template_revision", "c" * 40),
    ("image_reference", "other@sha256:abc"), ("cleanup_confirmed", False),
    ("all_passed", False), ("session_id", ""), ("started_at", "2020-01-01T00:00:00Z"),
])
def test_wrong_or_incomplete_evidence_rejected_without_stable_writes(setup, field, value):
    b, cfg, spec, _, _ = setup
    stable_seed(b)
    before = snapshot(b)
    ref, _, m = ready(setup)
    report = evidence(cfg, spec, ref, m)
    report[field] = value
    with pytest.raises(ValueError):
        promote(setup, ref, report)
    assert snapshot(b) == before


@pytest.mark.parametrize("mutation", ["missing-check", "public-params", "template", "manifest", "artifact"])
def test_forged_green_is_not_a_gate(setup, mutation):
    b, cfg, spec, _, _ = setup
    ref, env, m = ready(setup)
    report = evidence(cfg, spec, ref, m)
    if mutation == "missing-check":
        report["checks"].pop()
    elif mutation == "public-params":
        report["parameters"]["AllowedWebCidr"] = "0.0.0.0/0"
    elif mutation == "template":
        m["config"]["template_revision"] = "c" * 40
        b.put(env["manifest_key"], candidates.encode(m))
    elif mutation == "manifest":
        m["container"]["digest"] = "sha256:" + "c" * 64
        b.put(env["manifest_key"], candidates.encode(m))
    else:
        b.put(next(iter(env["objects"])), b"tampered")
    with pytest.raises(ValueError):
        promote(setup, ref, report)
    assert snapshot(b) == {}


def test_superseded_candidate_and_version_downgrade_rejected(setup):
    b, cfg, spec, shots, original = setup
    ref, _, m = ready(setup)
    report = evidence(cfg, spec, ref, m)
    newer = copy.deepcopy(original)
    for obj in (newer, newer["website"]):
        obj["verification_run_id"] = "101"
        obj["app_version"] = "v6.62.0"
    assert publish.publish(b, cfg, newer, shots, "new").candidate_ready
    with pytest.raises(ValueError, match="superseded"):
        promote(setup, ref, report)
    original["verification_run_id"] = "102"
    with pytest.raises(ValueError, match="rollback"):
        publish.publish(b, cfg, original, shots, "old version")
    assert snapshot(b) == {}


def test_expired_candidate_rejected(setup):
    b, cfg, spec, _, _ = setup
    ref, env, m = ready(setup)
    env["created_at"] = "2020-01-01T00:00:00Z"
    b.put(ref["key"], candidates.encode(env))
    with pytest.raises(ValueError, match="expired"):
        promote(setup, ref, evidence(cfg, spec, ref, m))
    assert snapshot(b) == {}


@pytest.mark.parametrize("change", ["current-hold", "source-hold", "cas"])
def test_new_manual_hold_cannot_be_cleared_by_old_run(setup, change):
    b, cfg, spec, _, _ = setup
    stable_seed(b)
    ref, _, m = ready(setup)
    report = evidence(cfg, spec, ref, m)
    def callback(m):
        return None
    if change in ("current-hold", "cas"):
        def add_hold(_m):
            cur = json.loads(b.get("verified/ghost/current.json"))
            cur["deploy"]["hold"] = {"reason": {"en": "new incident", "zh": "新故障"}}
            b.put("verified/ghost/current.json", candidates.encode(cur))
        if change == "current-hold":
            add_hold(m)
        else:
            callback = add_hold  # race after preflight, immediately before CAS
    else:
        spec.path.write_text(spec.raw + "  hold:\n    reason: {en: new, zh: new}\n")
    before_versions = b.get("verified/ghost/versions/v6.61.0.json")
    with pytest.raises(ValueError):
        promote(setup, ref, report, check_template=callback)
    assert b.get("verified/ghost/versions/v6.61.0.json") == before_versions
    if change != "source-hold":
        assert json.loads(b.get("verified/ghost/current.json"))["deploy"]["hold"]["reason"]["en"] == "new incident"


def test_success_promotes_exact_manifest_without_overwriting_stable_screenshots(setup):
    b, cfg, spec, _, _ = setup
    stable_seed(b)
    ref, _, m = ready(setup)
    report = evidence(cfg, spec, ref, m)
    promote(setup, ref, report)
    assert json.loads(b.get("verified/ghost/versions/v6.61.0.json")) == m
    assert json.loads(b.get("verified/ghost/current.json")) == m["website"]
    assert b.get("screenshots/ghost/v6.61.0/home.png") == b"previous stable bytes"
    assert json.loads(b.get("verified/index.json"))["apps"][0]["verification_id"] == ref["verification_id"]
    # 崩溃恢复通道只认字节完全一致的 evidence：精确重放幂等（写回同样的字节），
    # 换一份证据——哪怕只改 session_id——都不能借道重写已发布内容。
    published = snapshot(b)
    assert promote(setup, ref, copy.deepcopy(report)) == m
    assert snapshot(b) == published
    replay = copy.deepcopy(report)
    replay["session_id"] = "a-different-production-run"
    with pytest.raises(ValueError, match="current/hold"):
        promote(setup, ref, replay)
    assert snapshot(b) == published


@pytest.mark.parametrize("cleanup,keep,crash", [(True, False, False), (False, False, False),
                                              (True, True, False), (True, False, True)])
def test_run_cleanup_is_part_of_green(setup, monkeypatch, cleanup, keep, crash):
    b, cfg, spec, _, _ = setup
    ref, _, m = ready(setup)
    monkeypatch.setenv("GITHUB_RUN_ID", "200")
    monkeypatch.setattr(prodcheck, "verify_public_template", lambda m: "template")
    monkeypatch.setattr(prodcheck, "_create_stack", lambda *a: None)
    monkeypatch.setattr(prodcheck, "_wait_create", lambda *a: (True, "ok"))
    monkeypatch.setattr(prodcheck, "deployed_template_matches", lambda *a: True)
    monkeypatch.setattr(prodcheck.golden, "read_canary", lambda *a: SimpleNamespace(instance_id="i-1", public_dns="example.com"))
    monkeypatch.setattr(prodcheck.golden, "_wait_for_ssm_ready", lambda *a, **k: None)
    monkeypatch.setattr(prodcheck, "public_access_denied", lambda *a: prodcheck.CheckResult("public_access_denied", True))
    @contextmanager
    def tunnel(*args):
        yield "http://127.0.0.1:8080", "test-session"
    monkeypatch.setattr(prodcheck, "ssm_tunnel", tunnel)
    monkeypatch.setattr(prodcheck, "run_checks", lambda ctx: [prodcheck.CheckResult(n, True) for n in ctx.plan.checks])
    def destroy(*a):
        if crash:
            raise RuntimeError("cleanup transport failed")
        return cleanup, ["cleanup attempted"]
    monkeypatch.setattr(prodcheck, "destroy_stack", destroy)
    result = prodcheck.run(b, cfg, spec, m, object(), keep=keep, candidate_ref=ref)
    assert result["all_passed"] is (cleanup and not keep and not crash)
    assert result["cleanup_confirmed"] is (cleanup and not keep and not crash)
    assert snapshot(b) == {}


def test_public_template_drift_never_creates_resources(setup, monkeypatch):
    b, cfg, spec, _, _ = setup
    ref, _, m = ready(setup)
    monkeypatch.setattr(prodcheck, "http_request", lambda *a, **k: (200, {}, b"wrong public template"))
    monkeypatch.setattr(prodcheck, "_create_stack", lambda *a: pytest.fail("must not create stack"))
    report = prodcheck.run(b, cfg, spec, m, object(), candidate_ref=ref)
    assert not report["all_passed"]
    assert not report["cleanup_confirmed"]
    assert snapshot(b) == {}


def test_dry_run_never_constructs_backend_or_aws(setup, monkeypatch, tmp_path):
    _, cfg, spec, _, m = setup
    script = Path(__file__).resolve().parents[1] / "scripts/verify/run_production_verify.py"
    loader = importlib.util.spec_from_file_location("production_cli", script)
    cli = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(cli)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(cli.Config, "load", lambda: cfg)
    monkeypatch.setattr(cli.appspec, "load", lambda *a: spec)
    monkeypatch.setattr(cli, "make_backend", lambda *a: pytest.fail("backend called in dry-run"))
    monkeypatch.setattr(cli.golden, "Aws", lambda *a: pytest.fail("AWS called in dry-run"))
    monkeypatch.setattr("sys.argv", [str(script), "--app", "ghost", "--dry-run", "--manifest-file", str(manifest_path)])
    assert cli.main() == 0


def test_workflows_share_lock_and_only_dispatch_exact_candidate():
    root = Path(__file__).resolve().parents[1]
    application = yaml.safe_load((root / ".github/workflows/application-verify.yml").read_text())
    production = yaml.safe_load((root / ".github/workflows/production-verify.yml").read_text())
    assert application["concurrency"]["group"] == "verify-${{ inputs.app_name }}"
    assert production["concurrency"]["group"] == "verify-${{ inputs.app }}"
    steps = application["jobs"]["verify"]["steps"]
    dispatch = next(s for s in steps if "Chain exact" in s.get("name", ""))
    assert "CANDIDATE_READY" in dispatch["if"]
    assert "steps.pipeline.outputs.candidate" in dispatch["env"]["CANDIDATE"]
    assert "current" not in dispatch["run"] and "glob" not in dispatch["run"]
    assert "PUBLISHED" in application["jobs"]["notify-site"]["if"]
    assert production["permissions"]["contents"] == "read"


def test_promotion_current_cas_conflict_writes_no_stable_object(setup, monkeypatch):
    b, cfg, spec, _, _ = setup
    stable_seed(b)
    ref, _, m = ready(setup)
    before = snapshot(b)
    monkeypatch.setattr(b, "put_if_match", lambda *a: False)
    with pytest.raises(ValueError, match="CAS conflict"):
        promote(setup, ref, evidence(cfg, spec, ref, m))
    assert snapshot(b) == before


def test_success_does_not_clear_manual_hold(setup):
    b, cfg, spec, _, _ = setup
    stable_seed(b)
    current = json.loads(b.get("verified/ghost/current.json"))
    hold = {"reason": {"en": "manual incident", "zh": "人工暂停"}}
    current["deploy"]["hold"] = hold
    b.put("verified/ghost/current.json", candidates.encode(current))
    ref, _, m = ready(setup)
    promote(setup, ref, evidence(cfg, spec, ref, m))
    assert json.loads(b.get("verified/ghost/current.json"))["deploy"]["hold"] == hold
    assert "hold" not in json.loads(b.get("verified/ghost/versions/v6.61.0.json"))["website"]["deploy"]


def test_manifest_missing_new_production_contract_cannot_bypass_gate(setup):
    b, cfg, spec, shots, m = setup
    app_dir = cfg.root / "apps"
    app_dir.mkdir()
    (app_dir / "ghost.yaml").write_text(spec.raw)
    m["website"]["deploy"].pop("production_contract")
    with pytest.raises(ValueError, match="contract changed/missing"):
        publish.publish(b, cfg, m, shots, "old manifest")
    assert b.writes == []


def test_pipeline_emits_exact_reference_not_latest(setup, monkeypatch, tmp_path):
    from corenova import pipeline
    ref, _, _ = ready(setup)
    output = tmp_path / "github-output"
    summary = {"status": "CANDIDATE_READY", "candidate": ref}
    monkeypatch.setattr(pipeline, "run_verification", lambda *a, **k: summary)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["pipeline", "--app", "ghost"])
    pipeline.main()
    emitted = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert emitted["status"] == "CANDIDATE_READY"
    assert json.loads(emitted["candidate"]) == ref


def test_complete_candidate_identity_required(setup):
    b, *_ = setup
    ref, _, _ = ready(setup)
    del ref["manifest_sha256"]
    with pytest.raises(ValueError, match="complete exact"):
        candidates.load(b, ref)


def test_old_application_evidence_cannot_be_refreshed_by_reupload(setup):
    b, cfg, _, shots, m = setup
    m["verified_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="expired"):
        publish.publish(b, cfg, m, shots, "old verification")
    assert b.writes == []
