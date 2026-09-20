#!/bin/bash
# cfn-init asset: renders the nginx reverse proxy that fronts every CoreNova app container.
# Filename keeps the substring "nginx" so platformref.compute_revisions() can key
# nginx_base_revision off it (Platform Contract §2.1 public-mode mapping).
set -xeuo pipefail

APP_NAME="${CFNOVA_APP_NAME:?}"
CONTAINER_PORT="${CFNOVA_CONTAINER_PORT:?}"
SERVER_NAMES="${CFNOVA_SERVER_NAMES:-}"
TLS_PEM_PATH="${CFNOVA_TLS_PEM_PATH:-}"
SELF_SIGNED_TLS="${CFNOVA_SELF_SIGNED_TLS:-false}"
ALLOWED_WEB_CIDR="${CFNOVA_ALLOWED_WEB_CIDR:-127.0.0.1/32}"
MAX_BODY_MB="${CFNOVA_MAX_BODY_MB:-100}"
ADMIN_AUTH="${CFNOVA_ADMIN_AUTH:-false}"

# 默认仅允许 SSM 转发后的本机访问；不信任客户端提供的转发地址。
python3 -c 'import ipaddress, sys; ipaddress.IPv4Network(sys.argv[1], strict=False)' "$ALLOWED_WEB_CIDR"
[[ "$MAX_BODY_MB" =~ ^[1-9][0-9]{0,4}$ ]] && (( MAX_BODY_MB <= 10240 )) || exit 1
# 仅允许结构化选项，与 DockerSocketAccess 同一防注入纪律。
case "$ADMIN_AUTH" in true|false) ;; *) exit 1 ;; esac

install -d -m 0755 /etc/nginx/tls /var/log/nginx
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx logrotate || {
  echo "[corenova] nginx install failed"; exit 1;
}
# Ubuntu 的 nginx 包自带 sites-enabled/default（也是 80 default_server），
# 与我们这份 conf 并存会让 nginx -t 直接失败（实测 duplicate default server）。
rm -f /etc/nginx/sites-enabled/default

if [ -n "$TLS_PEM_PATH" ] && [ ! -s "$TLS_PEM_PATH" ]; then
  echo "[corenova] WARN: TlsPemPath=$TLS_PEM_PATH is missing or empty, 443 stays closed"
  TLS_PEM_PATH=""
fi

if [ -z "$TLS_PEM_PATH" ] && [ "$SELF_SIGNED_TLS" = "true" ]; then
  # Golden Verification must measure a real 443 listener; a self-signed bundle is enough because
  # the probe asserts reachability through the SG, not certificate trust.
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout /etc/nginx/tls/corenova-selfsigned.pem \
    -out /etc/nginx/tls/corenova-selfsigned.pem \
    -subj "/CN=corenova-canary" >/dev/null 2>&1
  chmod 0600 /etc/nginx/tls/corenova-selfsigned.pem
  TLS_PEM_PATH=/etc/nginx/tls/corenova-selfsigned.pem
fi

# HTTP 与 HTTPS 使用同一组代理、上传和访问控制指令，避免两条入口漂移。
install -d -m 0755 /etc/nginx/snippets

# 管理保护：整站 HTTP Basic。凭据只在首次生成后复用（栈更新不换密码）；
# 本机（SSM 端口转发）与健康探针经 geo 空 realm 免认证，远程访问必须先过
# AllowedWebCidr 再持凭据，两道门独立。
AUTH_LINES=""
ADMIN_GEO=""
if [ "$ADMIN_AUTH" = "true" ]; then
  install -d -m 0700 /opt/corenova/credentials
  if [ ! -s /opt/corenova/credentials/admin.txt ]; then
    ADMIN_PASS="$(openssl rand -hex 12)"
    printf 'corenova:%s\n' "$ADMIN_PASS" > /opt/corenova/credentials/admin.txt
  fi
  chown root:root /opt/corenova/credentials/admin.txt
  chmod 0600 /opt/corenova/credentials/admin.txt
  ADMIN_PASS="$(cut -d: -f2 /opt/corenova/credentials/admin.txt)"
  printf 'corenova:%s\n' "$(openssl passwd -6 "$ADMIN_PASS")" > /etc/nginx/corenova-admin.htpasswd
  chown root:www-data /etc/nginx/corenova-admin.htpasswd
  chmod 0640 /etc/nginx/corenova-admin.htpasswd
  ADMIN_GEO='geo $corenova_admin_realm { default "CoreNova admin access"; 127.0.0.1 ""; ::1 ""; }'
  AUTH_LINES='  auth_basic $corenova_admin_realm;
  auth_basic_user_file /etc/nginx/corenova-admin.htpasswd;'
fi

cat > /etc/nginx/snippets/corenova-app.conf <<EOF
allow 127.0.0.1;
allow ::1;
allow ${ALLOWED_WEB_CIDR};
deny all;
client_max_body_size ${MAX_BODY_MB}m;
client_body_timeout 300s;
location / {
${AUTH_LINES}
  proxy_pass http://corenova_${APP_NAME};
  proxy_http_version 1.1;
  proxy_set_header Host \$http_host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$remote_addr;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_set_header Upgrade \$http_upgrade;
  proxy_set_header Connection \$connection_upgrade;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_request_buffering off;
  proxy_buffering off;
}
EOF

cat > /etc/nginx/conf.d/corenova-proxy.conf <<EOF
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
${ADMIN_GEO}

upstream corenova_${APP_NAME} {
  server 127.0.0.1:${CONTAINER_PORT} max_fails=3 fail_timeout=10s;
  keepalive 16;
}

server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _ ${SERVER_NAMES};
  access_log /var/log/nginx/corenova-${APP_NAME}.access.log;
  include /etc/nginx/snippets/corenova-app.conf;
}
EOF

if [ -n "$TLS_PEM_PATH" ]; then
  cat >> /etc/nginx/conf.d/corenova-proxy.conf <<EOF

server {
  listen 443 ssl default_server;
  listen [::]:443 ssl default_server;
  server_name _ ${SERVER_NAMES};
  ssl_certificate ${TLS_PEM_PATH};
  ssl_certificate_key ${TLS_PEM_PATH};
  ssl_protocols TLSv1.2 TLSv1.3;
  access_log /var/log/nginx/corenova-${APP_NAME}.access.log;
  include /etc/nginx/snippets/corenova-app.conf;
}
EOF
fi

nginx -t
systemctl daemon-reload
systemctl enable nginx
systemctl restart nginx
echo "[corenova] nginx serving for ${APP_NAME} on 127.0.0.1:${CONTAINER_PORT}"

# Logrotate: keep 7 days of platform and nginx logs. The corenova log directory
# and nginx access log are not covered by any package-provided logrotate rule.
cat > /etc/logrotate.d/corenova <<EOF
/var/log/corenova/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
/var/log/nginx/corenova-*.access.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
