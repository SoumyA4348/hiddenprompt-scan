"""Launch census shards on EC2 (free-plan c7i-flex.large, self-terminating).

    python cloud/launch.py --run "2201-2211 --versions v1" --tag base [--shards 16]
    python cloud/launch.py --script rxiv.py --run "biorxiv:2021-01..2026-10 medrxiv:2021-01..2026-10" --tag rxiv

Uploads the current code to s3://BUCKET/in/, fills cloud/userdata.sh, starts one instance per shard and
writes their IDs to cloud/instance-ids-<tag>.txt. Results appear in out/<tag>-<k>/; merge with cloud/merge.py.
"""
import argparse
import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
AWS = os.environ.get("AWS_CLI", "aws")
BUCKET = os.environ["CENSUS_BUCKET"]   # a private S3 bucket you own
AMI = "ami-0d6dd9e8265d48906"

p = argparse.ArgumentParser()
p.add_argument("--run", required=True, help='census.py run arguments, e.g. "2201-2211 --versions v1"')
p.add_argument("--tag", required=True)
p.add_argument("--shards", type=int, default=16)
p.add_argument("--script", default="census.py")
a = p.parse_args()

for f in ("census.py", "fetch.py", "hiddenprompt_scan.py", "rxiv.py"):
    subprocess.run([AWS, "s3", "cp", str(ROOT / f), f"s3://{BUCKET}/in/{f}", "--only-show-errors"], check=True)
subprocess.run([AWS, "s3", "sync", str(ROOT / "data" / "listings"), f"s3://{BUCKET}/in/data/listings",
                "--only-show-errors"], check=True)
template = (ROOT / "cloud" / "userdata.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
ids, tmp = [], pathlib.Path(tempfile.mkdtemp())
for k in range(a.shards):
    ud = tmp / f"ud{k}.sh"
    ud.write_text(template.replace("__BUCKET__", BUCKET).replace("__SHARD__", f"{k}/{a.shards}")
                  .replace("__TAG__", a.tag).replace("__RUN__", a.run).replace("__SCRIPT__", a.script), encoding="ascii", newline="\n")
    r = subprocess.run([AWS, "ec2", "run-instances", "--region", "us-east-1", "--image-id", AMI,
                        "--instance-type", "c7i-flex.large", "--count", "1",
                        "--iam-instance-profile", "Name=census-ec2", "--instance-initiated-shutdown-behavior",
                        "terminate", "--user-data", f"file://{ud}", "--block-device-mappings",
                        "DeviceName=/dev/sda1,Ebs={VolumeSize=12,VolumeType=gp3,DeleteOnTermination=true}",
                        "--tag-specifications", f"ResourceType=instance,Tags=[{{Key=Name,Value=census-{a.tag}-{k}}}]",
                        "--query", "Instances[0].InstanceId", "--output", "text"], capture_output=True, text=True)
    out = (r.stdout or r.stderr).strip()
    print(f"{a.tag} {k} -> {out[:200]}", flush=True)
    if out.startswith("i-"):
        ids.append(out)
(ROOT / "cloud" / f"instance-ids-{a.tag}.txt").write_text("\n".join(ids) + "\n")
print(f"launched {len(ids)}/{a.shards}")
