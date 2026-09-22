#!/bin/bash
# Identify the requested EBS mapping before any filesystem operation; never guess disk order.
set +x
set -euo pipefail
python3 - "${CFNOVA_DATA_DIR:?}" <<'PY'
import datetime, hashlib, hmac, json, os, pathlib, re, stat, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def fail(message):
    sys.exit('[corenova] data volume: ' + message)


mount = pathlib.Path(sys.argv[1])
if (not re.fullmatch(r'/var/lib/corenova/[A-Za-z0-9_./-]+', str(mount))
        or '..' in mount.parts or mount.resolve() != mount or mount == pathlib.Path('/var/lib/corenova')):
    fail('unsafe mount path')
base = 'http://169.254.169.254/latest/'
token = urllib.request.urlopen(urllib.request.Request(base + 'api/token', method='PUT', headers={
    'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=5).read().decode()


def metadata(path):
    return urllib.request.urlopen(urllib.request.Request(base + 'meta-data/' + path, headers={
        'X-aws-ec2-metadata-token': token}), timeout=5).read().decode().strip()


# mkfs is the only irreversible action in this script, so "this blank disk is really a volume this
# stack just created" needs a proof. Disk content is not one: on Nitro, blocks of a brand-new EBS
# volume that were never written read back as stable non-zero data (measured 2026-09-22 on fresh
# encrypted and unencrypted gp3 volumes: zeros only in the first 4 KiB), so any "new volume is
# all zeros" check refuses legitimate first deployments. EC2 states the same fact in one call -
# a brand-new volume has no snapshot, is attached only to this instance, and was created minutes ago.
MAX_NEW_VOLUME_AGE = datetime.timedelta(hours=24)
API_RETRIES = 8
API_RETRY_INTERVAL_S = 15


def _local(name):
    # DescribeVolumes* responses are namespaced, so every tag comparison cuts the namespace off.
    return name.rsplit('}', 1)[-1]


def _children(element, name):
    return [child for child in element if _local(child.tag) == name]


def _fields(element):
    return {_local(child.tag): (child.text or '').strip() for child in element}


class ApiError(Exception):
    """The request or the grant is wrong (4xx): retrying only delays the stack."""


def ec2_query(params):
    """Signed EC2 Query call with the instance role credentials, stdlib only: the image has no
    awscli/botocore, and every credential read goes through IMDSv2."""
    region = metadata('placement/region')
    role = metadata('iam/security-credentials/').splitlines()[0]
    creds = json.loads(metadata('iam/security-credentials/' + role))
    host = 'ec2.' + region + '.' + metadata('services/domain')
    query = '&'.join(key + '=' + urllib.parse.quote(str(params[key]), safe='')
                     for key in sorted(params))
    when = datetime.datetime.now(datetime.timezone.utc)
    amz_date, date_stamp = when.strftime('%Y%m%dT%H%M%SZ'), when.strftime('%Y%m%d')
    headers = {'host': host, 'x-amz-date': amz_date, 'x-amz-security-token': creds['Token']}
    canonical_headers = ''.join(key + ':' + headers[key] + '\n' for key in sorted(headers))
    signed_headers = ';'.join(sorted(headers))
    scope = '/'.join([date_stamp, region, 'ec2', 'aws4_request'])

    def sha256_hex(value):
        return hashlib.sha256(value.encode()).hexdigest()

    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256', amz_date, scope,
        sha256_hex('\n'.join(['GET', '/', query, canonical_headers, signed_headers, sha256_hex('')]))])
    signing_key = ('AWS4' + creds['SecretAccessKey']).encode()
    for part in (date_stamp, region, 'ec2', 'aws4_request'):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    headers['authorization'] = (
        'AWS4-HMAC-SHA256 Credential=' + creds['AccessKeyId'] + '/' + scope
        + ', SignedHeaders=' + signed_headers
        + ', Signature=' + hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest())
    request = urllib.request.Request('https://' + host + '/?' + query, headers=headers)
    try:
        return urllib.request.urlopen(request, timeout=15).read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        code = re.search(r'<Code>([^<]+)</Code>', body)
        detail = 'HTTP ' + str(exc.code) + ' ' + (code.group(1) if code else body[:200])
        if exc.code >= 500:
            raise urllib.error.URLError(detail) from None
        # The message must name the fix: 403 here is a missing ec2:DescribeVolumes on the role.
        raise ApiError(host + ' ' + str(exc.code) + ' ' + (code.group(1) if code else body[:200])
                       + ' (instance role needs ec2:DescribeVolumes)') from None


def describe_volume(volume_id):
    root = xml.etree.ElementTree.fromstring(ec2_query({
        'Action': 'DescribeVolumes', 'Version': '2016-11-15', 'VolumeId.1': volume_id}))
    volumes = [item for group in _children(root, 'volumeSet') for item in _children(group, 'item')]
    if len(volumes) != 1:
        raise ApiError('EC2 reported ' + str(len(volumes)) + ' volumes for ' + volume_id)
    volume = _fields(volumes[0])
    volume['tags'] = {f['key']: f['value'] for group in _children(volumes[0], 'tagSet')
                      for f in map(_fields, _children(group, 'item'))}
    volume['attachments'] = [f for group in _children(volumes[0], 'attachmentSet')
                             for f in map(_fields, _children(group, 'item'))]
    return volume


def volume_id_of(device):
    """EBS exposes the volume id as the block device serial: NVMe drops the dash, Xen keeps it."""
    path = pathlib.Path('/sys/block') / pathlib.Path(device).name / 'device' / 'serial'
    try:
        serial = path.read_text(encoding='ascii').strip()
    except OSError:
        return ''
    match = re.fullmatch(r'vol-?([0-9a-f]{8,17})', serial)
    return 'vol-' + match.group(1) if match else ''


def assert_volume_is_new(device):
    volume_id = volume_id_of(device)
    app_name = os.environ.get('CFNOVA_APP_NAME', '')
    if not volume_id:
        fail('cannot read an EBS volume id from the block device serial')
    if not app_name:
        fail('CFNOVA_APP_NAME is required to authorize formatting')
    error = None
    for attempt in range(API_RETRIES):
        try:
            volume = describe_volume(volume_id)
            break
        except ApiError as exc:
            fail('cannot authorize formatting ' + volume_id + ': ' + str(exc))
        except Exception as exc:  # noqa: BLE001 - throttle, consistency and transport are transient
            error = exc
            if attempt + 1 < API_RETRIES:
                time.sleep(API_RETRY_INTERVAL_S)
    else:
        fail('cannot confirm ' + volume_id + ' is newly created (' + type(error).__name__ + ': '
             + str(error) + '); refusing format')
    instance_id = metadata('instance-id')
    attachments = volume['attachments']
    # EC2 Query XML is camelCase (the volume state is <status>); _fields only strips the namespace.
    problems = []
    if volume.get('snapshotId'):
        problems.append('restored from ' + volume['snapshotId'])
    if volume.get('status') != 'in-use':
        problems.append('status=' + (volume.get('status') or 'unknown'))
    if volume.get('multiAttachEnabled') == 'true':
        problems.append('Multi-Attach')
    if len(attachments) != 1 or attachments[0].get('instanceId') != instance_id:
        problems.append('not attached solely to ' + instance_id)
    if volume['tags'].get('corenova:app') != app_name:
        problems.append('tag corenova:app=' + (volume['tags'].get('corenova:app') or 'missing'))
    try:
        created = datetime.datetime.strptime(volume['createTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
        age = datetime.datetime.now(datetime.timezone.utc) - created.replace(
            tzinfo=datetime.timezone.utc)
        if age > MAX_NEW_VOLUME_AGE:
            problems.append('created ' + str(age).split('.')[0] + ' ago')
    except (KeyError, ValueError):
        problems.append('unreadable CreateTime')
    if problems:
        fail('blank volume ' + volume_id + ' is not provably new: ' + '; '.join(problems))
    return volume_id


names = metadata('block-device-mapping/').splitlines()
mappings = {metadata('block-device-mapping/' + key).removeprefix('/dev/') for key in names}
if not mappings.intersection({'sdf', 'xvdf'}):
    fail('IMDS has no requested sdf/xvdf mapping')
run('udevadm', 'settle', '--timeout=60')
candidates = set()
for name in ('sdf', 'xvdf'):
    path = pathlib.Path('/dev') / name
    if path.exists() and stat.S_ISBLK(path.stat().st_mode):
        candidates.add(str(path.resolve()))
for path in pathlib.Path('/dev').glob('nvme*n1'):
    raw = subprocess.run(['nvme', 'id-ctrl', '--raw-binary', str(path)], check=True, capture_output=True).stdout
    model = raw[24:64].decode('ascii', errors='replace').strip(' \x00')
    serial = raw[4:24].decode('ascii', errors='replace').strip(' \x00')
    mapping = raw[3072:3104].decode('ascii', errors='replace').strip(' \x00').removeprefix('/dev/')
    if model == 'Amazon Elastic Block Store' and re.fullmatch(r'vol-?[0-9a-f]+', serial) and mapping in ('sdf', 'xvdf'):
        candidates.add(str(path.resolve()))
if len(candidates) != 1:
    fail('missing or ambiguous EBS mapping')
device = candidates.pop()
root = run('findmnt', '-nro', 'SOURCE', '/').split('[')[0]
root_disks = run('lsblk', '-snrpo', 'NAME', root).splitlines()
if device in [os.path.realpath(x) for x in root_disks]:
    fail('refusing system disk')
info = json.loads(run('lsblk', '--json', '--output', 'NAME,TYPE,MOUNTPOINTS', device))['blockdevices']
if len(info) != 1 or info[0]['type'] != 'disk' or info[0].get('children'):
    fail('not an unpartitioned data disk')
mounted = [x for x in info[0].get('mountpoints', []) if x]
if mounted and mounted != [str(mount)]:
    fail('device already mounted elsewhere')
probe = subprocess.run(['blkid', '-p', '-o', 'value', '-s', 'TYPE', device], capture_output=True, text=True)
if probe.returncode not in (0, 2):
    fail('ambiguous or unreadable filesystem')
fs = probe.stdout.strip()
signatures = json.loads(run('wipefs', '--json', '--no-act', device)).get('signatures', [])
if fs == 'ext4':
    if any(x.get('type') != 'ext4' for x in signatures):
        fail('unexpected signature alongside ext4')
elif not fs and probe.returncode == 2 and not signatures and not mounted:
    # A signature-free disk may still contain unknown data, and reading it cannot tell (see above),
    # so formatting needs the volume to be provably new by EC2 identity instead.
    print('[corenova] ' + assert_volume_is_new(device) + ' confirmed newly created; formatting')
    run('mkfs.ext4', '-m', '0', '-L', 'corenova-data', device)
else:
    fail('unknown filesystem; refusing format')
uuid = run('blkid', '-s', 'UUID', '-o', 'value', device)
if not re.fullmatch(r'[0-9a-fA-F-]{36}', uuid):
    fail('invalid filesystem UUID')
mount.mkdir(parents=True, exist_ok=True)
fstab = pathlib.Path('/etc/fstab')
lines = fstab.read_text().splitlines()
kept = []
for line in lines:
    fields = line.split()
    if fields and not line.lstrip().startswith('#') and len(fields) > 1 and fields[1] == str(mount):
        if fields[0] != 'UUID=' + uuid:
            fail('conflicting fstab mount')
    else:
        kept.append(line)
if not mounted:
    run('mount', '-t', 'ext4', '-o', 'noatime', 'UUID=' + uuid, str(mount))
run('mountpoint', '-q', str(mount))
if run('findmnt', '-nro', 'UUID', '--target', str(mount)) != uuid:
    fail('mounted filesystem identity mismatch')
kept.append(f'UUID={uuid} {mount} ext4 defaults,noatime 0 2')
tmp = fstab.with_name('fstab.corenova.tmp')
with open(tmp, 'w') as stream:
    stream.write('\n'.join(kept) + '\n')
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(tmp, 0o644)
os.replace(tmp, fstab)
print('[corenova] confirmed data volume mounted at ' + str(mount))
PY
