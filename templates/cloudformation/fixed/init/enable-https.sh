#!/bin/bash
# Run only after completing the application's own setup through the private SSM tunnel.
set +x
set -euo pipefail
fail() { echo "[corenova] $*" >&2; exit 1; }
[ "$#" -eq 3 ] && [ "${3:-}" = --confirm-initialized ] || fail 'usage: enable-https.sh <hostname> <acme-email> --confirm-initialized'
[ "$EUID" -eq 0 ] || fail 'root is required'
HOST="$1"
EMAIL="$2"
python3 - "$HOST" "$EMAIL" <<'PY'
import ipaddress, re, sys
host, email = sys.argv[1:]
def domain(value):
    return (len(value) <= 253 and '.' in value and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x)
            for x in value.split('.')) and not value.endswith(('.localhost', '.local', '.invalid', '.test')))
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    sys.exit('hostname must be a public DNS name, not an IP address')
if not domain(host) or not host.rsplit('.', 1)[1].isalpha():
    sys.exit('invalid public hostname')
local = email.split('@')[0]
if (len(email) > 254 or len(local) > 64 or email.count('@') != 1
        or not re.fullmatch(r'[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*', local)
        or not domain(email.split('@')[-1])):
    sys.exit('invalid ACME email')
PY
umask 077
exec 9>/run/corenova-enable-https.lock
flock -n 9 || fail 'another HTTPS transition is running'
[ ! -e /opt/corenova/etc/https.env ] || fail 'HTTPS already configured; use certbot renew'
. /opt/corenova/etc/init.env
[ "${CFNOVA_APP_URL:-http://localhost:8080}" = http://localhost:8080 ] || fail 'initialize using LaunchUrl=http://localhost:8080 first'
[ "${CFNOVA_ALLOWED_WEB_CIDR:-}" = 127.0.0.1/32 ] &&
[ "${CFNOVA_SELF_SIGNED_TLS:-false}" = false ] &&
[ -z "${CFNOVA_TLS_PEM_PATH:-}" ] &&
[ "${CFNOVA_GOLDEN_MODE:-false}" = false ] || fail 'requires a private, non-Golden deployment'
[ "$(cat /run/corenova-cfn-init.rc)" = 0 ] || fail 'bootstrap has not completed'
mountpoint -q "${CFNOVA_DATA_DIR:?}" || fail 'data volume is not mounted'
systemctl is-active --quiet "corenova-${CFNOVA_APP_NAME:?}" || fail 'application is not running'
CODE="$(curl -sS --connect-timeout 3 --max-time 15 -o /dev/null -w '%{http_code}' "http://127.0.0.1${CFNOVA_HEALTH_PATH:-/}")"
case "$CODE" in 2??|3??) ;; *) fail 'private application readiness check failed' ;; esac
# No app-independent probe can prove that its administrator was created: the flag attests to that step.
TOKEN="$(curl -fsS --connect-timeout 3 --max-time 10 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)"
PUBLIC_IP="$(curl -fsS --connect-timeout 3 --max-time 10 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)"
python3 - "$HOST" "$PUBLIC_IP" <<'PY'
import ipaddress, signal, socket, sys
signal.alarm(15)
host, expected = sys.argv[1:]
if not ipaddress.ip_address(expected).is_global:
    sys.exit('instance has no public IPv4 address')
addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)}
# This flow configures the instance's public IPv4 only; reject AAAA and multi-target records too.
if addresses != {expected}:
    sys.exit('all DNS addresses must point directly to this instance public IPv4 (remove AAAA/proxy records)')
PY
BACKUP="$(mktemp -d /opt/corenova/etc/.https-backup.XXXXXX)"
cp -p /etc/nginx/conf.d/corenova-proxy.conf "$BACKUP/proxy.conf"
cp -p /etc/nginx/snippets/corenova-app.conf "$BACKUP/app.conf"
COMMITTED=false
rollback() {
  rc=$?
  trap - EXIT HUP INT TERM
  set +e
  if [ "$COMMITTED" != true ]; then
    rm -f /opt/corenova/etc/https.env
    rm -f /etc/letsencrypt/renewal-hooks/{pre,post}/corenova-nginx /etc/letsencrypt/renewal-hooks/deploy/corenova-https
    if cp -p "$BACKUP/proxy.conf" /etc/nginx/conf.d/corenova-proxy.conf &&
       cp -p "$BACKUP/app.conf" /etc/nginx/snippets/corenova-app.conf; then
      . /opt/corenova/etc/init.env
      export CFNOVA_APP_URL=http://localhost:8080
      /opt/corenova/bin/30-app-container.sh || systemctl stop "corenova-$CFNOVA_APP_NAME"
      if nginx -t; then systemctl restart nginx || systemctl stop nginx; else systemctl stop nginx; fi
    else
      systemctl stop nginx
    fi
    echo '[corenova] HTTPS failed; restored private configuration or stopped nginx (no public HTTP fallback)' >&2
  fi
  rm -rf "$BACKUP"
  exit "$rc"
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y certbot
systemctl stop nginx
certbot certonly --standalone --non-interactive --agree-tos --no-eff-email \
  --preferred-challenges http --cert-name "$HOST" -d "$HOST" --email="$EMAIL" \
  --server https://acme-v02.api.letsencrypt.org/directory
# nginx is still private until the certificate and application-native URL are ready.
systemctl start nginx
cat > "$BACKUP/https.env" <<EOF
export CFNOVA_HTTPS_HOSTNAME='$HOST'
export CFNOVA_APP_URL='https://$HOST'
export CFNOVA_SERVER_NAMES='$HOST'
export CFNOVA_TLS_PEM_PATH='/etc/nginx/tls/corenova.pem'
export CFNOVA_SELF_SIGNED_TLS=false
export CFNOVA_ALLOWED_WEB_CIDR='0.0.0.0/0'
export CFNOVA_ADMIN_AUTH=true
EOF
chmod 0600 "$BACKUP/https.env"
mv -f "$BACKUP/https.env" /opt/corenova/etc/https.env
/opt/corenova/bin/renew-https.sh
/opt/corenova/bin/30-app-container.sh
/opt/corenova/bin/10-nginx-base.sh
install -d -m 0755 /etc/letsencrypt/renewal-hooks/{pre,deploy,post}
printf '#!/bin/sh\nexec systemctl stop nginx\n' > /etc/letsencrypt/renewal-hooks/pre/corenova-nginx
printf '#!/bin/sh\nnginx -t && exec systemctl start nginx\n' > /etc/letsencrypt/renewal-hooks/post/corenova-nginx
chmod 0755 /etc/letsencrypt/renewal-hooks/{pre,post}/corenova-nginx
ln -sf /opt/corenova/bin/renew-https.sh /etc/letsencrypt/renewal-hooks/deploy/corenova-https
systemctl enable --now certbot.timer
COMMITTED=true
echo "[corenova] HTTPS enabled: https://$HOST; private SSM port 80 remains available."
echo '[corenova] Whole-site Basic protection is retained. Read /opt/corenova/credentials/admin.txt through SSM as root.'
echo '[corenova] Open inbound TCP 80 (ACME/redirect) and 443 in your security group only when ready; this script changes no AWS resources.'
