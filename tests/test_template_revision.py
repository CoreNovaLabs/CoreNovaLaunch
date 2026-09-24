"""模板-证据绑定的静态一致性（deployment-contract.md §2.4）。

关键断言：`usertemplate.revision`（验证流水线写入证据的 template_revision）
与 `build_user_template.py --publish-s3` 发布到公开桶的是**同一份合并输出**。
两边任何一边换了实现/参数而另一边没跟，这里立刻红——否则证据绑定的 revision
与用户实际部署的模板会悄悄漂移。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from corenova import usertemplate
from corenova.util import file_sha

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_template_revision_is_a_40_hex_content_sha():
    rev = usertemplate.revision(REPO_ROOT)
    assert re.fullmatch(r"[0-9a-f]{40}", rev), f"revision 形状非法: {rev!r}"


def test_revision_matches_the_published_template_artifact(tmp_path):
    # 用真实发布构建器在临时目录落盘，始终验证本次源码，不依赖本地残留产物。
    artifact = tmp_path / "corenova-one-click.template.yaml"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/verify/build_user_template.py"),
         "--out", str(artifact)], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert file_sha(artifact) == usertemplate.revision(REPO_ROOT), (
        "发布构建器产物与 usertemplate.revision 不一致："
        "验证证据绑定的模板与用户实际部署的模板会漂移，检查合并实现是否分叉"
    )


def test_fixed_sources_keep_the_one_click_rewire_guarantees():
    # 与 build_user_template.py 的落盘自检同口径：合并输出不得再引用
    # 三栈架构的参数/导出（单栈自包含），AmiId 必须直连 EC2 ImageId。
    tpl = usertemplate.build(REPO_ROOT)
    text = usertemplate.merged_text(REPO_ROOT)
    for bad in ("Ref: SubnetId\n", "Ref: SecurityGroupId\n", "Ref: NetworkStackName\n",
                "Export:\n", "Fn::ImportValue:"):
        assert bad not in text, f"合并模板残留三栈引用: {bad.strip()}"
    assert tpl["Parameters"]["AmiId"]["Type"] == "AWS::EC2::Image::Id"
    assert tpl["Resources"]["Instance"]["Properties"]["ImageId"] == {"Ref": "AmiId"}
