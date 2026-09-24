"""app-schema.md §5 各条规则的负向用例：每种越界都必须被拒。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from corenova import appspec, profiles
from corenova.appspec import AppSpec

BASE_APP = "ghost"

GOOD_SPEC: dict = {
    "app": {
        "name": BASE_APP,
        "category": "cms",
        "app_type": "stateful_app",
        "icon": "/icons/ghost.svg",
        "i18n": {
            "en": {"display_name": "Ghost Blog", "description": "Publishing platform"},
            "zh": {"display_name": "Ghost 博客", "description": "发布平台"},
        },
    },
    "source": {"repo": "TryGhost/Ghost", "version_strategy": "release_tag"},
    "deploy": {
        "docker_image": "ghost",
        "image_tag_template": "ghost:{version_no_v}-alpine",
        "container_port": 2368,
        "compose_file": f"apps/{BASE_APP}/docker-compose.yml",
    },
    "health_check": {
        "endpoint": "/",
        "expected_status": 200,
        "version_assertion": {
            "kind": "exec_command",
            "command": "node -p \"require('/usr/src/ghost/package.json').version\"",
            "expected": "{version_no_v}",
        },
    },
    "tests": {
        "predefined_dir": f"apps/{BASE_APP}/tests",
        "scenarios": [
            {"slug": "home", "url": "/", "caption": {"en": "Home", "zh": "首页"}},
            {"slug": "admin", "url": "/ghost/", "caption": {"en": "Admin", "zh": "后台"}},
        ],
    },
    "deployment": {
        "size": "small",
        "regions": ["us-east-1"],
        "post_deploy": {
            "admin_path": "/ghost/",
            "admin_setup": {
                "en": "First visit opens the setup wizard; no preset credentials.",
                "zh": "首次访问进入初始化向导；没有预置账号密码。",
            },
            "notes": [{"en": "Back up the data volume.", "zh": "备份数据卷。"}],
        },
        "cost_estimate": {
            "monthly_usd": 18,
            "note": {
                "en": "t3.small + 30 GB gp3, us-east-1 on-demand.",
                "zh": "t3.small + 30GB gp3，us-east-1 按需计费。",
            },
        },
        "data_path": "/var/lib/ghost/content",
        "app_url_env_name": "url",
    },
    "website": {
        "featured": True,
        "screenshots_order": ["home", "admin"],
        "tags": ["blog"],
        "features": [{"en": "Automated testing", "zh": "自动化测试"}],
    },
}

GOOD_COMPOSE = """services:
  ghost:
    image: ${CORENOVA_APP_IMAGE}
    ports:
      - "${CORENOVA_HOST_PORT}:${CORENOVA_CONTAINER_PORT}"
    environment:
      url: ${CORENOVA_APP_URL}
    volumes:
      - ${CORENOVA_DATA_DIR}:/var/lib/ghost/content
"""


def make(tmp_path: Path, mutate=None, compose: str = GOOD_COMPOSE) -> AppSpec:
    spec_data = copy.deepcopy(GOOD_SPEC)
    if mutate:
        mutate(spec_data)
    app_dir = tmp_path / "apps" / BASE_APP
    (app_dir / "tests").mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_dump(spec_data, allow_unicode=True)
    path = tmp_path / "apps" / f"{BASE_APP}.yaml"
    path.write_text(raw, encoding="utf-8")
    (app_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")
    spec = AppSpec(name=BASE_APP, path=path, raw=raw, data=yaml.safe_load(raw))
    return spec


def errors(tmp_path: Path, mutate=None, compose: str = GOOD_COMPOSE) -> list[str]:
    spec = make(tmp_path, mutate, compose)
    return appspec.validate(spec, tmp_path, "us-east-1")


def test_good_spec_passes(tmp_path):
    assert errors(tmp_path) == []


def test_ladder_instances_are_all_ranked():
    """LADDER ⊆ INSTANCE_ORDER：地板比较依赖 rank 已知，缺项会让规则 10 静默失效。"""
    for sizes in profiles.LADDER.values():
        for instance, _disk in sizes.values():
            assert instance in profiles.INSTANCE_ORDER, instance


def test_instance_rank_unknown_is_max_not_min():
    # 未知规格不得小于任何地板（用户自带更大规格不阻断），但也不能负向放行已知小规格
    assert profiles.instance_rank("m5.24xlarge") == len(profiles.INSTANCE_ORDER)
    assert profiles.instance_rank("t3.nano") < profiles.instance_rank("t3.small")


def test_rule4_mobile_tag_rejected(tmp_path):
    errs = errors(tmp_path, lambda d: d["deploy"].__setitem__("image_tag_template", "ghost:5-alpine"))
    assert any("§3.1.2" in e for e in errs), errs


def test_rule4_latest_rejected(tmp_path):
    errs = errors(tmp_path, lambda d: d["deploy"].__setitem__("image_tag_template", "ghost:latest"))
    assert any("image_tag_template" in e or "§3.1" in e for e in errs), errs


def test_rule4_unknown_placeholder_rejected(tmp_path):
    errs = errors(tmp_path, lambda d: d["deploy"].__setitem__("image_tag_template", "ghost:{sha}"))
    assert any("非法占位符" in e for e in errs), errs


def test_rule4_base_mismatch_rejected(tmp_path):
    def m(d):
        d["deploy"]["image_tag_template"] = "ghosty:{version_no_v}"

    assert any("§3.1.4" in e for e in errors(tmp_path, m))


def test_rule3_hardcoded_image_and_port_rejected(tmp_path):
    bad = """services:
  ghost:
    image: ghost:5-alpine
    ports:
      - "2368:2368"
    environment:
      url: http://localhost:2368
