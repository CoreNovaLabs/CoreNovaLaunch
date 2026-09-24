#!/bin/bash
# cfn-init asset: renders the systemd unit that runs the application container.
# Nothing about the app image or port is baked in - both arrive as CFN parameters, so the same
# template verifies Ghost today and any other registered app tomorrow.
set +x
set -euo pipefail
PERSISTENCE="${CFNOVA_PERSISTENCE-volume}"
DATA_DIR=""
DATA_MOUNT=""
DATA_ARGS=""
MOUNT_REQUIRES=""
MOUNT_PRE=""
case "$PERSISTENCE" in
  none)
    [ "${CFNOVA_DATA_VOLUME_SIZE-0}" = 0 ] && [ -z "${CFNOVA_DATA_CONTAINER_PATH-}" ] || {
      echo '[corenova] none requires size 0 and an empty container data path' >&2; exit 1;
    } ;;
  volume)
    SIZE="${CFNOVA_DATA_VOLUME_SIZE-30}"
    DATA_DIR="${CFNOVA_DATA_DIR:-/var/lib/corenova/app/data}"
    DATA_MOUNT="${CFNOVA_DATA_CONTAINER_PATH-/data}"
    [[ "$SIZE" =~ ^[1-9][0-9]{0,3}$ ]] && (( SIZE >= 8 && SIZE <= 4096 )) &&
      [ -n "$DATA_MOUNT" ] || {
        echo '[corenova] volume requires integer size 8..4096 and a container data path' >&2; exit 1;
      }
    DATA_ARGS="-v ${DATA_DIR}:${DATA_MOUNT}"
    MOUNT_REQUIRES="RequiresMountsFor=${DATA_DIR}"
    MOUNT_PRE="ExecStartPre=/usr/bin/mountpoint -q ${DATA_DIR}"
    ;;
  *) echo '[corenova] invalid persistence mode' >&2; exit 1 ;;
esac
. /opt/corenova/bin/05-access-policy.sh

APP_NAME="${CFNOVA_APP_NAME:?}"
IMAGE_REFERENCE="${CFNOVA_IMAGE_REFERENCE:?}"
CONTAINER_PORT="${CFNOVA_CONTAINER_PORT:?}"
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

# URL policy supplies the private SSM default, never an inferred public HTTP URL.
if [ "$PERSISTENCE" = volume ]; then
  mountpoint -q "$DATA_DIR" || { echo '[corenova] data volume is not mounted'; exit 1; }
fi

# 规范化根 URL，追加环境变量中的 / 不会生成双斜杠。
APP_URL="${APP_URL%/}"
if [ -n "$APP_URL_ENV_NAME" ]; then
  [[ "$APP_URL_ENV_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || exit 1
  [ -n "$APP_URL" ] || { echo '[corenova] 无法解析应用 URL'; exit 1; }
fi

docker pull "$IMAGE_REFERENCE"
if [ "$PERSISTENCE" = volume ]; then
  # 已有目录不重设 mode，不递归修改已有数据。
  [ -d "$DATA_DIR" ] || install -d -m 0755 "$DATA_DIR"
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
fi
mkdir -p /opt/corenova/env

cat > "$ENV_FILE" <<EOF
CORENOVA_APP_IMAGE=${IMAGE_REFERENCE}
CORENOVA_CONTAINER_PORT=${CONTAINER_PORT}
CORENOVA_APP_URL=${APP_URL}
EOF
if [ "$PERSISTENCE" = volume ]; then
  printf 'CORENOVA_DATA_DIR=%s\n' "$DATA_DIR" >> "$ENV_FILE"
fi
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
if [ "$PERSISTENCE" = volume ]; then
  chown root:corenova-app "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
else
  # Docker/systemd reads this as root; stateless apps need no host ownership changes.
  chmod 0600 "$ENV_FILE"
fi

cat > "/etc/systemd/system/corenova-${APP_NAME}.service" <<EOF
[Unit]
Description=CoreNova application container (${APP_NAME})
After=docker.service network-online.target
Requires=docker.service
${MOUNT_REQUIRES}

[Service]
Type=simple
Restart=always
RestartSec=5
${MOUNT_PRE}
ExecStartPre=-/usr/bin/docker rm -f ${APP_NAME}
ExecStartPre=/usr/bin/docker pull ${IMAGE_REFERENCE}
ExecStart=/usr/bin/docker run --rm --name ${APP_NAME} \\
  --label corenova.app=${APP_NAME} \\
  --log-driver awslogs --log-opt awslogs-group=${LOG_GROUP} --log-opt awslogs-stream=${APP_NAME} --log-opt awslogs-region=${AWS_REGION} \\
  --stop-timeout 30 \\
  -p 127.0.0.1:${CONTAINER_PORT}:${CONTAINER_PORT} \\
  ${DATA_ARGS} \\
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
# The start job still has docker rm/pull and the awslogs dial-out ahead of ExecStart, so a cold
# first boot can legitimately read "activating" past any fixed sleep (vikunja measured 2026-09-22;
# a one-shot is-active exit 3 then killed cfn-init long before the real gate). Convergence is
# bounded here; business readiness stays owned by 40-ready's health polling.
for _ in $(seq 1 24); do
  [ "$(systemctl is-active "corenova-${APP_NAME}" || true)" = active ] && break
  sleep 5
done
systemctl is-active "corenova-${APP_NAME}"
docker ps --filter "name=^/${APP_NAME}$" --format '{{.Names}} {{.Status}}'
