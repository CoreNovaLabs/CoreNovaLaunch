"""Isolated release candidates. Only promote() may touch the stable namespace.

The two workflows share verify-<app>. current CAS is an additional fence against
hold sync / out-of-band writers. Artifacts stay immutable even after promotion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .manifest import CHECKS
from .util import sanitize_for_id, utcnow
from .versioning import semver_relation

MAX_AGE_SECONDS = 24 * 60 * 60


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def component(value):
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or ".." in value:
        raise ValueError("invalid candidate identity")
    return value


def prefix(app, run_id, attempt, verification_id):
    return "/".join(["candidates", *(component(x) for x in (app, run_id, attempt, verification_id))])


@contextmanager
def _local_lock(backend, app):
    # DirBackend's historical put_if_match is not atomic, and None is not a
    # create-only condition. Keep the fix confined to the candidate path.
    if getattr(backend, "name", "") != "dir":
        yield
        return
    import fcntl
    path = backend.root / "candidates" / component(app) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def cas(backend, app, key, value, etag):
    with _local_lock(backend, app):
        if backend.get_with_etag(key)[1] != etag:
            return False
        return backend.put_if_match(key, encode(value), etag)


def immutable_put(backend, app, key, data, content_type="application/json"):
    with _local_lock(backend, app):
        old = backend.get(key)
        if old is not None:
            if old != data:
                raise ValueError(f"immutable candidate already exists: {key}")
            return
        if getattr(backend, "name", "") == "r2":
            # Preserve artifact MIME types and create-only semantics together.
            backend.s3.put_object(Bucket=backend.bucket, Key=key, Body=data,
                                  ContentType=content_type, IfNoneMatch="*")
        else:
            backend.put(key, data, content_type)


def _order(run, attempt):
    if not str(run).isdigit() or not str(attempt).isdigit():
        raise ValueError("production candidates require numeric run/attempt")
    return int(run), int(attempt)


def _newer(manifest, previous, attempt):
    if not previous:
        return
    old_run = previous.get("verification_run_id", "0")
    old_attempt = previous.get("verification_run_attempt", "1")
    if _order(manifest["verification_run_id"], attempt) <= _order(old_run, old_attempt):
        raise ValueError("stale candidate run/attempt")
    if semver_relation(manifest["app_version"], previous.get("app_version", "")) == "older":
        raise ValueError("candidate version rollback refused")


def _assert_recent(timestamp):
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))).total_seconds()
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise ValueError("candidate/verification expired")


def template_identity(manifest):
    template = manifest["website"]["deploy"].get("template") or {}
    revision = manifest.get("config", {}).get("template_revision")
    if not re.fullmatch(r"[a-f0-9]{40}", str(revision)) or template.get("revision") != revision:
        raise ValueError("missing/mismatched template_revision")
    if not str(template.get("url", "")).startswith("https://"):
        raise ValueError("public HTTPS template URL required")
    return template["url"], revision


def _probe(backend, cfg, key):
    probe = getattr(backend, "probe", None)
    if probe is not None:
        return probe(key) is True
    if getattr(backend, "name", "") == "dir":
        return bool(backend.get(key))
    from .util import http_request
    public = (cfg.r2_public_base_url or "").rstrip("/")
    if not public.startswith("https://"):
        return False
    status, _, body = http_request(public + "/" + key)
    return status == 200 and bool(body)


def stage(backend, cfg, manifest, screenshots_dir, report_html, attempt="1"):
    from .publish import LOCAL_CHECKS, UPLOAD_CHECKS, PublishResult

    result = PublishResult(verification_id=manifest["verification_id"], checks=dict(manifest["checks"]))
    if not all(manifest["checks"].get(c) is True for c in LOCAL_CHECKS):
        result.notes.append("candidate local gate failed; stable untouched")
        return result
    template_identity(manifest)
    _assert_recent(manifest.get("verified_at"))
    app = component(manifest["app"])
    attempt = component(attempt)
    _order(manifest["verification_run_id"], attempt)
    base = prefix(app, manifest["verification_run_id"], attempt, manifest["verification_id"])
    raw, current_etag = backend.get_with_etag(f"verified/{app}/current.json")
    current = json.loads(raw) if raw else None
    _newer(manifest, current, attempt)
    tip_key = f"candidates/{app}/latest.json"
    raw, tip_etag = backend.get_with_etag(tip_key)
    tip = json.loads(raw) if raw else None
    # Reserve intent before uploads: a newer run supersedes old evidence even
    # if its own upload fails. A retry needs a new Actions run_attempt.
    intent = {"verification_run_id": str(manifest["verification_run_id"]),
              "verification_run_attempt": attempt, "app_version": manifest["app_version"],
              "key": base + "/candidate.json"}
    _newer(manifest, tip, attempt)
    if not cas(backend, app, tip_key, intent, tip_etag):
        raise ValueError("candidate intent CAS conflict")

    m = copy.deepcopy({k: v for k, v in manifest.items() if not k.startswith("_")})
    m["verification_run_attempt"] = attempt
    m["website"]["verification_run_attempt"] = attempt
    m["website"] = {k: v for k, v in m["website"].items() if not k.startswith("_")}
    public = (cfg.r2_public_base_url or "").rstrip("/")
    uploads = {}
    shots = m["artifacts"]["screenshots"]
    if not shots:
        raise ValueError("candidate screenshots missing")
    for shot in shots:
        filename = component(shot["file"])
        data = (Path(screenshots_dir) / filename).read_bytes()
        key = base + "/screenshots/" + filename
        uploads[key] = (data, "image/png")
        shot["url"] = public + "/" + key
    m["website"]["screenshots"] = copy.deepcopy(shots)
    key = base + "/report.html"
    # Reports generated before staging can contain stable screenshot links.
    for old, new in zip(manifest["artifacts"]["screenshots"], shots, strict=True):
        report_html = report_html.replace(old["url"], new["url"])
    uploads[key] = (report_html.encode(), "text/html; charset=utf-8")
    m["artifacts"]["report_url"] = m["website"]["report_url"] = public + "/" + key
    m["checks"].update(dict.fromkeys(UPLOAD_CHECKS, False))
    m["website"]["status"] = "pending"
    # Operational hold is captured separately, never frozen in version evidence.
    m["website"]["deploy"].pop("hold", None)
    placeholder_key = base + "/manifest.pending.json"
    immutable_put(backend, app, placeholder_key, encode(m))
    if backend.get(placeholder_key) != encode(m):
        raise ValueError("candidate placeholder readback failed")
    objects = {}
    for key, (data, content_type) in uploads.items():
        immutable_put(backend, app, key, data, content_type)
        objects[key] = hashlib.sha256(data).hexdigest()
        if hashlib.sha256(backend.get(key) or b"").hexdigest() != objects[key]:
            raise ValueError("candidate artifact readback failed")
    probes = [_probe(backend, cfg, key) for key in [*objects, placeholder_key]]
    if not all(probes):
        raise ValueError("candidate public probe failed")
    m["checks"].update(dict.fromkeys(UPLOAD_CHECKS, True))
    m["website"]["status"] = "verified"
    manifest_key = base + "/manifest.json"
    immutable_put(backend, app, manifest_key, encode(m))
    if backend.get(manifest_key) != encode(m):
        raise ValueError("candidate manifest readback failed")
    if not _probe(backend, cfg, manifest_key):
        raise ValueError("candidate final manifest public probe failed")
    envelope = {**intent, "app": app, "verification_id": m["verification_id"],
                "manifest_key": manifest_key, "manifest_sha256": digest(m),
                "current_etag": current_etag, "created_at": utcnow(),
                "hold": copy.deepcopy((current or {}).get("deploy", {}).get("hold")),
                "declared_hold": copy.deepcopy(manifest["website"]["deploy"].get("hold")),
                "objects": objects}
    immutable_put(backend, app, intent["key"], encode(envelope))
    result.candidate_ready = True
    result.candidate = {k: envelope[k] for k in (
        "app", "app_version", "verification_id", "verification_run_id",
        "verification_run_attempt", "key", "manifest_sha256")}
    result.checks = m["checks"]
    result.notes.append("candidate ready; no stable objects written")
    return result


def load(backend, ref):
    fields = {"app", "app_version", "verification_id", "verification_run_id",
              "verification_run_attempt", "key", "manifest_sha256"}
    if not isinstance(ref, dict) or set(ref) != fields or not all(ref.values()):
        raise ValueError("complete exact candidate reference required")
    key = prefix(ref["app"], ref["verification_run_id"], ref["verification_run_attempt"],
                 ref["verification_id"]) + "/candidate.json"
    if ref.get("key") != key:
        raise ValueError("candidate key/identity mismatch")
    raw = backend.get(key)
    if not raw:
        raise ValueError("candidate not ready")
    envelope = json.loads(raw)
    for field, value in ref.items():
        if envelope.get(field) != value:
            raise ValueError(f"candidate evidence mismatch: {field}")
    m = json.loads(backend.get(envelope["manifest_key"]) or b"{}")
    if digest(m) != envelope["manifest_sha256"]:
        raise ValueError("candidate manifest digest mismatch")
    for field in ("app", "app_version", "verification_id", "verification_run_id", "verification_run_attempt"):
        if m.get(field) != envelope.get(field):
            raise ValueError(f"candidate manifest identity mismatch: {field}")
    for field in ("app", "app_version", "verification_id", "verification_run_id", "verification_run_attempt"):
        if m["website"].get(field) != m.get(field):
            raise ValueError("manifest/website identity mismatch")
    template_identity(m)
    if not all(m.get("checks", {}).get(c) is True for c in CHECKS):
        raise ValueError("candidate checks incomplete")
    return envelope, m


def _promotion_key(envelope):
    return envelope["key"].removesuffix("/candidate.json") + "/promotion.json"


def assert_fresh(backend, envelope, manifest, *, recovery=None):
    _assert_recent(envelope["created_at"])
    _assert_recent(manifest.get("verified_at"))
    app = manifest["app"]
    tip = json.loads(backend.get(f"candidates/{app}/latest.json") or b"{}")
    if tip.get("key") != envelope["key"]:
        raise ValueError("candidate superseded by newer run")
    raw, etag = backend.get_with_etag(f"verified/{app}/current.json")
    if etag != envelope["current_etag"]:
        if (recovery is None or raw != encode(recovery["current"])
                or backend.get(_promotion_key(envelope)) != encode(recovery)):
            raise ValueError("current/hold changed since candidate creation")
    else:
        _newer(manifest, json.loads(raw) if raw else None, manifest["verification_run_attempt"])
    return etag


def promote(backend, cfg, ref, evidence, *, production_run_id, spec, check_template,
            production_run_attempt="1"):
    from .publish import _put_json, _update_index, _update_versions_index
    from .util import file_sha

    envelope, m = load(backend, ref)
    current = copy.deepcopy(m["website"])
    # Never automatically clear a manual hold, including on a completion retry.
    hold = envelope["hold"] or envelope["declared_hold"]
    if hold:
        current["deploy"]["hold"] = hold
    recovery = {"candidate_sha256": digest(envelope), "evidence_sha256": digest(evidence),
                "app_config_revision": m.get("config", {}).get("app_config_revision"),
                "current": current}
    current_etag = assert_fresh(backend, envelope, m, recovery=recovery)
    expected = {**ref, "production_run_id": str(production_run_id),
                "production_run_attempt": str(production_run_attempt),
                "image_reference": m["container"]["image"] + "@" + m["container"]["digest"],
                "template_revision": template_identity(m)[1]}
    if any(evidence.get(k) != v for k, v in expected.items()):
        raise ValueError("production evidence identity mismatch")
    if (evidence.get("all_passed") is not True or evidence.get("cleanup_confirmed") is not True
            or not evidence.get("session_id")):
        raise ValueError("production checks/cleanup/SSM session not confirmed")
    created = datetime.fromisoformat(envelope["created_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(str(evidence.get("started_at", "")).replace("Z", "+00:00"))
    finished = datetime.fromisoformat(str(evidence.get("finished_at", "")).replace("Z", "+00:00"))
    if not created <= started <= finished <= datetime.now(timezone.utc):
        raise ValueError("stale production evidence timestamps")
    from .prodcheck import plan
    # spec is freshly read by caller; do not let a new contract/hold reuse old evidence.
    if file_sha(spec.path) != m.get("config", {}).get("app_config_revision"):
        raise ValueError("app configuration/hold changed")
    required = {"stack_created", "template_match", "public_access_denied", "health_external"}
    declared = spec.g("deployment.production_contract.checks") or []
    required.update(c for c in declared if c != "admin_auth")
    checks = evidence.get("checks") or []
    if (len(checks) != len(required) or {c.get("name") for c in checks} != required
            or not all(c.get("passed") is True for c in checks)):
        raise ValueError("incomplete production evidence")
    expected_params = plan(cfg, spec, m, run_id=str(production_run_id)).parameters
    if evidence.get("parameters") != expected_params:
        raise ValueError("production parameter mismatch")
    for key, sha in envelope["objects"].items():
        if hashlib.sha256(backend.get(key) or b"").hexdigest() != sha:
            raise ValueError("candidate artifact changed")
    check_template(m)  # public content SHA rechecked immediately before commit
    if file_sha(spec.path) != recovery["app_config_revision"]:
        raise ValueError("app configuration/hold changed")
    if assert_fresh(backend, envelope, m, recovery=recovery) != current_etag:
        raise ValueError("current/hold changed during promotion")
    # Bind retries before CAS; an exact committed projection is also required.
    immutable_put(backend, m["app"], _promotion_key(envelope), encode(recovery))
    current_key = f"verified/{m['app']}/current.json"
    if not cas(backend, m["app"], current_key, current, current_etag):
        raise ValueError("current CAS conflict; stable release unchanged")

    def ensure_committed():
        if backend.get(current_key) != encode(current):
            raise ValueError("committed current/hold changed")
        assert_fresh(backend, envelope, m, recovery=recovery)
        if file_sha(spec.path) != recovery["app_config_revision"]:
            raise ValueError("app configuration/hold changed")

    ensure_committed()
    _put_json(backend, f"verified/{m['app']}/versions/{sanitize_for_id(m['app_version'])}.json", m)
    ensure_committed()
    _update_index(backend, m["app"], m)
    ensure_committed()
    _update_versions_index(backend, m["app"], m)
    return m
