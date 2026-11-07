#!/bin/bash
# EC2 boot script: pull code + database from S3, run the census, push results back every 10 min, then power off.
# The instance is launched with shutdown behaviour = terminate, so powering off deletes it.
exec > /var/log/census-boot.log 2>&1
set -x
shutdown -h +600                       # hard cap: 10 hours, whatever happens
B=__BUCKET__
SHARD=__SHARD__
TAG=__TAG__                            # results go to out/$TAG-$K
K=${SHARD%/*}
apt-get update -y && apt-get install -y python3-venv
python3 -m venv /opt/venv
/opt/venv/bin/pip install -q pymupdf==1.28.2 httpx==0.28.1 boto3
cat > /opt/sync.py <<'PY'
import pathlib, sqlite3, sys
import boto3
s3, bucket, mode = boto3.client("s3"), sys.argv[1], sys.argv[2]
root = pathlib.Path("/opt/census")
if mode == "down":                     # in/<path> -> /opt/census/<path>
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="in/"):
        for it in page.get("Contents", []):
            dst = root / it["Key"][3:]
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, it["Key"], str(dst))
elif mode == "up":                     # consistent snapshot of the database + logs -> out/
    src, snap = sqlite3.connect(root / "data/census.sqlite"), sqlite3.connect("/tmp/snap.sqlite")
    src.backup(snap); snap.close(); src.close()
    s3.upload_file("/tmp/snap.sqlite", bucket, f"out/{sys.argv[3]}/census.sqlite")
    for f in (root / "data/logs").glob("aws*"):
        s3.upload_file(str(f), bucket, f"out/{sys.argv[3]}/{f.name}")
elif mode == "stop?":                  # exit 0 if someone uploaded ctl/STOP
    try:
        s3.head_object(Bucket=bucket, Key="ctl/STOP")
    except Exception:
        sys.exit(1)
PY
/opt/venv/bin/python /opt/sync.py $B down
mkdir -p /opt/census/data/logs && cd /opt/census
/opt/venv/bin/python -u __SCRIPT__ run __RUN__ --workers 3 --shard $SHARD > data/logs/aws.log 2> data/logs/aws.err &
PID=$!
while kill -0 $PID 2>/dev/null; do
  sleep 600
  /opt/venv/bin/python /opt/sync.py $B stop? && touch data/STOP
  /opt/venv/bin/python /opt/sync.py $B up $TAG-$K
done
/opt/venv/bin/python /opt/sync.py $B up $TAG-$K
shutdown -h now
