"""one-click 单栈模板的合并约束（scripts/verify/build_user_template.py）。

回归：2026-08-31 线上事故 -- 合并模板保留了 network.yaml 的跨栈 Export
（`${StackPrefix}-network-*`，StackPrefix 默认 corenova），与用户账号里既有的
corenova-network 栈导出同名，CREATE 即回滚
（"Export with name corenova-network-VpcId is already exported by stack corenova-network"）。
单栈模板自包含：任何 Export / Fn::ImportValue 都不允许出现。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify" / "build_user_template.py"


def build(tmp_path: pathlib.Path) -> dict:
    out = tmp_path / "one-click.template.yaml"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def test_merged_template_has_no_cross_stack_exports(tmp_path):
    tpl = build(tmp_path)
    outputs = tpl.get("Outputs") or {}
    # 一键部署用户只需要入口地址和定位实例的 ID（网络/ IAM 等落地细节已剔除）
    assert set(outputs) == {"InstanceId", "PublicIp", "PublicDnsName", "PrivateIp", "ResolvedLaunchUrl",
                            "SSMPortForwardCommand", "EnableHttpsCommand"}
    for key, o in outputs.items():
        assert "Export" not in o, f"Output {key!r} 仍带 Export 块：{o.get('Export')}"


def test_merged_template_is_self_contained(tmp_path):
    """脚本自身的 rewire 自检已覆盖，这里从产物侧再验一次：无 ImportValue、无旧网络栈参数。"""
    out = tmp_path / "one-click.template.yaml"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "Fn::ImportValue" not in text
    assert "Ref: SubnetId" not in text
    assert "Ref: SecurityGroupId" not in text
    assert "NetworkStackName" not in text


def test_user_interface_exposes_complete_verified_runtime_contract(tmp_path):
    tpl = build(tmp_path)
    groups = tpl["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    grouped = {parameter for group in groups for parameter in group["Parameters"]}
    assert {
        "AmiId",
        "InstanceType",
        "DataVolumeSize",
        "DataContainerPath",
        "HealthCheckPath",
        "AppUrlEnvironmentName",
        "ExtraEnvironment",
    } <= grouped


def test_one_click_template_requires_the_verified_ami(tmp_path):
    tpl = build(tmp_path)
    assert tpl["Parameters"]["AmiId"]["Type"] == "AWS::EC2::Image::Id"
    assert "Default" not in tpl["Parameters"]["AmiId"]
    assert tpl["Resources"]["Instance"]["Properties"]["ImageId"] == {"Ref": "AmiId"}
    assert "UseSsmPublicAmi" not in tpl["Conditions"]


def test_optional_capabilities_are_closed_and_wired(tmp_path):
    tpl = build(tmp_path)
    params = tpl["Parameters"]
    assert params["AllowedWebCidr"]["Default"] == "127.0.0.1/32"
    assert params["DockerSocketAccess"]["Default"] == "false"
    assert params["ExtraPortIngressCidr"]["Default"] == "127.0.0.1/32"
    for protocol in ("Tcp", "Udp"):
        port = f"Extra{protocol}Port"
        assert params[port]["Default"] == 0
        ingress = tpl["Resources"][f"Extra{protocol}Ingress"]
        assert ingress["Condition"] == f"Extra{protocol}Enabled"
        assert ingress["Properties"] == {
            "GroupId": {"Ref": "BaseSG"}, "IpProtocol": protocol.lower(),
            "FromPort": {"Ref": port}, "ToPort": {"Ref": port},
            "CidrIp": {"Ref": "ExtraPortIngressCidr"},
            "Description": f"Explicit {protocol.upper()} application port",
        }
    init = tpl["Resources"]["Instance"]["Metadata"]["AWS::CloudFormation::Init"]
    files = init["20-assets"]["files"]
    assert files["/opt/corenova/etc/extra.env"]["group"] in init["10-packages"]["groups"]
    env = str(files["/opt/corenova/etc/init.env"]["content"])
    for key in ("AllowedWebCidr", "DockerSocketAccess", "ExtraTcpPort", "ExtraUdpPort", "MaxUploadSizeMb"):
        assert "${" + key + "}" in env


def render_asset(tmp_path, name, overrides=None, extra_env=""):
    """执行真实 Bash 资产，仅替换主机路径和外部系统命令，禁止接触宿主机服务。"""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for path in ("opt/etc", "opt/bin", "systemd", "nginx/conf.d", "logrotate"):
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
    policy = (ROOT / "templates/cloudformation/fixed/init/05-access-policy.sh").read_text()
    (tmp_path / "opt/bin/05-access-policy.sh").write_text(policy.replace("/opt/corenova", str(tmp_path / "opt")))
    extra_file = tmp_path / "opt/etc/extra.env"
    extra_file.write_text(extra_env)
    text = (ROOT / "templates/cloudformation/fixed/init" / name).read_text()
    for source, target in (
        ("/opt/corenova", "opt"), ("/etc/systemd/system", "systemd"),
        ("/etc/nginx", "nginx"), ("/var/log/nginx", "logs"),
        ("/etc/logrotate.d", "logrotate"),
    ):
        text = text.replace(source, str(tmp_path / target))
    mocks = r'''
docker() {
  printf '%s\n' "docker $*" >> "$MOCK_LOG"
  case "$*" in
    'image inspect'*) printf '%s\n' "$MOCK_IMAGE_USER" ;;
    *'--entrypoint id'*)
      case "${!#}" in -u) echo 1000 ;; -g) echo 1000 ;; esac ;;
  esac
}
chown() { printf '%s\n' "chown $*" >> "$MOCK_LOG"; }
stat() {
  if [ "$2" = '%u' ]; then printf '%s\n' "${MOCK_OWNER%%:*}";
  else printf '%s\n' "$MOCK_OWNER"; fi
}
curl() { printf '%s\n' "$MOCK_DNS"; }
systemctl() { :; }
mountpoint() { return "${MOCK_MOUNT_RC:-0}"; }
sleep() { :; }
apt-get() { :; }
nginx() { :; }
'''
    script = tmp_path / name
    script.write_text(mocks + text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CFNOVA_")}
    env.update({
        "CFNOVA_APP_NAME": "demo", "CFNOVA_IMAGE_REFERENCE": "example/demo@sha256:abc",
        "CFNOVA_CONTAINER_PORT": "8080", "CFNOVA_DATA_DIR": str(data),
        "CFNOVA_APP_URL": "https://example.test/", "CFNOVA_EXTRA_ENV_FILE": str(extra_file),
        "MOCK_LOG": str(tmp_path / "commands.log"), "MOCK_IMAGE_USER": "1000:1000",
        "MOCK_OWNER": "1000:1000", "MOCK_DNS": "ec2.example.test",
    })
    env.update(overrides or {})
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)
    return proc


def test_runtime_resolves_legacy_url_and_native_url_wins(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {
        "CFNOVA_APP_URL_ENV_NAME": "ROOT_URL",
    }, "ROOT_URL=${CORENOVA_APP_URL}/ignored\nPUBLIC_URL=${CORENOVA_APP_URL}/")
    assert proc.returncode == 0, proc.stderr
    env = (tmp_path / "opt/env/demo.env").read_text()
    assert env.count("ROOT_URL=") == 1
    assert "ROOT_URL=https://example.test\n" in env
    assert "PUBLIC_URL=https://example.test/\n" in env
    assert "${" not in env
    unit = (tmp_path / "systemd/corenova-demo.service").read_text()
    assert "-p 127.0.0.1:8080:8080" in unit
    assert "EnvironmentFile=" not in unit
    assert "docker.sock" not in unit


def test_runtime_defaults_to_private_ssm_url(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {
        "CFNOVA_APP_URL": "", "CFNOVA_APP_URL_ENV_NAME": "ROOT_URL",
    }, "X=${CORENOVA_APP_URL}/")
    assert proc.returncode == 0, proc.stderr
    env = (tmp_path / "opt/env/demo.env").read_text()
    assert "ROOT_URL=http://localhost:8080\n" in env
    assert "X=http://localhost:8080/\n" in env


@pytest.mark.parametrize("extra", ["X=${UNSUPPORTED}", "X=a\rb"])
def test_runtime_fails_closed_on_invalid_or_unresolved_environment(tmp_path, extra):
    proc = render_asset(tmp_path, "30-app-container.sh", {
        "CFNOVA_APP_URL": "", "MOCK_DNS": "",
    }, extra)
    assert proc.returncode != 0
    assert not (tmp_path / "systemd/corenova-demo.service").exists()


def test_initial_ext4_data_directory_gets_image_ownership(tmp_path):
    (tmp_path / "data/lost+found").mkdir(parents=True)
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_OWNER": "0:0"})
    assert proc.returncode == 0, proc.stderr
    commands = (tmp_path / "commands.log").read_text()
    assert f"chown 1000:1000 {tmp_path}/data\n" in commands
    assert "--entrypoint id" not in commands
    assert "chown -R" not in commands


def test_numeric_uid_does_not_require_tools_in_the_image(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_IMAGE_USER": "1000"})
    assert proc.returncode == 0, proc.stderr
    commands = (tmp_path / "commands.log").read_text()
    assert f"chown 1000 {tmp_path}/data\n" in commands
    assert "--entrypoint id" not in commands


def test_existing_data_with_wrong_owner_is_not_modified(tmp_path):
    (tmp_path / "data").mkdir()
    existing = tmp_path / "data/project.txt"
    existing.write_text("keep me")
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_OWNER": "0:0"})
    assert proc.returncode != 0
    assert existing.read_text() == "keep me"
    assert "chown " not in (tmp_path / "commands.log").read_text()
    assert not (tmp_path / "systemd/corenova-demo.service").exists()


def test_existing_data_with_matching_uid_keeps_mode_and_contents(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "project.txt").write_text("keep me")
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_IMAGE_USER": "1000"})
    assert proc.returncode == 0, proc.stderr
    assert data.stat().st_mode & 0o777 == 0o700
    assert (data / "project.txt").read_text() == "keep me"
    assert f"chown 1000 {data}" not in (tmp_path / "commands.log").read_text()


def test_named_image_user_is_resolved_without_host_mounts(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_IMAGE_USER": "coder"})
    assert proc.returncode == 0, proc.stderr
    commands = (tmp_path / "commands.log").read_text().splitlines()
    probes = [line for line in commands if "--entrypoint id" in line]
    assert len(probes) == 2
    for command in probes:
        assert "--network none --read-only --cap-drop ALL" in command
        assert " -v " not in command and "--mount" not in command


def test_extra_ports_reach_docker_without_exposing_gui(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {
        "CFNOVA_EXTRA_TCP_PORT": "22000", "CFNOVA_EXTRA_UDP_PORT": "22000",
    })
    assert proc.returncode == 0, proc.stderr
    unit = (tmp_path / "systemd/corenova-demo.service").read_text()
    assert "-p 0.0.0.0:22000:22000/tcp" in unit
    assert "-p 0.0.0.0:22000:22000/udp" in unit
    assert "-p 127.0.0.1:8080:8080" in unit


@pytest.mark.parametrize("port", ["8080", "22", "443", "65536", "22000 --privileged"])
def test_extra_ports_reject_admin_bypass_and_injection(tmp_path, port):
    proc = render_asset(tmp_path, "30-app-container.sh", {"CFNOVA_EXTRA_TCP_PORT": port})
    assert proc.returncode != 0
    assert not (tmp_path / "systemd/corenova-demo.service").exists()


def test_proxy_shares_limits_websocket_and_access_control_on_both_schemes(tmp_path):
    pem = tmp_path / "test.pem"
    pem.write_text("test fixture")
    proc = render_asset(tmp_path, "10-nginx-base.sh", {
        "CFNOVA_TLS_PEM_PATH": str(pem), "CFNOVA_MAX_BODY_MB": "256",
    })
    assert proc.returncode == 0, proc.stderr
    config = (tmp_path / "nginx/conf.d/corenova-proxy.conf").read_text()
    common = (tmp_path / "nginx/snippets/corenova-app.conf").read_text()
    assert config.count("snippets/corenova-app.conf;") == 2
    assert "listen 80" in config and "listen 443 ssl" in config
    for directive in (
        "allow 127.0.0.1/32;", "deny all;", "client_max_body_size 256m;",
        "proxy_set_header Upgrade $http_upgrade;", "proxy_set_header Connection $connection_upgrade;",
        "proxy_http_version 1.1;", "proxy_request_buffering off;", "proxy_buffering off;",
        "proxy_set_header X-Forwarded-For $remote_addr;", "proxy_read_timeout 3600s;",
    ):
        assert directive in common
    assert "location = /corenova-health" not in config


@pytest.mark.parametrize("overrides", [
    {"CFNOVA_ALLOWED_WEB_CIDR": "0.0.0.0/0; allow all"},
    {"CFNOVA_ALLOWED_WEB_CIDR": "999.999.0.0/24"},
    {"CFNOVA_MAX_BODY_MB": "0"},
    {"CFNOVA_MAX_BODY_MB": "10241"},
])
def test_proxy_rejects_invalid_access_and_upload_parameters(tmp_path, overrides):
    proc = render_asset(tmp_path, "10-nginx-base.sh", overrides)
    assert proc.returncode != 0
    assert not (tmp_path / "nginx/conf.d/corenova-proxy.conf").exists()


def test_registered_data_mounts_and_default_host_capabilities_agree():
    for app in ("code-server", "gitea", "vikunja", "portainer", "netdata", "syncthing"):
        spec = yaml.safe_load((ROOT / "apps" / f"{app}.yaml").read_text())
        compose = yaml.safe_load((ROOT / spec["deploy"]["compose_file"]).read_text())
        service = compose["services"][app]
        assert service["volumes"] == ["${CORENOVA_DATA_DIR}:" + spec["deployment"]["data_path"]]
        assert not service.get("privileged") and not service.get("cap_add")
        assert not service.get("security_opt")
    vikunja = yaml.safe_load((ROOT / "apps/vikunja/docker-compose.yml").read_text())
    assert vikunja["services"]["vikunja"]["environment"]["VIKUNJA_FILES_BASEPATH"] == "/db"


def docker(*args, check=True):
    proc = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=120)
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.skipif(os.environ.get("CORENOVA_TEST_DOCKER") != "1", reason="显式启用本地 Docker 集成测试")
def test_real_nginx_upload_websocket_and_acl(tmp_path):
    for image in ("nginx:alpine", "node:22-alpine"):
        docker("image", "inspect", image)
    proc = render_asset(tmp_path, "10-nginx-base.sh", {
        "CFNOVA_SELF_SIGNED_TLS": "true", "CFNOVA_MAX_BODY_MB": "2",
        "CFNOVA_GOLDEN_MODE": "true", "CFNOVA_APP_NAME": "corenova-canary",
        "CFNOVA_ADMIN_AUTH": "false",
    })
    assert proc.returncode == 0, proc.stderr
    fixture = tmp_path / "nginx"
    for file in fixture.rglob("*.conf"):
        file.write_text(file.read_text().replace(str(fixture), "/fixture").replace(str(tmp_path / "logs"), "/tmp"))
    (fixture / "nginx.conf").write_text("events {}\nhttp { include /fixture/conf.d/*.conf; }\n")
    suffix = uuid.uuid4().hex[:12]
    upstream, proxy = f"cn-upstream-{suffix}", f"cn-proxy-{suffix}"
    server = r'''
const http = require('http'), crypto = require('crypto');
const server = http.createServer((req, res) => {
  let bytes = 0;
  req.on('data', b => bytes += b.length);
  req.on('end', () => res.end(JSON.stringify({bytes, headers: req.headers})));
});
server.on('upgrade', (req, socket) => {
  const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] +
    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
    'Sec-WebSocket-Accept: ' + accept + '\r\n\r\n');
});
server.listen(8080, '127.0.0.1');
'''
    client = r'''
const assert = require('assert'), http = require('http'), https = require('https');
const ip = Object.values(require('os').networkInterfaces()).flat().find(x => x.family === 'IPv4' && !x.internal).address;
async function request(tls, host, path, size = 0) {
  return new Promise((resolve, reject) => {
    const req = (tls ? https : http).request({host, port: tls ? 443 : 80, path,
      method: size ? 'POST' : 'GET', rejectUnauthorized: false, agent: false,
      headers: {'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'}}, res => {
      let body = ''; res.on('data', b => body += b); res.on('end', () => resolve([res.statusCode, body]));
    });
    req.on('error', reject); req.setTimeout(10000, () => req.destroy(new Error('timeout')));
    if (size) req.write(Buffer.alloc(size)); // 无 Content-Length，覆盖流式上传
    req.end();
  });
}
async function websocket(tls) {
  return new Promise((resolve, reject) => {
    const req = (tls ? https : http).request({host: '127.0.0.1', port: tls ? 443 : 80,
      rejectUnauthorized: false, agent: false, headers: {Connection: 'Upgrade', Upgrade: 'websocket',
      'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='} });
    req.on('upgrade', (res, socket) => { assert.equal(res.statusCode, 101); socket.destroy(); resolve(); });
    req.on('response', res => reject(new Error('no upgrade: ' + res.statusCode)));
    req.on('error', reject); req.setTimeout(10000, () => req.destroy(new Error('timeout'))); req.end();
  });
}
(async () => {
  for (const tls of [false, true]) {
    let [code, body] = await request(tls, '127.0.0.1', '/', 1536 * 1024);
    assert.equal(code, 200); const result = JSON.parse(body);
    assert.equal(result.bytes, 1536 * 1024);
    assert.equal(result.headers['x-forwarded-proto'], tls ? 'https' : 'http');
    assert.equal(result.headers['x-forwarded-for'], '127.0.0.1');
    assert.equal((await request(tls, '127.0.0.1', '/', 3 * 1024 * 1024))[0], 413);
    for (const path of ['/', '/corenova-health']) assert.equal((await request(tls, ip, path))[0], 403);
    await websocket(tls);
  }
  console.log('upload, websocket, ACL: PASS');
})().catch(e => { console.error(e); process.exit(1); });
'''
    try:
        docker("run", "-d", "--name", upstream, "--pull=never", "node:22-alpine", "node", "-e", server)
        docker("create", "--name", proxy, "--network", f"container:{upstream}", "--pull=never",
               "nginx:alpine", "nginx", "-c", "/fixture/nginx.conf", "-g", "daemon off;")
        docker("cp", str(fixture), f"{proxy}:/fixture")
        docker("start", proxy)
        for _ in range(30):
            if docker("inspect", "--format", "{{.State.Running}}", proxy) == "true":
                probe = docker("exec", upstream, "node", "-e",
                               "require('http').get('http://127.0.0.1',{agent:false},r=>{console.log(r.statusCode);r.resume()}).on('error',()=>{}).setTimeout(2000,function(){this.destroy()})",
                               check=False)
                if probe == "200":
                    break
            time.sleep(0.2)
        assert "PASS" in docker("exec", upstream, "node", "-e", client)
    finally:
        docker("rm", "-f", proxy, upstream, check=False)


@pytest.mark.skipif(os.environ.get("CORENOVA_TEST_DOCKER") != "1", reason="显式启用本地 Docker 集成测试")
@pytest.mark.parametrize("app,image", [
    ("code-server", "ghcr.io/coder/code-server:4.137.0"),
    ("vikunja", "vikunja/vikunja:2.6.0"),
    ("gitea", "docker.m.daocloud.io/gitea/gitea:1.27.3"),
    ("portainer", "portainer/portainer-ce:2.45.0"),
    ("netdata", "netdata/netdata:v2.11.0"),
    ("syncthing", "syncthing/syncthing:2.1.5"),
])
def test_real_single_container_starts_with_registered_data_and_environment(tmp_path, app, image):
    docker("image", "inspect", image)
    spec = yaml.safe_load((ROOT / "apps" / f"{app}.yaml").read_text())
    uid = docker("image", "inspect", "--format", "{{.Config.User}}", image)
    suffix = uuid.uuid4().hex[:12]
    container, volume = f"cn-app-{suffix}", f"cn-data-{suffix}"
    docker("volume", "create", volume)
    try:
        proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_IMAGE_USER": uid})
        assert proc.returncode == 0, proc.stderr
        # 从真实资产执行记录取初始化属主，不把测试配置为全员可写来掩盖权限问题。
        for line in (tmp_path / "commands.log").read_text().splitlines():
            if line.startswith("chown ") and line.endswith("/data"):
                owner = line.split()[1]
                docker("run", "--rm", "--pull=never", "--network", "none", "-v", f"{volume}:/data",
                       "alpine:3.19", "chown", owner, "/data")
        env = [value.replace("${CORENOVA_APP_URL}", "http://localhost") for value in spec["deploy"].get("extra_environment", [])]
        if name := spec["deployment"].get("app_url_env_name"):
            env.append(f"{name}=http://localhost")
        args = ["run", "-d", "--name", container, "--pull=never", "--mount",
                f"type=volume,source={volume},target={spec['deployment']['data_path']},volume-nocopy"]
        for value in env:
            args += ["-e", value]
        args.append(image)
        docker(*args)
        port, path = spec["deploy"]["container_port"], spec["health_check"]["endpoint"]
        probe = f"require('http').get('http://127.0.0.1:{port}{path}',{{agent:false}},r=>{{console.log(r.statusCode);r.resume()}}).on('error',()=>{{}}).setTimeout(2000,function(){{this.destroy()}})"
        for _ in range(45):
            code = docker("run", "--rm", "--pull=never", "--network", f"container:{container}",
                          "node:22-alpine", "node", "-e", probe, check=False)
            if code.startswith(("2", "3")):
                break
            time.sleep(1)
        assert code.startswith(("2", "3")), f"{app}: HTTP 就绪探针失败"
    finally:
        docker("rm", "-f", container, check=False)
        docker("volume", "rm", volume, check=False)
