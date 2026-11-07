#!/bin/bash
# Audit-only instance (one machine): finishes the hidden-text screen + judge for arXiv and bio/medRxiv.
# Launch index 0-14: bioRxiv + medRxiv census shards (rxiv.py). Launch index 15: hidden-text audit (screen + judge).
# Each instance pushes results to S3 every 10 min, deletes the remaining launch schedules, and terminates when done.
exec > /var/log/census-boot.log 2>&1
set -x
shutdown -h +600                       # hard cap: 10 hours, whatever happens
B=__BUCKET__
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 3600")
IDX=15
apt-get update -y && apt-get install -y python3-venv
python3 -m venv /opt/venv
/opt/venv/bin/pip install -q pymupdf==1.28.2 httpx==0.28.1 boto3
cat > /opt/sync.py <<'PY'
import pathlib, sqlite3, sys
import boto3
s3, bucket, mode = boto3.client("s3", region_name="us-east-1"), sys.argv[1], sys.argv[2]
root = pathlib.Path("/opt/census")
if mode == "down":                     # <prefix>/<path> -> /opt/census/<dest>/<path>
    prefix, dest = sys.argv[3], root / sys.argv[4]
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        for it in page.get("Contents", []):
            out = dest / it["Key"][len(prefix) + 1:]
            out.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, it["Key"], str(out))
elif mode == "up":                     # consistent snapshots of the given databases + logs -> out/<tag>/
    tag = sys.argv[3]
    for name in sys.argv[4:]:
        db = root / "data" / name
        if not db.exists():
            continue
        src, snap = sqlite3.connect(db, timeout=60), sqlite3.connect("/tmp/snap.sqlite")
        src.backup(snap); snap.close(); src.close()
        s3.upload_file("/tmp/snap.sqlite", bucket, f"out/{tag}/{name}")
    for f in (root / "data/logs").glob("*"):
        s3.upload_file(str(f), bucket, f"out/{tag}/{f.name}")
elif mode == "stop?":                  # exit 0 if someone uploaded ctl/STOP
    try:
        s3.head_object(Bucket=bucket, Key="ctl/STOP")
    except Exception:
        sys.exit(1)
elif mode == "unschedule":             # the fleet is up: drop the remaining retry schedules
    sch = boto3.client("scheduler", region_name="us-east-1")
    for name in sys.argv[3:]:
        try:
            sch.delete_schedule(Name=name)
        except Exception as e:
            print("unschedule", name, e)
PY
mkdir -p /opt/census/data/logs
/opt/venv/bin/python /opt/sync.py $B down in .
cd /opt/census
if [ "$IDX" = "15" ]; then
  TAG=audit
  DBS="audit.sqlite judge.sqlite"
  /opt/venv/bin/python /opt/sync.py $B down audit data
  P=/opt/venv/bin/python
  ($P -u audit.py data/census.sqlite --workers 16 > data/logs/audit-screen.log 2>&1;
   $P -u audit.py data/census.sqlite --table rxiv --workers 16 > data/logs/audit-screen-rxiv.log 2>&1;
   $P -u audit.py data/census.sqlite --judge deepseek.v3.2 --workers 16 > data/logs/audit-judge.log 2>&1;
   $P -u audit.py data/census.sqlite --table rxiv --judge deepseek.v3.2 --workers 16 > data/logs/audit-judge-rxiv.log 2>&1) &
else
  TAG=rxiv-$IDX
  DBS="census.sqlite"
  /opt/venv/bin/python -u rxiv.py run biorxiv:2021-01..2026-10 medrxiv:2021-01..2026-10 --workers 3 --shard $IDX/15 \
    > data/logs/aws.log 2> data/logs/aws.err &
fi
PID=$!
while kill -0 $PID 2>/dev/null; do
  sleep 600
  /opt/venv/bin/python /opt/sync.py $B stop? && touch data/STOP && pkill -f audit.py
  /opt/venv/bin/python /opt/sync.py $B up $TAG $DBS
done
/opt/venv/bin/python /opt/sync.py $B up $TAG $DBS
shutdown -h now
