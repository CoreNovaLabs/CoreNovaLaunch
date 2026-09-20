#!/bin/bash
# cfn-init asset: renders the systemd unit that runs the application container.
# Nothing about the app image or port is baked in - both arrive as CFN parameters, so the same
# template verifies Ghost today and any other registered app tomorrow.
set -xeuo pipefail

APP_NAME="${CFNOVA_APP_NAME:?}"
IMAGE_REFERENCE="${CFNOVA_IMAGE_REFERENCE:?}"
CONTAINER_PORT="${CFNOVA_CONTAINER_PORT:?}"
DATA_DIR="${CFNOVA_DATA_DIR:-/var/lib/corenova/app/data}"
DATA_MOUNT="${CFNOVA_DATA_CONTAINER_PATH:-/data}"
APP_URL="${CFNOVA_APP_URL:-}"
APP_URL_ENV_NAME="${CFNOVA_APP_URL_ENV_NAME:-}"
EXTRA_ENV_FILE="${CFNOVA_EXTRA_ENV_FILE:-}"
LOG_GROUP="${CFNOVA_LOG_GROUP:-/corenova/apps}"
AWS_REGION="${CFNOVA_AWS_REGION:-us-east-1}"
ENV_FILE="/opt/corenova/env/${APP_NAME}.env"
DOCKER_SOCKET_ACCESS="${CFNOVA_DOCKER_SOCKET_ACCESS:-false}"
HOST_METRICS_ACCESS="${CFNOVA_HOST_METRICS_ACCESS:-false}"
EXTRA_TCP_PORT="${CFNOVA_EXTRA_TCP_PORT:-0}"
EXTRA_UDP_PORT="${CFNOVA_EXTRA_UDP_PORT:-0}"

# 仅允许结构化选项，禁止向 systemd 注入任意 Docker 参数。
EXTRA_ARGS=""
case "$DOCKER_SOCKET_ACCESS" in
  true)
    [ -S /var/run/docker.sock ] || { echo '[corenova] Docker socket 不存在'; exit 1; }
    EXTRA_ARGS="--mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"
    ;;
  false) ;;
  *) exit 1 ;;
esac
case "$HOST_METRICS_ACCESS" in
  true)
    # 宿主机指标只读挂载（netdata 约定 /host 前缀）；不暴露任何可写宿主路径。
    EXTRA_ARGS="$EXTRA_ARGS -v /proc:/host/proc:ro -v /sys:/host/sys:ro -v /etc/localtime:/etc/localtime:ro"
    ;;
  false) ;;
  *) exit 1 ;;
esac
for protocol in tcp udp; do
  if [ "$protocol" = tcp ]; then port="$EXTRA_TCP_PORT"; else port="$EXTRA_UDP_PORT"; fi
  [[ "$port" =~ ^(0|[1-9][0-9]{0,4})$ ]] || exit 1
  if [ "$port" != 0 ]; then
    # 禁止绕过管理入口代理，也不得占用 SSH/HTTP(S)。
    if (( port > 65535 || port < 1024 || port == CONTAINER_PORT )); then
      echo '[corenova] 额外端口必须为独立的非特权业务端口'; exit 1
    fi
    EXTRA_ARGS="$EXTRA_ARGS -p 0.0.0.0:${port}:${port}/${protocol}"
  fi
done

# LaunchUrl is optional because the instance public DNS name only exists after launch.
# Resolve it through IMDSv2 so apps such as Ghost receive a correct absolute base URL.
if [ -z "$APP_URL" ]; then
  set +x
  IMDS_TOKEN="$(curl -fsS --connect-timeout 2 --max-time 5 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token || true)"
  if [ -n "$IMDS_TOKEN" ]; then
    PUBLIC_DNS="$(curl -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" http://169.254.169.254/latest/meta-data/public-hostname || true)"
    if [ -n "$PUBLIC_DNS" ]; then
      APP_URL="http://${PUBLIC_DNS}"
    fi
  fi
  set -x
fi

