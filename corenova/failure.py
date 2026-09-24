"""FAILED classification + the GitHub-issue failure registry (workflow-state-machine.md §4/§7).

Only TRANSIENT may auto-retry. Everything else routes to FIX_PR or MANUAL_REQUIRED —
a blanket retry policy would hide real regressions behind flaky-green runs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote as _urlencode

from .gh import github_headers, github_token
from .util import HttpError, http_json, http_request, is_transitional_error, log

LABEL = "verify-failed"
MAX_ATTEMPTS = 3

ALL_CLASSIFICATIONS = ("TRANSIENT", "APPLICATION", "TEST", "INFRASTRUCTURE", "MANUAL_REQUIRED")


@dataclass
class FailureRecord:
    app: str
    app_version: str
    verification_id: str
    classification: str
    failed_stage: str
    failed_check: str
    attempts: int = 1
    run_url: str = ""
    platform_verification_id: str = ""
    detail: str = ""

    def title(self) -> str:
        return f"verify({self.app}): {self.app_version} FAILED ({self.classification})"

    def labels(self) -> list[str]:
        out = [LABEL, f"classification:{self.classification}", f"app:{self.app}"]
        if self.classification in ("INFRASTRUCTURE", "MANUAL_REQUIRED") or self.attempts >= MAX_ATTEMPTS:
            out.append("needs-human")
        return out

    def body(self) -> str:
        meta = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        return (
            f"Application Verification 失败。\n\n"
            f"```corenova-failure\n{json.dumps(_block(self), ensure_ascii=False, indent=2)}\n```\n\n"
            f"### 详情\n\n```\n{self.detail[:4000]}\n```\n\n"
            f"运行：{self.run_url or '（本地运行，无 Actions 链接）'}\n\n"
            f"<!-- full record: {meta} -->\n"
        )


def _block(r: FailureRecord) -> dict[str, Any]:
    return {
        "app": r.app,
        "app_version": r.app_version,
        "verification_id": r.verification_id,
        "classification": r.classification,
        "failed_stage": r.failed_stage,
        "failed_check": r.failed_check,
        "attempts": r.attempts,
        "run_url": r.run_url,
        "platform_verification_id": r.platform_verification_id,
    }


def classify(stage: str, check: str = "", err: BaseException | None = None) -> str:
    """Map a failure onto the state machine's five categories."""
    if err is not None and is_transitional_error(err):
        return "TRANSIENT"
    if stage in ("DEPLOYING", "DEPLOYED") or check in ("cfn", "ami", "cfn_init", "nginx_platform"):
        return "INFRASTRUCTURE"
    # Deterministic checks route by their own nature BEFORE any stage default:
    # an invalid platform contract sends the platform side back to golden-verify
    # (not a FIX_PR against apps/), and a schema violation is an app-registry fix —
    # neither is cured by retrying.
    if check == "required_platform_contract_valid":
        return "INFRASTRUCTURE"
    if check == "app_schema":
        return "APPLICATION"
    if check == "tests_passed":
        return "TEST"
    if check in ("compose_started", "container_healthy", "health_check_passed"):
        return "APPLICATION"
    if check == "screenshots_generated":
        return "TEST"
    if stage == "RESOLVED":
        # Transitional errors already returned TRANSIENT above; what reaches here is
        # a deterministic resolution failure (missing registry file, unknown tag,
        # malformed YAML) — surface it for a human instead of auto-retrying.
        return "MANUAL_REQUIRED" if err is not None else "APPLICATION"
    if stage == "PUBLISHING":
        return "TRANSIENT"
    return "MANUAL_REQUIRED"


# --------------------------------------------------------------------------- registry


def repo_name() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "")


def _headers() -> dict[str, str]:
    return github_headers(github_token())


