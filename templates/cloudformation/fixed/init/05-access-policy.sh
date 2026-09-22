#!/bin/bash
# Shared by both renderers; persisted HTTPS state wins over initial CFN parameters.
set +x
set -euo pipefail
if [ -f /opt/corenova/etc/https.env ]; then
  . /opt/corenova/etc/https.env
fi
GOLDEN_HTTP=false
if [ "${CFNOVA_GOLDEN_MODE:-false}" = true ]; then
  if [ "${CFNOVA_APP_NAME:-}" != corenova-canary ] ||
     [ "${CFNOVA_SELF_SIGNED_TLS:-false}" != true ] ||
     [ "${CFNOVA_ADMIN_AUTH:-true}" != false ]; then
    echo '[corenova] invalid Golden identity/security combination' >&2
    exit 1
  fi
  GOLDEN_HTTP=true
fi
if [ "${CFNOVA_SELF_SIGNED_TLS:-false}" = true ] && [ "$GOLDEN_HTTP" != true ]; then
  echo '[corenova] self-signed TLS is restricted to the explicit Golden canary' >&2
  exit 1
fi
if [ -n "${CFNOVA_TLS_PEM_PATH:-}" ] && [ ! -s "$CFNOVA_TLS_PEM_PATH" ]; then
  echo '[corenova] configured TLS PEM is missing; refusing HTTP fallback' >&2
  exit 1
fi
CFNOVA_APP_URL="${CFNOVA_APP_URL:-http://localhost:8080}"
python3 - "$CFNOVA_APP_URL" "$GOLDEN_HTTP" <<'PY'
import re, sys
from urllib.parse import urlsplit
url, golden = sys.argv[1:]
p = urlsplit(url)
if (not re.fullmatch(r'https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/+?=&-]*)?', url)
        or not p.hostname or p.username or p.password or (p.port is not None and not 1 <= p.port <= 65535)):
    sys.exit('invalid LaunchUrl')
if p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1') and golden != 'true':
    sys.exit('public HTTP LaunchUrl is forbidden; initialize through SSM, then enable HTTPS')
PY
export CFNOVA_APP_URL
