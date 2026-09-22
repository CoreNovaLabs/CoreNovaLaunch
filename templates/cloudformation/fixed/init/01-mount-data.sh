#!/bin/bash
# Identify the requested EBS mapping before any filesystem operation; never guess disk order.
set +x
set -euo pipefail
python3 - "${CFNOVA_DATA_DIR:?}" <<'PY'
import json, os, pathlib, re, stat, subprocess, sys, urllib.request


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
    # A signature-free disk may still contain unknown data. Only an all-zero EBS disk is new.
    with open(device, 'rb', buffering=0) as disk:
        while chunk := disk.read(8 * 1024 * 1024):
            if chunk.count(0) != len(chunk):
                fail('unrecognized non-empty disk; refusing format')
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
