"""Download every shard's database from S3 and merge it into data/census.sqlite.

    python cloud/merge.py <bucket>

Shards started from a copy of the local database, so rows they share are identical;
INSERT OR REPLACE keeps the newest copy of each PDF. Uses the AWS CLI already logged in.
"""
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
AWS = os.environ.get("AWS_CLI", "aws")
bucket = sys.argv[1]
out = ROOT / "data" / "shards"
subprocess.run([AWS, "s3", "sync", f"s3://{bucket}/out/", str(out), "--only-show-errors"], check=True)

db = ROOT / "data" / "census.sqlite"
shutil.copy(db, db.with_suffix(".before-merge.sqlite"))
con = sqlite3.connect(db)
before = con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
for shard in sorted(out.glob("*-*/census.sqlite")):  # shard-k (census), base-k (2022 baseline), ...
    con.execute("ATTACH ? AS s", (str(shard),))
    con.execute("INSERT OR REPLACE INTO scans SELECT * FROM s.scans")
    if con.execute("SELECT 1 FROM s.sqlite_master WHERE name='rxiv'").fetchone():   # bioRxiv/medRxiv shards
        con.execute("CREATE TABLE IF NOT EXISTS rxiv AS SELECT * FROM s.rxiv WHERE 0")
        con.execute("INSERT OR REPLACE INTO rxiv SELECT * FROM s.rxiv")
    con.commit()
    con.execute("DETACH s")
    print(shard.parent.name, "merged")
print(f"rows: {before} -> {con.execute('SELECT COUNT(*) FROM scans').fetchone()[0]}")