"""
    errs = errors(tmp_path, compose=bad)
    assert any("image" in e for e in errs), errs
    assert any("端口" in e for e in errs), errs
    assert any("硬编码带端口 URL" in e for e in errs), errs


def test_rule3_unknown_compose_variable_rejected(tmp_path):
    bad = GOOD_COMPOSE + "    restart: ${SOMETHING_ELSE}\n"
    assert any("未声明变量" in e for e in errors(tmp_path, compose=bad))


def test_rule8_non_ascii_slug_rejected(tmp_path):
    def m(d):
        d["tests"]["scenarios"][0]["slug"] = "首页加载"

    assert any("slug" in e for e in errors(tmp_path, m))


def test_rule8_order_mismatch_rejected(tmp_path):
    def m(d):
        d["website"]["screenshots_order"] = ["admin"]

    assert any("screenshots_order" in e for e in errors(tmp_path, m))


def test_rule14_multi_region_rejected(tmp_path):
    def m(d):
        d["deployment"]["regions"] = ["us-east-1", "eu-west-1"]

    assert any("规则14" in e for e in errors(tmp_path, m))


def test_rule10_database_small_rejected(tmp_path):
    def m(d):
        d["app"]["app_type"] = "database"

    assert any("无 small 档" in e or "min_size" in e for e in errors(tmp_path, m))


def test_rule10_below_floor_needs_override(tmp_path):
    def m(d):
        d["deploy"]["disk_gb"] = 5  # 低于 stateful_app/small 的 30GB 地板

    assert any("override:" in e for e in errors(tmp_path, m))


def test_rule10_above_floor_is_free(tmp_path):
    def m(d):
        d["deployment"]["size"] = "xlarge"

    assert errors(tmp_path, m) == []


def test_rule12_assertion_missing_field_rejected(tmp_path):
    def m(d):
        d["health_check"]["version_assertion"] = {"kind": "env", "expected": "{version}"}

    assert any("缺字段 name" in e for e in errors(tmp_path, m))


def test_rule13_features_need_both_locales(tmp_path):
    def m(d):
        d["website"]["features"] = [{"en": "Only english"}]

    assert any("规则13" in e for e in errors(tmp_path, m))


def test_rule15_override_needs_reason(tmp_path):
    def m(d):
        d["release_type_override"] = "security_update"

    assert any("规则15" in e for e in errors(tmp_path, m))


@pytest.mark.parametrize("value", ["A=x\nB=y", "A=x\rB=y", "A=${UNKNOWN}", "A=${BROKEN"])
def test_rule16_rejects_multiline_and_unknown_placeholders(tmp_path, value):
    errs = errors(tmp_path, lambda d: d["deploy"].__setitem__("extra_environment", [value]))
    assert any("规则16" in e for e in errs)


def test_rule16_accepts_resolved_url_placeholder(tmp_path):
    assert errors(tmp_path, lambda d: d["deploy"].__setitem__(
        "extra_environment", ["PUBLIC_URL=${CORENOVA_APP_URL}/"]
    )) == []


def test_rule17_admin_path_without_setup_rejected(tmp_path):
    def m(d):
        d["deployment"]["post_deploy"] = {"admin_path": "/ghost/"}

    assert any("规则17" in e and "admin_setup" in e for e in errors(tmp_path, m))


def test_rule17_admin_path_must_start_with_slash(tmp_path):
    def m(d):
        d["deployment"]["post_deploy"]["admin_path"] = "ghost/"

    assert any("规则17" in e and "admin_path" in e for e in errors(tmp_path, m))


def test_rule17_credentials_rejected_in_copy(tmp_path):
    def m(d):
        d["deployment"]["post_deploy"]["admin_setup"]["en"] = "Login with password admin123"

    assert any("规则17" in e and "敏感词" in e for e in errors(tmp_path, m))


def test_rule17_malformed_shapes_report_not_crash(tmp_path):
    # 字符串 post_deploy：报违规，不抛异常
    errs = errors(tmp_path, lambda d: d["deployment"].__setitem__("post_deploy", "/ghost/"))
    assert any("规则17" in e and "映射" in e for e in errs), errs
    # notes 非列表
    def m(d):
        d["deployment"]["post_deploy"]["notes"] = "not a list"

    assert any("规则17" in e and "列表" in e for e in errors(tmp_path, m))
    # notes 项缺语言
    def m2(d):
        d["deployment"]["post_deploy"]["notes"] = [{"en": "only english"}]

    assert any("规则17" in e for e in errors(tmp_path, m2))


def test_rule17_absent_is_fine(tmp_path):
    def m(d):
        del d["deployment"]["post_deploy"]

    assert errors(tmp_path, m) == []


def test_rule18_nonpositive_usd_rejected(tmp_path):
    def m(d):
        d["deployment"]["cost_estimate"] = {"monthly_usd": 0}

    assert any("规则18" in e and "monthly_usd" in e for e in errors(tmp_path, m))


def test_rule18_note_requires_both_languages(tmp_path):
    def m(d):
        d["deployment"]["cost_estimate"] = {"monthly_usd": 18, "note": {"en": "t3.small + 30GB gp3"}}

    assert any("规则18" in e and "note" in e for e in errors(tmp_path, m))


def test_rule18_sensitive_words_rejected_in_note(tmp_path):
    def m(d):
        d["deployment"]["cost_estimate"]["note"] = {
            "en": "pay the bill with your token", "zh": "用 token 付账单"}

    assert any("规则18" in e and "敏感词" in e for e in errors(tmp_path, m))


def test_rule18_malformed_shape_report_not_crash(tmp_path):
    errs = errors(tmp_path, lambda d: d["deployment"].__setitem__("cost_estimate", "18/month"))
    assert any("规则18" in e and "映射" in e for e in errs), errs


def test_rule18_absent_is_fine(tmp_path):
    def m(d):
        del d["deployment"]["cost_estimate"]

    assert errors(tmp_path, m) == []


def test_rule19_must_start_with_slash(tmp_path):
    def m(d):
        d["deployment"]["data_path"] = "var/lib/ghost"

    assert any("规则19" in e and "/ 开头" in e for e in errors(tmp_path, m))


def test_rule19_shell_chars_rejected(tmp_path):
    def m(d):
        d["deployment"]["data_path"] = "/data; rm -rf /"

    assert any("规则19" in e and "非法字符" in e for e in errors(tmp_path, m))


def test_rule19_stateful_app_requires_data_path(tmp_path):
    def m(d):
        del d["deployment"]["data_path"]

    assert any("规则19" in e and "stateful_app" in e for e in errors(tmp_path, m))


def test_rule19_absent_is_fine_for_stateless_app(tmp_path):
    def m(d):
        d["app"]["app_type"] = "stateless_web"
        del d["deployment"]["data_path"]

    assert errors(tmp_path, m) == []


STATELESS_COMPOSE = "services:\n  web:\n    image: ${CORENOVA_APP_IMAGE}\n"


def stateless(d):
    d["app"]["app_type"] = "stateless_web"
    d["deployment"].pop("data_path", None)
    d["deployment"]["persistence"] = "none"


@pytest.mark.parametrize("name", ["cyberchef", "drawio", "it-tools"])
def test_registered_stateless_apps_validate(name):
    root = Path(__file__).resolve().parents[1]
    spec = appspec.load(name, root)
    assert spec.g("deployment.persistence") == "none"
    assert appspec.validate(spec, root, "us-east-1") == []


@pytest.mark.parametrize("mode", ["unknown", "", None, False, 0, [], {}])
def test_persistence_rejects_unknown_values(tmp_path, mode):
    errs = errors(tmp_path, lambda d: d["deployment"].__setitem__("persistence", mode))
    assert any("deployment.persistence" in e for e in errs), errs


@pytest.mark.parametrize("app_type", sorted(set(profiles.TYPES) - {"stateless_web"}))
def test_none_requires_stateless_web(tmp_path, app_type):
    def mutate(d):
        stateless(d)
        d["app"]["app_type"] = app_type

    assert any("仅允许 app_type=stateless_web" in e
               for e in errors(tmp_path, mutate, STATELESS_COMPOSE))


@pytest.mark.parametrize("path", ["/data", "", None, False])
def test_none_rejects_even_empty_explicit_data_path(tmp_path, path):
    def mutate(d):
        stateless(d)
        d["deployment"]["data_path"] = path

    assert any("persistence=none" in e and "data_path" in e
               for e in errors(tmp_path, mutate, STATELESS_COMPOSE))


@pytest.mark.parametrize("section", ["deploy", "deployment"])
def test_none_rejects_volume_declarations(tmp_path, section):
    def mutate(d):
        stateless(d)
        d[section]["volumes"] = []

    assert any("禁止 volumes" in e for e in errors(tmp_path, mutate, STATELESS_COMPOSE))


@pytest.mark.parametrize("compose", [
    GOOD_COMPOSE,
    STATELESS_COMPOSE + "volumes: {}\n",
    STATELESS_COMPOSE + "volumes:\n  data: {}\n",
    STATELESS_COMPOSE + "  sidecar:\n    volumes: ['/host:/data']\n",
    STATELESS_COMPOSE + "  sidecar:\n    volumes: ['/data']\n",
    STATELESS_COMPOSE + "  sidecar:\n    volumes: [{type: bind, source: /host, target: /data}]\n",
    STATELESS_COMPOSE + "  sidecar:\n    volumes_from: [web]\n",
    "x-base: &base\n  volumes: ['/host:/data']\nservices:\n  web:\n    <<: *base\n",
    "services: [invalid]\n",
    "services: [\n",
])
def test_none_rejects_compose_mounts_or_unverifiable_compose(tmp_path, compose):
    assert any("persistence=none" in e and "volumes" in e
               for e in errors(tmp_path, stateless, compose))


def test_none_rejects_data_dir_write(tmp_path):
    def mutate(d):
        stateless(d)
        d["deployment"]["production_contract"] = {"checks": ["data_dir_write"]}

    assert any("persistence=none 禁止 data_dir_write" in e
               for e in errors(tmp_path, mutate, STATELESS_COMPOSE))


def test_none_valid_without_mounts(tmp_path):
    assert errors(tmp_path, stateless, STATELESS_COMPOSE) == []


@pytest.mark.parametrize("path", [None, "", "relative", "/data;bad"])
def test_explicit_volume_requires_valid_path(tmp_path, path):
    def mutate(d):
        d["app"]["app_type"] = "stateless_web"
        d["deployment"]["persistence"] = "volume"
        d["deployment"]["data_path"] = path

    assert errors(tmp_path, mutate)


def test_explicit_volume_with_path_valid(tmp_path):
    assert errors(tmp_path, lambda d: d["deployment"].__setitem__("persistence", "volume")) == []


def test_rule20_app_url_env_name_must_be_safe_name(tmp_path):
    def m(d):
        d["deployment"]["app_url_env_name"] = "url; export BAD"

    assert any("规则20" in e for e in errors(tmp_path, m))


def test_rule20_app_url_env_name_is_optional(tmp_path):
    def m(d):
        del d["deployment"]["app_url_env_name"]

    assert errors(tmp_path, m) == []


def test_rule1_name_must_match_filename(tmp_path):
    def m(d):
        d["app"]["name"] = "ghost-blog"

    assert any("规则1" in e for e in errors(tmp_path, m))


def test_resources_derive(tmp_path):
    spec = make(tmp_path)
    assert spec.resources() == ("t3.small", 30)


@pytest.mark.parametrize("tpl,expect", [
    ("ghost:{version_no_v}-alpine", "ghost:6.61.0-alpine"),
    ("ghost:{version}", "ghost:v6.61.0"),
])
def test_tag_template_rendering(tmp_path, tpl, expect):
    spec = make(tmp_path, lambda d: d["deploy"].__setitem__("image_tag_template", tpl))
    assert appspec.render_image_ref(spec, "v6.61.0") == expect


def test_rule21_hold_valid_passes(tmp_path):
    def add_hold(d):
        d["deployment"]["hold"] = {
            "reason": {"en": "Deployment paused pending verification.", "zh": "暂停部署：待验证。"},
        }

    assert errors(tmp_path, add_hold) == []


def test_rule21_hold_reason_must_be_bilingual(tmp_path):
    errs = errors(
        tmp_path,
        lambda d: d["deployment"].__setitem__("hold", {"reason": {"en": "paused"}}),
    )
    assert any("规则21" in e and "双语" in e for e in errs), errs


def test_rule21_hold_rejects_non_mapping(tmp_path):
    errs = errors(tmp_path, lambda d: d["deployment"].__setitem__("hold", "paused"))
    assert any("规则21" in e and "映射" in e for e in errs), errs


def test_rule21_hold_rejects_extra_keys(tmp_path):
    errs = errors(
        tmp_path,
        lambda d: d["deployment"].__setitem__(
            "hold",
            {
                "reason": {"en": "paused", "zh": "暂停"},
                "until": "2026-10-01",
            },
        ),
    )
    assert any("规则21" in e and "额外键" in e for e in errs), errs


def test_rule21_hold_rejects_secret_words(tmp_path):
    errs = errors(
        tmp_path,
        lambda d: d["deployment"].__setitem__(
            "hold",
            {"reason": {"en": "reset the admin password", "zh": "重置管理员密码"}},
        ),
    )
    assert any("规则21" in e and "敏感词" in e for e in errs), errs
