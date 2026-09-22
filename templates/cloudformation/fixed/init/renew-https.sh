#!/bin/bash
# Certbot deploy hook: swap the key+chain as one root-only PEM, with rollback on reload failure.
set +x
set -euo pipefail
[ "$EUID" -eq 0 ] && [ "$#" -eq 0 ] || exit 1
umask 077
. /opt/corenova/etc/https.env
HOST="${CFNOVA_HTTPS_HOSTNAME:?}"
[[ "$HOST" =~ ^[a-z0-9][a-z0-9.-]+$ ]] || exit 1
LINEAGE="/etc/letsencrypt/live/$HOST"
[ "${RENEWED_LINEAGE:-$LINEAGE}" = "$LINEAGE" ] || exit 0
openssl x509 -in "$LINEAGE/cert.pem" -noout -checkhost "$HOST"
openssl x509 -in "$LINEAGE/cert.pem" -noout -checkend 86400
openssl verify -verify_hostname "$HOST" -CAfile /etc/ssl/certs/ca-certificates.crt -untrusted "$LINEAGE/chain.pem" "$LINEAGE/cert.pem"
CERT_KEY="$(openssl x509 -in "$LINEAGE/cert.pem" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)"
PRIVATE_KEY="$(openssl pkey -in "$LINEAGE/privkey.pem" -pubout -outform DER | sha256sum)"
[ "$CERT_KEY" = "$PRIVATE_KEY" ] || { echo '[corenova] certificate/key mismatch' >&2; exit 1; }
install -d -m 0700 /etc/nginx/tls
PEM=/etc/nginx/tls/corenova.pem
TMP="$(mktemp /etc/nginx/tls/.corenova-pem.XXXXXX)"
BACKUP="$(mktemp /etc/nginx/tls/.corenova-backup.XXXXXX)"
HAD_PEM=false
if [ -f "$PEM" ]; then cp -p "$PEM" "$BACKUP"; HAD_PEM=true; fi
cleanup() { rm -f "$TMP" "$BACKUP"; }
trap cleanup EXIT
cat "$LINEAGE/privkey.pem" "$LINEAGE/fullchain.pem" > "$TMP"
chmod 0600 "$TMP"
mv -f "$TMP" "$PEM"
reload_nginx() {
  nginx -t || return 1
  if systemctl is-active --quiet nginx; then systemctl reload nginx; fi
}
if ! reload_nginx; then
  if [ "$HAD_PEM" = true ]; then mv -f "$BACKUP" "$PEM"; else rm -f "$PEM"; fi
  reload_nginx || systemctl stop nginx
  exit 1
fi