# 规范化根 URL，追加环境变量中的 / 不会生成双斜杠。
APP_URL="${APP_URL%/}"
if [ -n "$APP_URL_ENV_NAME" ]; then
  [[ "$APP_URL_ENV_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || exit 1
  [ -n "$APP_URL" ] || { echo '[corenova] 无法解析应用 URL'; exit 1; }
fi

# 已有目录不重设 mode，不递归修改已有数据。
[ -d "$DATA_DIR" ] || install -d -m 0755 "$DATA_DIR"
docker pull "$IMAGE_REFERENCE"
IMAGE_USER="$(docker image inspect --format '{{.Config.User}}' "$IMAGE_REFERENCE")"
if [ -n "$IMAGE_USER" ] && [ "$IMAGE_USER" != root ] && [ "$IMAGE_USER" != 0 ]; then
  # Run only id, without network, writable rootfs, host mounts or application entrypoint.
  if [[ "$IMAGE_USER" =~ ^([0-9]+):([0-9]+)$ ]]; then
    DATA_UID="${BASH_REMATCH[1]}"
    DATA_GID="${BASH_REMATCH[2]}"
  elif [[ "$IMAGE_USER" =~ ^[0-9]+$ ]]; then
    # distroless 镜像可能没有 id；数字 UID 已足以保证 owner 可写，不猜测组号。
    DATA_UID="$IMAGE_USER"
    DATA_GID=""
  else
    DATA_UID="$(docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint id "$IMAGE_REFERENCE" -u)"
    DATA_GID="$(docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint id "$IMAGE_REFERENCE" -g)"
  fi
  [[ "$DATA_UID" =~ ^[0-9]+$ && "$DATA_GID" =~ ^[0-9]*$ ]] || exit 1
  DATA_OWNER="${DATA_UID}${DATA_GID:+:$DATA_GID}"
  OWNER_FORMAT='%u'
  [ -z "$DATA_GID" ] || OWNER_FORMAT='%u:%g'
  # 新 ext4 卷自带 lost+found；它不是应用数据，不能导致首次部署被拒。
  if [ -z "$(find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)" ]; then
    chown "$DATA_OWNER" "$DATA_DIR"
  elif [ "$(stat -c "$OWNER_FORMAT" "$DATA_DIR")" != "$DATA_OWNER" ]; then
    echo '[corenova] existing data ownership requires operator review; refusing recursive chown'
    exit 1
  fi
fi
mkdir -p /opt/corenova/env

cat > "$ENV_FILE" <<EOF
CORENOVA_APP_IMAGE=${IMAGE_REFERENCE}
CORENOVA_CONTAINER_PORT=${CONTAINER_PORT}
CORENOVA_DATA_DIR=${DATA_DIR}
CORENOVA_APP_URL=${APP_URL}
EOF
if [ -n "$EXTRA_ENV_FILE" ] && [ -s "$EXTRA_ENV_FILE" ]; then
  # Expand only the supported URL placeholder; never eval arbitrary config.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*=.+$ && "$line" != *$'\r'* ]] || {
      echo '[corenova] 环境变量必须为单行 KEY=VALUE'; exit 1;
    }
    if [[ "$line" == *'${CORENOVA_APP_URL}'* ]] && [ -z "$APP_URL" ]; then
      echo '[corenova] 无法解析应用 URL'; exit 1
    fi
    line="${line//\$\{CORENOVA_APP_URL\}/$APP_URL}"
    if [[ "$line" == *'${'* ]]; then
      echo '[corenova] unsupported environment placeholder'; exit 1
    fi
    # 原生 URL 字段优先，兼容旧 Manifest 中重复的 URL 行。
    [ "${line%%=*}" = "$APP_URL_ENV_NAME" ] && continue
    printf '%s\n' "$line" >> "$ENV_FILE"
  done < "$EXTRA_ENV_FILE"
fi
if [ -n "$APP_URL_ENV_NAME" ]; then
  printf '%s=%s\n' "$APP_URL_ENV_NAME" "$APP_URL" >> "$ENV_FILE"
fi
chown root:corenova-app "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat > "/etc/systemd/system/corenova-${APP_NAME}.service" <<EOF
[Unit]
Description=CoreNova application container (${APP_NAME})
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker rm -f ${APP_NAME}
ExecStartPre=/usr/bin/docker pull ${IMAGE_REFERENCE}
ExecStart=/usr/bin/docker run --rm --name ${APP_NAME} \\
  --label corenova.app=${APP_NAME} \\
  --log-driver awslogs --log-opt awslogs-group=${LOG_GROUP} --log-opt awslogs-stream=${APP_NAME} --log-opt awslogs-region=${AWS_REGION} \\
  --stop-timeout 30 \\
  -p 127.0.0.1:${CONTAINER_PORT}:${CONTAINER_PORT} \\
  -v ${DATA_DIR}:${DATA_MOUNT} \\
  --env-file ${ENV_FILE} \\
  ${EXTRA_ARGS} \\
  ${IMAGE_REFERENCE}
ExecStop=/usr/bin/docker stop ${APP_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "corenova-${APP_NAME}"
systemctl restart "corenova-${APP_NAME}"
sleep 5
systemctl is-active "corenova-${APP_NAME}"
docker ps --filter "name=^/${APP_NAME}$" --format '{{.Names}} {{.Status}}'