def find_issue(record: FailureRecord) -> dict[str, Any] | None:
    """Idempotency key = verification_id (state-machine §7).

    幂等键在正文 fenced 块里，不在 title 里——搜索必须限定 in:body，
    用 in:title 永远检不到目标，幂等更新也就从未生效。
    """
    repo = repo_name()
    if not repo:
        return None
    q = f"repo:{repo} is:issue is:open in:body {record.verification_id}"
    try:
        hits = http_json(f"https://api.github.com/search/issues?q={_urlencode(q, safe='')}&per_page=5", headers=_headers())
    except HttpError as exc:
        log(f"查询失败台账出错（忽略）：{exc}")
        return None
    for it in hits.get("items") or []:
        if "state" in it and it["state"] != "open":
            continue
        if meta_from_body(it.get("body") or "").get("verification_id") == record.verification_id:
            return it
    return None


def record_failure(record: FailureRecord) -> None:
    """Create or update the ledger issue. Never raises: reporting must not mask the result."""
    repo = repo_name()
    if not repo:
        log("非 GitHub 环境（无 GITHUB_REPOSITORY）→ 失败台账仅打印")
        print(json.dumps(_block(record), ensure_ascii=False))
        return
    existing = find_issue(record)
    try:
        if existing:
            number = existing["number"]
            record.attempts = _attempts_of(existing.get("body") or "") + 1
            http_request(
                f"https://api.github.com/repos/{repo}/issues/{number}",
                method="PATCH",
                headers=_headers(),
                data={"title": record.title(), "body": record.body(), "labels": record.labels(), "state": "open"},
            )
            log(f"失败台账已更新 #{number}（attempt {record.attempts}）")
        else:
            resp = http_json(
                f"https://api.github.com/repos/{repo}/issues",
                method="POST",
                headers=_headers(),
                data={"title": record.title(), "body": record.body(), "labels": record.labels()},
            )
            log(f"失败台账已创建 #{resp.get('number')}")
    except Exception as exc:  # noqa: BLE001
        log(f"写失败台账出错（忽略）：{type(exc).__name__}: {exc}")


