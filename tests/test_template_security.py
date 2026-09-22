"""Offline execution of shipped assets, with host/network commands replaced by strict fakes."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from corenova import golden, usertemplate
from tests.test_user_template import ROOT, render_asset

INIT = ROOT / "templates/cloudformation/fixed/init"


def test_every_embedded_shell_parses_and_remains_same_source():
    cfg = SimpleNamespace(root=ROOT)
    assert golden.asset_drift(cfg) == []
    assert golden.canary_parity_errors(cfg) == []
    for name, body in golden.inlined_assets(cfg, "app.yaml").items():
        assert body.startswith("#!/bin/bash\n"), f"{name}: sudo/execve requires a first-line shebang"
        proc = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
        assert proc.returncode == 0, (name, proc.stderr)
    tpl = yaml.safe_load((ROOT / "templates/cloudformation/fixed/app.yaml").read_text())
    userdata = tpl["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    assert subprocess.run(["bash", "-n"], input=userdata, text=True).returncode == 0


def test_sync_preserves_executable_shebang_and_is_idempotent(tmp_path):
    fixed = tmp_path / "templates/cloudformation/fixed"
    (fixed / "init").mkdir(parents=True)
    (fixed / "app.yaml").write_text((INIT.parent / "app.yaml").read_text())
    for source in INIT.glob("*.sh"):
        (fixed / "init" / source.name).write_text(source.read_text())
    cfg = SimpleNamespace(root=tmp_path)
    golden.sync_init_assets(cfg)
    first = (fixed / "app.yaml").read_bytes()
    golden.sync_init_assets(cfg)
    assert (fixed / "app.yaml").read_bytes() == first
    assert golden.asset_drift(cfg) == []


def test_persisted_https_url_reaches_actual_native_renderer(tmp_path):
    (tmp_path / "opt/etc").mkdir(parents=True)
    (tmp_path / "opt/etc/https.env").write_text("export CFNOVA_APP_URL=https://example.com\n")
    proc = render_asset(tmp_path, "30-app-container.sh", {
        "CFNOVA_APP_URL": "http://localhost:8080", "CFNOVA_APP_URL_ENV_NAME": "ROOT_URL",
    })
    assert proc.returncode == 0, proc.stderr
    env = (tmp_path / "opt/env/demo.env").read_text()
    assert "ROOT_URL=https://example.com\n" in env
    assert "CORENOVA_APP_URL=https://example.com\n" in env


def test_private_outputs_are_copyable_and_do_not_contain_credentials():
    tpl = usertemplate.build(ROOT)
    p, outputs = tpl["Parameters"], tpl["Outputs"]
    assert p["LaunchUrl"]["Default"] == "http://localhost:8080"
    assert p["AllowedWebCidr"]["Default"] == p["HttpIngressCidr"]["Default"] == "127.0.0.1/32"
    assert p["SelfSignedTls"]["Default"] == "false"
    assert p["AdminAuthEnabled"]["Default"] == "true"
    assert outputs["ResolvedLaunchUrl"]["Value"]["Fn::If"][-1] == "http://localhost:8080"
    command = outputs["SSMPortForwardCommand"]["Value"]["Fn::Sub"]
    argv = shlex.split(command.replace("${AWS::Region}", "us-east-1").replace("${Instance}", "i-example"))
    assert argv[:3] == ["aws", "ssm", "start-session"]
    assert argv[argv.index("--target") + 1] == "i-example"
    assert argv[argv.index("--document-name") + 1] == "AWS-StartPortForwardingSession"
    assert json.loads(argv[argv.index("--parameters") + 1]) == {"portNumber": ["80"], "localPortNumber": ["8080"]}
    assert "enable-https.sh" in outputs["EnableHttpsCommand"]["Value"]
    assert "admin.txt" in outputs["EnableHttpsCommand"]["Description"]
    assert all("credentials" not in str(v["Value"]) for v in outputs.values())


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.2", "http://localhost.evil", "http://user:pass@localhost", "http://localhost:99999"])
@pytest.mark.parametrize("asset", ["10-nginx-base.sh", "30-app-container.sh"])
def test_public_http_and_malformed_urls_fail_before_render(tmp_path, url, asset):
    proc = render_asset(tmp_path, asset, {"CFNOVA_APP_URL": url, "CFNOVA_ADMIN_AUTH": "true"})
    assert proc.returncode != 0
    assert not (tmp_path / "nginx/conf.d/corenova-proxy.conf").exists()
    assert not (tmp_path / "systemd/corenova-demo.service").exists()


def test_missing_explicit_pem_never_falls_back_to_http(tmp_path):
    proc = render_asset(tmp_path, "10-nginx-base.sh", {"CFNOVA_TLS_PEM_PATH": str(tmp_path / "absent.pem")})
    assert proc.returncode != 0
    assert "refusing HTTP fallback" in proc.stderr
    assert not (tmp_path / "nginx/conf.d/corenova-proxy.conf").exists()


@pytest.mark.parametrize("override", [
    {"CFNOVA_SELF_SIGNED_TLS": "true"},
    {"CFNOVA_SELF_SIGNED_TLS": "true", "CFNOVA_GOLDEN_MODE": "true"},
    {"CFNOVA_SELF_SIGNED_TLS": "true", "CFNOVA_GOLDEN_MODE": "true", "CFNOVA_APP_NAME": "corenova-canary", "CFNOVA_ADMIN_AUTH": "true"},
])
def test_golden_exception_requires_identity_and_no_credentials(tmp_path, override):
    proc = render_asset(tmp_path, "10-nginx-base.sh", override)
    assert proc.returncode != 0
    assert not (tmp_path / "nginx/conf.d/corenova-proxy.conf").exists()


def test_mount_protection_precedes_docker_and_survives_reboots(tmp_path):
    proc = render_asset(tmp_path, "30-app-container.sh", {"MOCK_MOUNT_RC": "1"})
    assert proc.returncode != 0
    assert not (tmp_path / "commands.log").exists()
    proc = render_asset(tmp_path, "30-app-container.sh")
    assert proc.returncode == 0, proc.stderr
    unit = (tmp_path / "systemd/corenova-demo.service").read_text()
    assert f"RequiresMountsFor={tmp_path}/data" in unit
    assert f"ExecStartPre=/usr/bin/mountpoint -q {tmp_path}/data" in unit
    assert unit.index("/usr/bin/mountpoint") < unit.index("/usr/bin/docker")


def run_https_asset(tmp_path, asset="enable-https.sh", args=None, failure="", dns="8.8.8.8", initialized=True):
    """No real certbot/curl/systemctl runs; all paths including the dummy secrets live in tmp_path."""
    for directory in ("opt/bin", "opt/etc", "nginx/conf.d", "nginx/snippets", "nginx/tls", "logs", "run", "logrotate", "letsencrypt/live/example.com"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    substitutions = (
        ("/opt/corenova", "opt"), ("/etc/nginx", "nginx"), ("/var/log/nginx", "logs"),
        ("/etc/logrotate.d", "logrotate"), ("/etc/letsencrypt", "letsencrypt"), ("/run/corenova", "run/corenova"),
    )
    mock_bin = tmp_path / "mocks"
    mock_bin.mkdir(exist_ok=True)
    # Every external operation that might affect the host/network is an executable fake.
    fake = '''#!/bin/bash
name="${0##*/}"
printf '%s %s\\n' "$name" "$*" >> "$MOCK_LOG"
case "$name" in
  systemctl)
    if [ "$FAILURE" = reload ] && [ "$1" = reload ] && [ ! -f "$ONCE" ]; then touch "$ONCE"; exit 1; fi ;;
  nginx) if [ "$FAILURE" = nginx ] && [ -f "$STATE" ]; then exit 1; fi ;;
  certbot) [ "$FAILURE" != acme ] || exit 1 ;;
  mountpoint) [ "$FAILURE" != mount ] || exit 1 ;;
  curl) case "$*" in *api/token*) printf token ;; *public-ipv4*) printf 8.8.8.8 ;; *) printf 200 ;; esac ;;
  openssl)
    if [ "$FAILURE" = certificate ] && [ "$1" = verify ]; then exit 1; fi
    case "$1" in rand) printf SYNTHETIC_PASSWORD ;; passwd) read -r secret; printf HASH ;;
      pkey) if [[ "$*" != *' -in '* ]]; then command cat >/dev/null; fi; printf fixture-public-key ;;
      x509) printf fixture-public-key ;; esac ;;
  sha256sum) command shasum -a 256 ;;
  python3)
    if [ "$#" = 3 ] && [ "$3" = 8.8.8.8 ]; then
      exec "$PYTHON" -c 'import socket,sys; dns=sys.argv.pop(); socket.getaddrinfo=lambda *a,**k: [(2,1,6,"",(dns,80))]; exec(sys.stdin.read())' "$2" "$3" "$DNS"
    fi
    exec "$PYTHON" "$@" ;;
  app-render)
    if [ -f "$STATE" ]; then . "$STATE"; else . "$INIT_ENV"; fi
    printf '%s\\n' "$CFNOVA_APP_URL" > "$APP_URL_FILE"
    [ "$FAILURE" != app ] || [ ! -f "$STATE" ] ;;
esac
'''
    for name in ("flock", "systemctl", "nginx", "certbot", "mountpoint", "curl", "openssl", "sha256sum", "python3", "app-render", "apt-get", "chown"):
        path = mock_bin / name
        path.write_text(fake)
        path.chmod(0o755)
    for source in INIT.glob("*.sh"):
        text = source.read_text().replace('"$EUID"', "0")
        for old, new in substitutions:
            text = text.replace(old, str(tmp_path / new))
        target = tmp_path / "opt/bin" / source.name
        target.write_text(text)
        target.chmod(0o755)
    # Native rendering is tested with the real renderer separately; here record transition order/rollback.
    (tmp_path / "opt/bin/30-app-container.sh").write_text("#!/bin/bash\nexec app-render\n")
    init_env = tmp_path / "opt/etc/init.env"
    init_env.write_text("\n".join(f"export {k}={shlex.quote(v)}" for k, v in {
        "CFNOVA_APP_NAME": "demo", "CFNOVA_APP_URL": "http://localhost:8080",
        "CFNOVA_ALLOWED_WEB_CIDR": "127.0.0.1/32", "CFNOVA_SELF_SIGNED_TLS": "false",
        "CFNOVA_CONTAINER_PORT": "8080", "CFNOVA_DATA_DIR": str(tmp_path / "data"),
    }.items()) + "\n")
    (tmp_path / "run/corenova-cfn-init.rc").write_text("0" if initialized else "1")
    (tmp_path / "nginx/conf.d/corenova-proxy.conf").write_text("private-config\n")
    (tmp_path / "nginx/snippets/corenova-app.conf").write_text("private-snippet\n")
    for name in ("cert.pem", "chain.pem", "privkey.pem", "fullchain.pem"):
        (tmp_path / "letsencrypt/live/example.com" / name).write_text("synthetic-test-" + name + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CFNOVA_", "RENEWED_"))}
    env.update(PATH=str(mock_bin) + ":" + env["PATH"], PYTHON=sys.executable,
               MOCK_LOG=str(tmp_path / "commands.log"), FAILURE=failure, DNS=dns,
               STATE=str(tmp_path / "opt/etc/https.env"), INIT_ENV=str(init_env),
               APP_URL_FILE=str(tmp_path / "app-url"), ONCE=str(tmp_path / "failed-once"))
    if asset == "renew-https.sh":
        (tmp_path / "opt/etc/https.env").write_text("export CFNOVA_HTTPS_HOSTNAME=example.com\n")
        (tmp_path / "nginx/tls/corenova.pem").write_text("previous-valid-pem\n")
    if args is None:
        args = ["example.com", "admin@example.com", "--confirm-initialized"] if asset == "enable-https.sh" else []
    return subprocess.run(["bash", "-x", str(tmp_path / "opt/bin" / asset), *args], env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("args", [[], ["example.com", "admin@example.com"],
    ["--bad", "admin@example.com", "--confirm-initialized"],
    ["example.com;touch /tmp/unsafe", "admin@example.com", "--confirm-initialized"],
    ["example.com", "a@b.com\n--staging", "--confirm-initialized"],
    ["example.com", "a..b@example.com", "--confirm-initialized"],
    ["127.0.0.1", "admin@example.com", "--confirm-initialized"],
    ["a..example.com", "admin@example.com", "--confirm-initialized"],
    ["example.com", "admin@example.com", "--force"],
])
def test_enable_https_rejects_parameters_without_acme(tmp_path, args):
    proc = run_https_asset(tmp_path, args=args)
    assert proc.returncode != 0
    log = (tmp_path / "commands.log").read_text() if (tmp_path / "commands.log").exists() else ""
    assert "certbot " not in log
    assert "systemctl stop nginx" not in log


@pytest.mark.parametrize("options", [{"dns": "8.8.4.4"}, {"initialized": False}, {"failure": "mount"}])
def test_initialization_and_dns_are_checked_before_acme(tmp_path, options):
    proc = run_https_asset(tmp_path, **options)
    assert proc.returncode != 0
    assert "certbot " not in (tmp_path / "commands.log").read_text()
    assert (tmp_path / "nginx/conf.d/corenova-proxy.conf").read_text() == "private-config\n"


@pytest.mark.parametrize("failure", ["acme", "certificate", "nginx", "app"])
def test_failed_https_transition_restores_private_state(tmp_path, failure):
    proc = run_https_asset(tmp_path, failure=failure)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not (tmp_path / "opt/etc/https.env").exists()
    assert (tmp_path / "nginx/conf.d/corenova-proxy.conf").read_text() == "private-config\n"
    assert (tmp_path / "nginx/snippets/corenova-app.conf").read_text() == "private-snippet\n"
    assert (tmp_path / "app-url").read_text().strip() == "http://localhost:8080"
    assert "systemctl restart nginx" in (tmp_path / "commands.log").read_text()


def test_https_success_keeps_auth_and_redirects_remote_http_before_proxy(tmp_path):
    proc = run_https_asset(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = (tmp_path / "opt/etc/https.env").read_text()
    assert "CFNOVA_APP_URL='https://example.com'" in state
    assert "CFNOVA_ADMIN_AUTH=true" in state
    assert (tmp_path / "app-url").read_text().strip() == "https://example.com"
    config = (tmp_path / "nginx/conf.d/corenova-proxy.conf").read_text()
    snippet = (tmp_path / "nginx/snippets/corenova-app.conf").read_text()
    assert 'if ($corenova_remote) { return 308 https://example.com$request_uri; }' in config
    assert config.index("return 308") < config.index("include ")
    assert '"http:1" "";' in config
    assert 'auth_basic $corenova_admin_realm;' in snippet
    assert 'proxy_set_header Authorization $corenova_authorization;' in snippet
    assert '127.0.0.1 0; ::1 0;' in config
    assert 'listen 80 default_server;' in config and 'listen 443 ssl default_server;' in config
    assert 'SYNTHETIC_PASSWORD' not in proc.stdout + proc.stderr + (tmp_path / "commands.log").read_text()
    assert (tmp_path / "opt/credentials/admin.txt").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "nginx/tls/corenova.pem").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "letsencrypt/renewal-hooks/deploy/corenova-https").is_symlink()
    assert "systemctl enable --now certbot.timer" in (tmp_path / "commands.log").read_text()


def test_renewal_failure_restores_previous_pem(tmp_path):
    proc = run_https_asset(tmp_path, "renew-https.sh", failure="reload")
    assert proc.returncode != 0
    assert (tmp_path / "nginx/tls/corenova.pem").read_text() == "previous-valid-pem\n"
    assert (tmp_path / "commands.log").read_text().count("systemctl reload nginx") == 2, proc.stderr
    assert not list((tmp_path / "nginx/tls").glob(".corenova-*"))


def execute_mount(tmp_path, monkeypatch, *, devices=("xvdf",), fs="ext4", contents=b"", system_disk=False,
                  signatures=None, children=False, mount_failure=False, mapping="sdf", nvme_mapping="sdf"):
    """Execute the actual inline Python, replacing only devices, IMDS and OS commands."""
    source = (INIT / "01-mount-data.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in devices:
        (dev / name).write_bytes(contents)
    data_root = tmp_path / "data"
    fstab = tmp_path / "fstab"
    fstab.write_text("# existing root entry\nUUID=root / ext4 defaults 0 1\n")
    source = source.replace("/var/lib/corenova", str(data_root)).replace("'/dev'", repr(str(dev)))
    source = source.replace("'/etc/fstab'", repr(str(fstab)))
    mount = data_root / "app/data"
    calls = []
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    current_fs = fs

    def command(args, **kwargs):
        nonlocal current_fs
        calls.append(args)
        cmd = args[0]
        out, rc = "", 0
        if cmd == "nvme":
            raw = bytearray(4096)
            raw[4:24] = b"vol0123456789abcdef0 "
            raw[24:64] = b"Amazon Elastic Block Store".ljust(40)
            raw[3072:3104] = nvme_mapping.encode().ljust(32)
            return subprocess.CompletedProcess(args, 0, bytes(raw), b"")
        if cmd == "findmnt":
            out = "/dev/root" if "SOURCE" in args else uuid
        elif cmd == "lsblk":
            if "--json" in args:
                out = json.dumps({"blockdevices": [{"type": "disk", "mountpoints": [None], "children": [{}] if children else []}]})
            else:
                out = str(dev / devices[0]) if system_disk else "/dev/root\n/dev/system"
        elif cmd == "blkid":
            out = uuid if "UUID" in args else current_fs
            rc = 0 if out else 2
        elif cmd == "wipefs":
            out = json.dumps({"signatures": signatures if signatures is not None else ([{"type": fs}] if fs else [])})
        elif cmd == "mkfs.ext4":
            current_fs = "ext4"
        elif cmd == "mount" and mount_failure:
            raise subprocess.CalledProcessError(32, args)
        elif cmd not in ("mount", "mountpoint", "udevadm"):
            pytest.fail(f"unmocked command: {args}")
        if kwargs.get("check") and rc:
            raise subprocess.CalledProcessError(rc, args)
        return subprocess.CompletedProcess(args, rc, out, "")

    def urlopen(request, **kwargs):
        if request.full_url.endswith("api/token"):
            assert request.method == "PUT"
            return io.BytesIO(b"test-token")
        assert request.get_header("X-aws-ec2-metadata-token") == "test-token"
        return io.BytesIO(b"root\nebs1" if request.full_url.endswith("mapping/") else mapping.encode())

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", command)
        m.setattr(stat, "S_ISBLK", lambda mode: True)
        m.setattr("urllib.request.urlopen", urlopen)
        m.setattr(sys, "argv", ["mount-data", str(mount)])
        try:
            exec(compile(source, "01-mount-data.sh:python", "exec"), {})
            error = None
        except (SystemExit, subprocess.CalledProcessError) as exc:
            error = exc
    return calls, fstab.read_text(), error


@pytest.mark.parametrize("device", ["sdf", "xvdf", "nvme7n1"])
def test_mount_identifies_mapping_not_enumeration_order(tmp_path, monkeypatch, device):
    calls, fstab, error = execute_mount(tmp_path, monkeypatch, devices=(device,))
    assert error is None, error
    assert not any(c[0] == "mkfs.ext4" for c in calls)
    assert "UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in fstab
    assert "nofail" not in fstab
    assert f"{tmp_path}/dev/" not in fstab


@pytest.mark.parametrize("options", [
    {"devices": ()}, {"system_disk": True}, {"devices": ("sdf", "xvdf")},
    {"devices": ("nvme7n1",), "nvme_mapping": "sda1"}, {"mapping": "sda1"},
    {"fs": "xfs"}, {"fs": "", "contents": b"unrecognized data"},
    {"fs": "", "signatures": [{"type": "gpt"}]}, {"children": True}, {"mount_failure": True},
])
def test_mount_failure_never_formats_or_updates_fstab(tmp_path, monkeypatch, options):
    calls, fstab, error = execute_mount(tmp_path, monkeypatch, **options)
    assert error is not None
    assert not any(c[0] == "mkfs.ext4" for c in calls)
    assert "UUID=aaaaaaaa" not in fstab


def test_only_confirmed_all_zero_new_ebs_is_formatted(tmp_path, monkeypatch):
    calls, fstab, error = execute_mount(tmp_path, monkeypatch, fs="", contents=b"\0" * 10000)
    assert error is None, error
    assert len([c for c in calls if c[0] == "mkfs.ext4"]) == 1
    assert "UUID=aaaaaaaa" in fstab


def test_userdata_signals_failure_and_exits_before_starting_services(tmp_path):
    tpl = usertemplate.build(ROOT)
    init = tpl["Resources"]["Instance"]["Metadata"]["AWS::CloudFormation::Init"]
    commands = init["30-run-assets"]["commands"]
    assert "01-mount-data.sh" in commands[sorted(commands)[0]]["command"]
    body = tpl["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    failure = body.split('if [ "$INIT_RC" -ne 0 ]; then', 1)[1].split("\nfi", 1)[0]
    failure = failure.replace("${AppName}", "demo").replace("${WaitHandle}", "https://offline.invalid")
    script = '''systemctl() { :; }
cfn-signal() { printf 'CFN_FAILURE:%s\\n' "$*"; return 1; }
curl() { printf 'FALLBACK:%s\\n' "$*"; }
INIT_RC=42
''' + failure + '\nprintf UNREACHABLE\n'
    proc = subprocess.run(["bash"], input=script, text=True, capture_output=True)
    assert proc.returncode == 42
    assert "CFN_FAILURE:-e 42" in proc.stdout
    assert '"Status":"FAILURE"' in proc.stdout
    assert "UNREACHABLE" not in proc.stdout


@pytest.mark.skipif(os.environ.get("CORENOVA_TEST_DOCKER") != "1", reason="requires existing local Docker images")
@pytest.mark.parametrize("golden_canary", [False, True])
def test_real_nginx_public_http_never_authenticates_or_proxies_credentials(tmp_path, golden_canary):
    import uuid
    from tests.test_user_template import docker

    for image in ("nginx:alpine", "node:22-alpine"):
        docker("image", "inspect", image)
    proc = run_https_asset(tmp_path)
    assert proc.returncode == 0, proc.stderr
    if golden_canary:
        (tmp_path / "opt/etc/https.env").unlink()
        proc = render_asset(tmp_path, "10-nginx-base.sh", {
            "CFNOVA_APP_NAME": "corenova-canary", "CFNOVA_GOLDEN_MODE": "true",
            "CFNOVA_SELF_SIGNED_TLS": "true", "CFNOVA_ADMIN_AUTH": "false",
            "CFNOVA_ALLOWED_WEB_CIDR": "0.0.0.0/0", "CFNOVA_APP_URL": "http://ec2.example.test",
        })
        assert proc.returncode == 0, proc.stderr
    pem = tmp_path / "nginx/tls/corenova.pem"
    cert = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                           "-keyout", str(pem), "-out", str(pem), "-subj", "/CN=example.com"], capture_output=True)
    assert cert.returncode == 0, cert.stderr
    password = subprocess.run(["openssl", "passwd", "-apr1", "-stdin"], input="test-password\n", text=True, capture_output=True)
    assert password.returncode == 0, password.stderr
    htpasswd = tmp_path / "nginx/corenova-admin.htpasswd"
    htpasswd.write_text("corenova:" + password.stdout)
    htpasswd.chmod(0o644)  # Synthetic fixture only; copied into an isolated container.
    fixture = tmp_path / "nginx"
    for path in fixture.rglob("*.conf"):
        path.write_text(path.read_text().replace(str(fixture), "/fixture").replace(str(tmp_path / "logs"), "/tmp"))
    (fixture / "nginx.conf").write_text("events {}\nhttp { include /fixture/conf.d/*.conf; }\n")
    suffix = uuid.uuid4().hex[:10]
    upstream, proxy = "cn-security-app-" + suffix, "cn-security-proxy-" + suffix
    server = "require('http').createServer((q,r)=>r.end(JSON.stringify({authorization:q.headers.authorization||'',path:q.url}))).listen(8080,'127.0.0.1')"
    client = r'''
const assert = require('assert'), http = require('http'), https = require('https');
const ip = Object.values(require('os').networkInterfaces()).flat().find(x => x.family === 'IPv4' && !x.internal).address;
const credential = 'Basic ' + Buffer.from('corenova:test-password').toString('base64');
const golden = process.argv[1] === 'golden';
function get(tls, host, authorization) {
  return new Promise((resolve,reject) => {
    const headers = {'Host':'attacker.invalid','X-Forwarded-For':'127.0.0.1'};
    if (authorization) headers.Authorization = authorization;
    const q = (tls ? https : http).get({host,port:tls?443:80,path:'/check',headers,agent:false,rejectUnauthorized:false},r => {
      let body=''; r.on('data',b=>body+=b); r.on('end',()=>resolve({code:r.statusCode,headers:r.headers,body}));
    }); q.on('error',reject); q.setTimeout(5000,()=>q.destroy(new Error('timeout')));
  });
}
(async()=>{
  for (const auth of [undefined, credential]) {
    const r = await get(false, ip, auth);
    if (golden) {
      assert.equal(r.code,200); assert.equal(JSON.parse(r.body).authorization,'');
    } else {
      assert.equal(r.code,308); assert.equal(r.headers.location,'https://example.com/check');
      assert(!r.body.includes('authorization'));
    }
    assert.equal(r.headers['www-authenticate'],undefined);
  }
  assert.equal((await get(true,ip)).code,golden?200:401);
  const authorized = await get(true,ip,credential);
  assert.equal(authorized.code,200); assert.equal(JSON.parse(authorized.body).authorization,credential);
  const privateResponse = await get(false,'127.0.0.1');
  assert.equal(privateResponse.code,200); assert.equal(JSON.parse(privateResponse.body).authorization,'');
  console.log('HTTP redirect without auth, TLS Basic, private SSM: PASS');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    try:
        docker("run", "-d", "--name", upstream, "--pull=never", "node:22-alpine", "node", "-e", server)
        docker("create", "--name", proxy, "--network", f"container:{upstream}", "--pull=never",
               "nginx:alpine", "nginx", "-c", "/fixture/nginx.conf", "-g", "daemon off;")
        docker("cp", str(fixture), f"{proxy}:/fixture")
        docker("start", proxy)
        docker("exec", proxy, "nginx", "-t", "-c", "/fixture/nginx.conf")
        assert "PASS" in docker("exec", upstream, "node", "-e", client, "golden" if golden_canary else "private")
    finally:
        docker("rm", "-f", proxy, upstream, check=False)
