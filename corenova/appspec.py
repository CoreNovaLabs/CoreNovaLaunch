"""Load `apps/{app}.yaml` and enforce app-schema.md §5 (21 rules) + app-profiles.md.

The validator is deliberately self-contained: Repo C's CI must be able to reject a bad
registration before any Docker / AWS / network work happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import profiles
from .util import strip_v

ALLOWED_COMPOSE_VARS = {
    "CORENOVA_APP_IMAGE",
    "CORENOVA_APP_IMAGE_REF",
    "CORENOVA_CONTAINER_PORT",
    "CORENOVA_HOST_PORT",
    "CORENOVA_APP_URL",
    "CORENOVA_DATA_DIR",
}

NAME_RE = re.compile(r"^[a-z0-9-]+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z_]+)\}")
VERSION_IN_TAG_RE = re.compile(r"\d+\.\d+")
MOBILE_TAGS = {"latest", "stable", "main", "edge", "nightly", "master"}


@dataclass
class AppSpec:
    name: str
    path: Path
    raw: str
    data: dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors (contract paths) ------------------------------
    def g(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def app_type(self) -> str:
        return self.g("app.app_type", "")

    @property
    def container_port(self) -> int:
        raw = self.g("deploy.container_port", 0)
        try:
            return int(raw)
        except (ValueError, TypeError):
            raise ValueError(f"deploy.container_port 必须为整数，实际值为 {raw!r}") from None

    @property
    def source_repo(self) -> str:
        return self.g("source.repo", "")

    @property
    def version_strategy(self) -> str:
        return self.g("source.version_strategy", "")

    @property
    def scenarios(self) -> list[dict[str, Any]]:
        return self.g("tests.scenarios") or []

    @property
    def startup_timeout(self) -> int:
        return int(
            self.g("health_check.startup_timeout_seconds")
            or profiles.startup_timeout(self.app_type)
        )

    def size(self) -> tuple[str, str, str]:
        return profiles.resolve_size(self.app_type, self.g("deployment.size"))

    def resources(self) -> tuple[str, int]:
        """Effective (instance_type, disk_gb): explicit per-dimension override wins,
        otherwise derived from the size ladder (app-profiles.md §5 方式 B)."""
        eff_size, min_size, _default = self.size()
        # 越界档位（如 database 选 small）由 §5 规则 10 报错，这里退回地板档推导以免崩在 None 上
        base = profiles.derive(self.app_type, eff_size) or profiles.derive(self.app_type, min_size)
        if base is None:
            raise ValueError(
                f"app-profiles: {self.app_type} 在 {eff_size!r} 和 {min_size!r} 档均无资源配置"
            )
        base_instance, base_disk = base
        min_res = profiles.derive(self.app_type, min_size)
        if min_res is None:
            raise ValueError(f"app-profiles: {self.app_type} 在 min_size={min_size!r} 档无资源配置")
        min_instance, min_disk = min_res
        instance = self.g("deploy.instance_type") or self.g("resources.instance_type") or base_instance
        disk = self.g("deploy.disk_gb") or self.g("resources.disk_gb") or base_disk
        # 低于 min_size 地板必须带 `# override: <reason>`
        if profiles.instance_rank(instance) < profiles.instance_rank(min_instance) or int(disk) < min_disk:
            if "override:" not in self.raw:
                raise ValueError(
                    f"app-schema §5 规则 10：{instance}/{disk}GB 低于 {self.app_type} 的 "
                    f"min_size 地板（{min_instance}/{min_disk}GB），必须写 `# override: <reason>`"
                )
        return str(instance), int(disk)


def load(name: str, root: Path) -> AppSpec:
    path = root / "apps" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"应用注册文件不存在：{path}")
    raw = path.read_text(encoding="utf-8")
    spec = AppSpec(name=name, path=path, raw=raw, data=yaml.safe_load(raw) or {})
    return spec


def all_apps(root: Path) -> list[str]:
    return sorted(p.stem for p in (root / "apps").glob("*.yaml"))


# --------------------------------------------------------------------------- validation


def validate(spec: AppSpec, root: Path, platform_region: str) -> list[str]:
    """Return a list of human-readable violations (empty == valid)."""
    e: list[str] = []
    g = spec.g

    if g("schema_version", 1) != 1 or g("deploy.services") is not None:
        return ["多容器 v2 尚未接入生产发布门禁；请使用实验性 stack 编译器，不得经 v1 发布"]

    # 1 文件名 == app.name
    if g("app.name") != spec.name:
        e.append(f"规则1: app.name={g('app.name')!r} 必须等于文件名 {spec.name!r}")
    if not NAME_RE.match(spec.name):
        e.append(f"规则1: app.name {spec.name!r} 不匹配 ^[a-z0-9-]+$")

    # 2 双语齐全
    for loc in ("en", "zh"):
        for f in ("display_name", "description"):
            if not g(f"app.i18n.{loc}.{f}"):
                e.append(f"规则2: app.i18n.{loc}.{f} 必填")
    if not str(g("app.icon", "")).startswith("/"):
        e.append("规则2: app.icon 必须以 / 开头（指向 Repo A 静态资源）")

    if g("app.category") not in profiles.CATEGORIES:
        e.append(f"规则: app.category {g('app.category')!r} 不在枚举内")
    if g("app.app_type") not in profiles.TYPES:
        e.append(f"规则9: app.app_type {g('app.app_type')!r} 不在枚举内")

    # source
    if not re.match(r"^[^/]+/[^/]+$", str(g("source.repo", ""))):
        e.append(f"规则: source.repo {g('source.repo')!r} 必须是 owner/name")
    if g("source.version_strategy") not in profiles.VERSION_STRATEGIES:
        e.append(f"规则: source.version_strategy {g('source.version_strategy')!r} 不在枚举内")

    # 3 compose 文件与变量纪律
    compose_rel = g("deploy.compose_file")
    if not compose_rel:
        e.append("规则3: deploy.compose_file 必填")
    else:
        compose = root / compose_rel
        if not compose.exists():
            e.append(f"规则3: compose 文件不存在 {compose_rel}")
        else:
            e.extend(_validate_compose(compose.read_text(encoding="utf-8")))

    # 4 image_tag_template（§3.1 六条）
    e.extend(_validate_tag_template(spec))

    # 5 deploy.instance_type 与 resources.instance_type 同时出现必须相等
    a, b = g("deploy.instance_type"), g("resources.instance_type")
    if a and b and a != b:
        e.append(f"规则5: deploy.instance_type={a} 与 resources.instance_type={b} 冲突")

    # 6 health_check
    hc_endpoint = str(g("health_check.endpoint", ""))
    if not hc_endpoint.startswith("/"):
        e.append(f"规则6: health_check.endpoint {hc_endpoint!r} 必须以 / 开头")
    status = g("health_check.expected_status")
    if not isinstance(status, int) or not ((200 <= status < 300) or (300 <= status < 400)):
        e.append(f"规则6: health_check.expected_status={status!r} 必须为 2xx/3xx")
    if g("health_check.method", "GET") == "POST" and not g("health_check.body"):
        e.append("规则6: method=POST 时 health_check.body 必须非空")

    # 7 + 14 regions
    regions = g("deployment.regions") or []
    if not regions:
        e.append("规则7: deployment.regions 非空必填")
    elif regions != [platform_region]:
        e.append(f"规则14: v1 单区域，deployment.regions={regions} 必须等于 [{platform_region!r}]")

    # 8 scenarios ↔ screenshots_order ↔ Manifest
    scenarios = spec.scenarios
    if not isinstance(scenarios, list):
        e.append(f"规则8: tests.scenarios 必须是列表，实为 {type(scenarios).__name__}")
        scenarios = []
    slugs: list[str] = []
    for i, s in enumerate(scenarios):
        if not isinstance(s, dict):
            e.append(f"规则8: tests.scenarios[{i}] 必须是映射，实为 {type(s).__name__}")
            continue
        slug = str(s.get("slug", ""))
        slugs.append(slug)
        if not SLUG_RE.match(slug):
            e.append(f"规则8: slug {slug!r} 必须匹配 ^[a-z0-9][a-z0-9-]*$（禁止中文/空格）")
        if not str(s.get("url", "")).startswith("/"):
            e.append(f"规则8: scenario {slug!r} 的 url 必须以 / 开头")
        caption = s.get("caption")
        if not isinstance(caption, dict):
            e.append(f"规则8: scenario {slug!r} 缺少 caption（必须是 en/zh 映射）")
            continue
        for loc in ("en", "zh"):
            if not caption.get(loc):
                e.append(f"规则8: scenario {slug!r} 缺少 caption.{loc}")
    if len(slugs) != len(set(slugs)):
        e.append("规则8: tests.scenarios[].slug 重复")
    order = g("website.screenshots_order") or []
    if slugs and sorted(order) != sorted(slugs):
        e.append(f"规则8: website.screenshots_order={order} 与 scenarios slug={slugs} 不一致")

    # 10 / 11 尺寸阶梯
    try:
        eff_size, min_size, _default = spec.size()
    except KeyError:
        eff_size = min_size = None
    if eff_size not in profiles.SIZE_ORDER:
        e.append(f"规则10: deployment.size={g('deployment.size')!r} 不在 small/medium/large/xlarge 内")
    elif eff_size not in profiles.LADDER.get(spec.app_type, {}):
        e.append(f"规则10: {spec.app_type} 无 {eff_size} 档（地板 {min_size}）")
    elif profiles.rank(eff_size) < profiles.rank(min_size):
        e.append(f"规则10: size={eff_size} 低于 {spec.app_type} 的 min_size={min_size}")
    try:
        spec.resources()
    except ValueError as exc:
        e.append(str(exc))
    except KeyError:
        pass  # 未知 app_type 已由规则9 报出，这里不重复崩溃

    # 12 version_assertion
    va = g("health_check.version_assertion")
    if va:
        if not isinstance(va, dict):
            e.append(f"规则12: version_assertion 必须是映射，实为 {type(va).__name__}")
        else:
            kind = va.get("kind")
            if kind not in profiles.ASSERTION_KINDS:
                e.append(f"规则12: version_assertion.kind={kind!r} 不在枚举 {profiles.ASSERTION_KINDS}")
            else:
                required = {
                    "env": ["name"], "label": ["name"], "header": ["name"],
                    "api_json_path": ["path", "json_pointer"], "exec_command": ["command"],
                }[kind]
                for r in required:
                    if not va.get(r):
                        e.append(f"规则12: version_assertion.kind={kind} 缺字段 {r}")
            if kind == "exec_command":
                command = va.get("command")
                valid_command = isinstance(command, str) and bool(command.strip())
                valid_argv = (isinstance(command, list) and bool(command)
                              and all(isinstance(x, str) and bool(x.strip()) for x in command))
                if not (valid_command or valid_argv):
                    e.append("规则12: exec_command.command 必须是非空字符串或非空字符串参数列表")
            expected = str(va.get("expected", ""))
            if not expected:
                e.append("规则12: version_assertion.expected 必填")
            for var in TEMPLATE_VAR_RE.findall(expected):
                if var not in ("version", "version_no_v"):
                    e.append(f"规则12: version_assertion.expected 含非法占位符 {{{var}}}")
            if va.get("match", "exact") not in ("exact", "prefix"):
                e.append("规则12: version_assertion.match 必须为 exact|prefix")

    # 13 features 双语
    features = g("website.features") or []
    if not isinstance(features, list):
        e.append(f"规则13: website.features 必须是列表，实为 {type(features).__name__}")
    else:
        for i, f in enumerate(features):
            if not isinstance(f, dict) or not (f.get("en") and f.get("zh")):
                e.append(f"规则13: website.features[{i}] 必须同时含 en 与 zh")

    # 15 release_type_override 需 reason
    rto = g("release_type_override")
    if rto:
        if rto not in profiles.RELEASE_TYPES:
            e.append(f"规则15: release_type_override={rto!r} 不在枚举内")
        if "reason:" not in spec.raw:
            e.append("规则15: release_type_override 非空必须带 `# reason:` 注释")

    # 16 extra_environment：KEY=VALUE 形式、禁止敏感词（密钥走 SSM/Secrets）
    extra_env = g("deploy.extra_environment") or []
    if not isinstance(extra_env, list):
        e.append(f"规则16: deploy.extra_environment 必须是列表，实为 {type(extra_env).__name__}")
    else:
        for i, item in enumerate(extra_env):
            s = str(item)
            if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\r\n]+", s):
                e.append(f"规则16: extra_environment[{i}] 必须为单行 KEY=VALUE 形式：{s!r}")
            elif re.search(r"secret|password|token|private_key", s, re.I):
                e.append(f"规则16: extra_environment[{i}] 含敏感词，敏感值走 SSM/Secrets，不进 app schema")
            if "${" in s.replace("${CORENOVA_APP_URL}", ""):
                e.append(f"规则16: extra_environment[{i}] 仅支持 ${{CORENOVA_APP_URL}} 占位符")

    # 17 post_deploy：部署后指引（网站详情页渲染）。只写"去哪/怎么获取"，凭据不进契约
    pd = g("deployment.post_deploy")
    if pd is not None:
        if not isinstance(pd, dict):
            e.append(f"规则17: deployment.post_deploy 必须是映射，实为 {type(pd).__name__}")
        else:
            admin_path = pd.get("admin_path")
            if admin_path is not None and (not isinstance(admin_path, str) or not admin_path.startswith("/")):
                e.append(f"规则17: post_deploy.admin_path 必须是以 / 开头的路径，实为 {admin_path!r}")
            setup = pd.get("admin_setup")
            if admin_path is not None and (not isinstance(setup, dict) or not setup.get("en") or not setup.get("zh")):
                e.append("规则17: post_deploy.admin_path 存在时，admin_setup.{en,zh} 必须均非空（有后台入口就必须说明怎么进去）")
            texts: list[str] = []
            if isinstance(setup, dict):
                texts += [str(setup.get("en") or ""), str(setup.get("zh") or "")]
            notes = pd.get("notes") or []
            if not isinstance(notes, list):
                e.append(f"规则17: post_deploy.notes 必须是列表，实为 {type(notes).__name__}")
                notes = []
            for i, note in enumerate(notes):
                if not isinstance(note, dict) or not note.get("en") or not note.get("zh"):
                    e.append(f"规则17: post_deploy.notes[{i}] 必须 en/zh 均非空")
                    continue
                texts += [str(note["en"]), str(note["zh"])]
            if any(re.search(r"secret|password|token|private_key", s, re.I) for s in texts):
                e.append("规则17: post_deploy 文案含敏感词——凭据不进契约，只允许写'去哪获取'")

    # 18 cost_estimate：月成本估算（网站部署区成本卡的事实源）。价格是注册时人工核对的
    # 事实——前端只展示，绝不按实例档/磁盘自行计算（§6 反模式：展示数据必须有注册来源）
    ce = g("deployment.cost_estimate")
    if ce is not None:
        if not isinstance(ce, dict):
            e.append(f"规则18: deployment.cost_estimate 必须是映射，实为 {type(ce).__name__}")
        else:
            usd = ce.get("monthly_usd")
            if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
                e.append(f"规则18: cost_estimate.monthly_usd 必须为正数，实为 {usd!r}")
            note = ce.get("note")
            if note is not None:
                if not isinstance(note, dict) or not note.get("en") or not note.get("zh"):
                    e.append("规则18: cost_estimate.note 若存在，en/zh 必须均非空（估算口径必须双语）")
                elif re.search(r"secret|password|token|private_key",
                               str(note.get("en")) + str(note.get("zh")), re.I):
                    e.append("规则18: cost_estimate.note 含敏感词——口径说明不放凭据类内容")

    # 19 data_path：容器内数据挂载目录（CFN DataContainerPath 的真相源）。
    # 必须以 / 开头；含空格或 shell 元字符则拒绝——路径不得注入命令。
    dp = g("deployment.data_path")
    if spec.app_type == "stateful_app" and dp is None:
        e.append("规则19: stateful_app 必须声明 deployment.data_path")
    elif dp is not None:
        if not isinstance(dp, str) or not dp.startswith("/"):
            e.append(f"规则19: deployment.data_path 必须以 / 开头的字符串，实为 {dp!r}")
        elif re.search(r"[\s;&|`$]", dp):
            e.append(f"规则19: deployment.data_path 含非法字符（空格或 shell 元字符）：{dp!r}")

    # 20 应用原生 URL 环境变量名；值由模板按 LaunchUrl / PublicDnsName 生成。
    app_url_env_name = g("deployment.app_url_env_name")
    if app_url_env_name is not None and (
        not isinstance(app_url_env_name, str)
        or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", app_url_env_name)
    ):
        e.append(
            "规则20: deployment.app_url_env_name 必须匹配 "
            f"^[A-Za-z_][A-Za-z0-9_]*$，实为 {app_url_env_name!r}"
        )

    # 21 deployment.hold：运维性部署暂停，独立于历史验证结果（“已验证”≠“当前可部署”）。
    # 解除条件是生产契约重新验证通过，不是版本更新；reason 必须双语，官网原样渲染。
    hold = g("deployment.hold")
    if hold is not None:
        if not isinstance(hold, dict):
            e.append(f"规则21: deployment.hold 必须是映射，实为 {type(hold).__name__}")
        else:
            reason = hold.get("reason")
            if not isinstance(reason, dict) or not reason.get("en") or not reason.get("zh"):
                e.append("规则21: hold.reason.{en,zh} 必须均非空（暂停原因必须双语说明）")
            elif re.search(r"secret|password|token|private_key",
                           str(reason.get("en")) + str(reason.get("zh")), re.I):
                e.append("规则21: hold.reason 含敏感词——暂停说明不涉及凭据类内容")
            unexpected = sorted(set(hold) - {"reason"})
            if unexpected:
                e.append(f"规则21: deployment.hold 不允许额外键 {unexpected}")

    # 22 deployment.production_contract：L1.5 生产核对声明（deployment-contract.md §2.6）。
    # checks 同时决定一次性栈要实证什么、深链必须携带哪些保护参数；声明了依赖 data_path /
    # app_url_env_name 的核对项却没声明字段本身，核对就没有对象可测。
    pc = g("deployment.production_contract")
    if pc is not None:
        if not isinstance(pc, dict):
            e.append(f"规则22: deployment.production_contract 必须是映射，实为 {type(pc).__name__}")
        else:
            unexpected = sorted(set(pc) - {"checks"})
            if unexpected:
                e.append(f"规则22: deployment.production_contract 不允许额外键 {unexpected}")
            checks = pc.get("checks")
            allowed = {"admin_auth", "data_dir_write", "url_injection", "host_metrics"}
            if (
                not isinstance(checks, list)
                or not checks
                or len(set(checks)) != len(checks)
                or not set(checks) <= allowed
            ):
                e.append(
                    f"规则22: production_contract.checks 必须是 {sorted(allowed)} 的去重非空子集，实为 {checks!r}"
                )
            else:
                if "data_dir_write" in checks and not g("deployment.data_path"):
                    e.append("规则22: checks 含 data_dir_write 必须同时声明 deployment.data_path（规则19）")
                if "url_injection" in checks:
                    # 注入通道二选一：应用原生变量（规则20）或 extra env 的 ${CORENOVA_APP_URL}
                    # 占位符（模板展开）；两条都是已验证形态，不得强制应用改声明方式。
                    has_placeholder = any(
                        "${CORENOVA_APP_URL}" in str(item)
                        for item in (g("deploy.extra_environment") or [])
                    )
                    if not g("deployment.app_url_env_name") and not has_placeholder:
                        e.append(
                            "规则22: checks 含 url_injection 必须声明 deployment.app_url_env_name"
                            "（规则20）或含 ${CORENOVA_APP_URL} 的 extra_environment 占位注入"
                        )

    # tests 目录
    tdir = g("tests.predefined_dir")
    if not tdir:
        e.append("规则: tests.predefined_dir 必填")
    elif not (root / tdir).is_dir():
        e.append(f"规则: tests.predefined_dir 目录不存在 {tdir}")

    if g("website.featured") is None:
        e.append("规则: website.featured 必填 boolean")
    if not g("website.tags"):
        e.append("规则: website.tags 非空必填")
    return e


def _validate_compose(text: str) -> list[str]:
    e: list[str] = []
    vars_used = set(re.findall(r"\$\{([A-Z_]+)(?::-[^\}]*)?\}", text))
    unknown = sorted(vars_used - ALLOWED_COMPOSE_VARS)
    if unknown:
        e.append(f"规则3: compose 引用未声明变量 {unknown}")
    for m in re.finditer(r"^\s*image:\s*(.+?)\s*$", text, re.M):
        if m.group(1) != "${CORENOVA_APP_IMAGE}":
            e.append(f"规则3: compose image 必须是 ${{CORENOVA_APP_IMAGE}}，实为 {m.group(1)!r}")
    for m in re.finditer(r"^\s*-\s*[\"']?(\$\{[A-Z_]+\}|\d+):(\$\{[A-Z_]+\}|\d+)[\"']?\s*$", text, re.M):
        if not (m.group(1) == "${CORENOVA_HOST_PORT}" and m.group(2) == "${CORENOVA_CONTAINER_PORT}"):
            e.append(f"规则3: compose 端口必须是 ${{CORENOVA_HOST_PORT}}:${{CORENOVA_CONTAINER_PORT}}，实为 {m.group(0).strip()}")
    for m in re.finditer(r"https?://[^\s\"']*/?\:(\d{2,5})", text):
        e.append(f"规则3: compose 含硬编码带端口 URL（应改用 ${{CORENOVA_APP_URL}}）：{m.group(0)}")
    return e


def _validate_tag_template(spec: AppSpec) -> list[str]:
    e: list[str] = []
    g = spec.g
    base = str(g("deploy.docker_image", ""))
    tpl = g("deploy.image_tag_template")
    if ":" in base:
        e.append(f"规则: deploy.docker_image {base!r} 必须是镜像基名（不含 : tag），tag 由 image_tag_template 渲染")
    if not tpl:
        e.append("规则4: deploy.image_tag_template 必填（禁止移动 tag 充当被验证镜像）")
        return e
    for var in TEMPLATE_VAR_RE.findall(tpl):
        if var not in ("version", "version_no_v"):
            e.append(f"规则4(§3.1.1): image_tag_template 含非法占位符 {{{var}}}，只允许 {{version}} / {{version_no_v}}")
    # 以样例版本渲染，验证 §3.1.2/3/4
    try:
        rendered = render_image_ref(spec, "5.75.0")
    except (KeyError, ValueError, IndexError) as exc:
        e.append(f"规则4(§3.1.1): image_tag_template={tpl!r} 无法渲染：{type(exc).__name__}: {exc}")
        return e
    tag = rendered.split(":")[-1]
    if not VERSION_IN_TAG_RE.search(tag):
        e.append(f"规则4(§3.1.2): 渲染结果 {rendered!r} 的 tag 不含点分版本号，属移动 tag")
    if tag in MOBILE_TAGS or re.fullmatch(r"\d+", tag):
        e.append(f"规则4(§3.1.3): 渲染结果 tag={tag!r} 属移动语义或仅 major 号")
    if tpl.split(":")[0] != base:
        e.append(
            f"规则4(§3.1.4): image_tag_template 的仓库部分 {tpl.split(':')[0]!r} "
            f"与 deploy.docker_image={base!r} 不一致"
        )
    if spec.version_strategy in ("release_tag", "semver_latest") and not TEMPLATE_VAR_RE.search(tpl):
        e.append(f"规则4(§3.1.5): version_strategy={spec.version_strategy} 时模板必须含版本占位符")
    if spec.version_strategy == "release_tag" and (base.endswith(":latest") or rendered.endswith(":latest")):
        e.append("规则4: version_strategy=release_tag 时镜像引用不得为 :latest")
    return e


def render_image_ref(spec: AppSpec, app_version: str) -> str:
    """Render `deploy.image_tag_template` for a concrete app_version -> full image ref."""
    tpl = spec.g("deploy.image_tag_template")
    if not tpl:
        raise ValueError("deploy.image_tag_template 未配置，无法渲染镜像引用")
    return tpl.format(
        version=app_version, version_no_v=strip_v(app_version)
    )