def meta_from_body(body: str) -> dict[str, Any]:
    """从台账正文解析 ```corenova-failure JSON 块；解析失败返回 {}。"""
    m = re.search(r"```corenova-failure\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def resolve_failures(app: str, app_version: str, verification_id: str) -> None:
    """发布成功后关闭对应失败台账（workflow-state-machine.md §7：成功则关闭 issue）。

    跨 run 的重试会携带新的 verification_id（§2 的"沿用同一号"只在单次验证内成立），
    纯 vid 匹配关不掉历史条目；故按 app 标签搜索，凡正文里 app_version 或
    verification_id 与本次成功一致的条目都视为已解决。版本不同（如下一版的
    MANUAL_REQUIRED 跟踪票）不关。永不抛出：台账不能掩盖发布结果。
    """
    repo = repo_name()
    if not repo:
        return
    q = f"repo:{repo} is:issue is:open label:{LABEL} label:app:{app}"
    try:
        hits = http_json(
            f"https://api.github.com/search/issues?q={_urlencode(q, safe='')}&per_page=50",
            headers=_headers(),
        )
    except HttpError as exc:
        log(f"查询可解决的失败台账出错（忽略）：{exc}")
        return
    for it in hits.get("items") or []:
        meta = meta_from_body(it.get("body") or "")
        vid_match = verification_id and meta.get("verification_id") == verification_id
        ver_match = (
            app_version
            and meta.get("app_version") not in ("", "unknown")
            and meta.get("app_version") == app_version
        )
        if not (vid_match or ver_match):
            continue
        try:
            http_request(
                f"https://api.github.com/repos/{repo}/issues/{it['number']}/comments",
                method="POST", headers=_headers(),
                data={"body": f"已由成功的验证 `{verification_id}` 解决，自动关闭。"},
            )
            http_request(
                f"https://api.github.com/repos/{repo}/issues/{it['number']}",
                method="PATCH", headers=_headers(), data={"state": "closed"},
            )
            log(f"失败台账已关闭 #{it['number']}（{app}@{app_version} 发布成功）")
        except Exception as exc:  # noqa: BLE001
            log(f"关闭台账出错（忽略）：{type(exc).__name__}: {exc}")


def _attempts_of(body: str) -> int:
    m = re.search(r'"attempts":\s*(\d+)', body)
    return int(m.group(1)) if m else 1


def dispatch_target(classification: str) -> str:
    """INFRASTRUCTURE 的复验只能落在 golden-verify，其余一律落 application-verify。"""
    return "golden-verify.yml" if classification == "INFRASTRUCTURE" else "application-verify.yml"


def _label_of(item: dict[str, Any], prefix: str) -> str:
    for lab in item.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else str(lab)
        if name and name.startswith(prefix):
            return name[len(prefix):]
    return ""


def row_from_issue(item: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    """issue + fenced metadata → 台账行。§7 规则 1/5：仅 TRANSIENT 且 attempts<3 可自动重试。"""
    meta = meta or {}
    classification = str(meta.get("classification") or _label_of(item, "classification:"))
    attempts = int(meta.get("attempts") or _attempts_of(item.get("body") or ""))
    return {
        "issue_number": item.get("number"),
        "issue_url": item.get("html_url") or "",
        "app": str(meta.get("app") or _label_of(item, "app:")),
        "app_version": str(meta.get("app_version") or ""),
        "verification_id": str(meta.get("verification_id") or ""),
        "classification": classification,
        "failed_stage": str(meta.get("failed_stage") or ""),
        "failed_check": str(meta.get("failed_check") or ""),
        "attempts": attempts,
        "run_url": str(meta.get("run_url") or ""),
        "platform_verification_id": str(meta.get("platform_verification_id") or ""),
        "retryable": (
            classification == "TRANSIENT"
            and attempts < MAX_ATTEMPTS
            and str(item.get("state") or "").lower() == "open"
        ),
        "dispatch_target": dispatch_target(classification),
        "updated_at": item.get("updated_at") or "",
    }


def open_failures_by_classification(
    cls: str, limit: int = 50, errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按 label 拉某一分类的 open verify-failed issue，解析成台账行列表。"""
    repo = repo_name()
    if not repo:
        return []
    q = f"repo:{repo} is:issue is:open label:{LABEL} label:classification:{cls}"
    try:
        hits = http_json(
            f"https://api.github.com/search/issues?q={_urlencode(q, safe='')}&per_page={limit}",
            headers=_headers(),
        )
    except HttpError as exc:
        if errors is not None:
            errors.append(f"{cls}: {type(exc).__name__}: {exc}")
        log(f"读取 classification={cls} 台账失败：{type(exc).__name__}: {exc}")
        return []
    return [row_from_issue(it, meta_from_body(it.get("body") or "")) for it in (hits.get("items") or [])]


def open_transient_failures(limit: int = 20) -> list[dict[str, Any]]:
    """Entries reverify-failed.yml may legitimately retry (TRANSIENT, attempts < 3, open)."""
    repo = repo_name()
    if not repo:
        return []
    q = f"repo:{repo} is:issue is:open label:{LABEL} label:classification:TRANSIENT"
    try:
        hits = http_json(
            f"https://api.github.com/search/issues?q={_urlencode(q, safe='')}&per_page={limit}", headers=_headers()
        )
    except HttpError as exc:
        log(f"读取失败台账出错：{exc}")
        return []
    out = []
    for it in hits.get("items") or []:
        m = re.search(r"```corenova-failure\n(.*?)\n```", it.get("body") or "", re.S)
        if not m:
            continue
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if int(meta.get("attempts", 1)) < MAX_ATTEMPTS:
            meta["issue_number"] = it["number"]
            out.append(meta)
    return out


def pending_review_failures(limit: int = 30) -> list[dict[str, Any]]:
    """Non-transient failures, surfaced for humans only — never auto-retried."""
    repo = repo_name()
    if not repo:
        return []
    out: list[dict[str, Any]] = []
    for cls in ("APPLICATION", "TEST", "INFRASTRUCTURE", "MANUAL_REQUIRED"):
        q = f"repo:{repo} is:issue is:open label:{LABEL} label:classification:{cls}"
        try:
            hits = http_json(f"https://api.github.com/search/issues?q={_urlencode(q, safe='')}&per_page={limit}", headers=_headers())
        except HttpError:
            continue
        for it in hits.get("items") or []:
            out.append({"classification": cls, "number": it["number"], "title": it["title"]})
    return out


@dataclass
class Summary:
    ok: bool
    classification: str = ""
    notes: list[str] = field(default_factory=list)
