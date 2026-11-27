#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-backup & Auto-restore MariaDB database + SePay state to a Private Hugging Face Dataset.
Uses pure HTTP (requests) so zero heavy dependencies are needed.
Configured via Space Secrets:
  HF_TOKEN       - Hugging Face Write Token
  HF_BACKUP_REPO - e.g. "username/pkm-db-backup" (private dataset)
"""
import base64
import gzip
import json
import os
import subprocess
import sys
import time
import requests

HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
HF_BACKUP_REPO = os.environ.get("HF_BACKUP_REPO", "").strip()
DATA_DIR = os.environ.get("PKM_DATA_DIR", "/home/user/data")
MYSQL_SOCK = os.environ.get("MYSQL_SOCK", "/tmp/mysqld.sock")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}


def ensure_dataset_repo():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        return False
    parts = HF_BACKUP_REPO.split("/", 1)
    if len(parts) != 2:
        return False
    org, name = parts
    try:
        r = requests.post(
            "https://huggingface.co/api/repos/create",
            headers=HEADERS,
            json={"type": "dataset", "name": name, "organization": org, "private": True},
            timeout=20,
        )
        # 200/201 created, 409 already exists
        if r.status_code in (200, 201, 409):
            return True
        # Fallback without organization (personal account)
        r2 = requests.post(
            "https://huggingface.co/api/repos/create",
            headers=HEADERS,
            json={"type": "dataset", "name": name, "private": True},
            timeout=20,
        )
        return r2.status_code in (200, 201, 409)
    except Exception as e:
        print(f"[backup] ensure_dataset_repo warn: {e}", flush=True)
        return False


def download_file(filename):
    url = f"https://huggingface.co/datasets/{HF_BACKUP_REPO}/resolve/main/{filename}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        if r.status_code == 200 and len(r.content) > 0:
            return r.content
    except Exception as e:
        print(f"[backup] download {filename} warn: {e}", flush=True)
    return None


def upload_files(files_dict, commit_msg):
    """Commit multiple small files to HF Dataset via /api/datasets/{repo}/commit/main."""
    url = f"https://huggingface.co/api/datasets/{HF_BACKUP_REPO}/commit/main"
    lines = [json.dumps({"key": "header", "value": {"summary": commit_msg}})]
    for path, raw_bytes in files_dict.items():
        lines.append(json.dumps({
            "key": "file",
            "value": {
                "content": base64.b64encode(raw_bytes).decode("ascii"),
                "path": path,
                "encoding": "base64",
            }
        }))
    payload = "\n".join(lines).encode("utf-8")
    r = requests.post(
        url,
        headers={**HEADERS, "Content-Type": "application/x-ndjson"},
        data=payload,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print(f"[backup] commit failed HTTP {r.status_code}: {r.text[:200]}", flush=True)
        return False
    return True


def restore():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        print("[backup] HF_TOKEN/HF_BACKUP_REPO not set; skipping remote restore.", flush=True)
        return False
    ensure_dataset_repo()
    sql_gz = download_file("pokemon_latest.sql.gz")
    if sql_gz:
        try:
            sql_bytes = gzip.decompress(sql_gz)
            if b"CREATE TABLE" in sql_bytes or b"INSERT INTO" in sql_bytes:
                out_path = "/tmp/restored_pokemon.sql"
                with open(out_path, "wb") as f:
                    f.write(sql_bytes)
                print(f"[backup] Restored remote DB backup ({len(sql_bytes)} bytes) -> {out_path}", flush=True)
                # Also restore pay JSON files
                os.makedirs(DATA_DIR, exist_ok=True)
                for jf in ("pay_orders.json", "sepay_config.json", "sepay_processed.json"):
                    jb = download_file(jf)
                    if jb:
                        with open(os.path.join(DATA_DIR, jf), "wb") as f:
                            f.write(jb)
                return True
        except Exception as e:
            print(f"[backup] Failed to decompress remote backup: {e}", flush=True)
    print("[backup] No remote backup found yet; will use bundled pokemon_dump.sql.", flush=True)
    return False


def backup_once():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        return False
    try:
        proc = subprocess.run(
            [
                "mysqldump",
                f"--socket={MYSQL_SOCK}",
                "-u", "root", "-pqweqwe",
                "--single-transaction", "--quick", "--routines",
                "--databases", "pokemon",
            ],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0 or len(proc.stdout) < 1000:
            print(f"[backup] mysqldump failed: rc={proc.returncode} err={proc.stderr[:200]}", flush=True)
            return False
        gz = gzip.compress(proc.stdout, compresslevel=1)
        files = {"pokemon_latest.sql.gz": gz}
        for jf in ("pay_orders.json", "sepay_config.json", "sepay_processed.json"):
            fp = os.path.join(DATA_DIR, jf)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    files[jf] = f.read()
        ok = upload_files(files, f"auto-backup {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        if ok:
            print(f"[backup] Uploaded DB snapshot ({len(gz)/1024:.1f} KB gz) to {HF_BACKUP_REPO}", flush=True)
        return ok
    except Exception as e:
        print(f"[backup] backup_once error: {e}", flush=True)
        return False


def backup_loop():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        return
    ensure_dataset_repo()
    interval = int(os.environ.get("BACKUP_INTERVAL_SEC", "300"))
    time.sleep(60)
    while True:
        backup_once()
        time.sleep(interval)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "restore":
        sys.exit(0 if restore() else 1)
    elif cmd == "loop":
        backup_loop()
    else:
        sys.exit(0 if backup_once() else 1)
