#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-backup & Auto-restore MariaDB database + SePay state to a Private Hugging Face Dataset.
Supports BOTH Hugging Face Git LFS files (*.gz) and regular files (*.json) using pure requests.
Configured via Environment Variables:
  HF_TOKEN       - Hugging Face Write Token
  HF_BACKUP_REPO - e.g. "username/pkm-db-backup" (private dataset)
"""
import base64
import gzip
import hashlib
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
FLUSH_FLAG = "/tmp/pkm/flush_save"
BACKUP_LOG = "/tmp/backup.log"
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}"
    print(line, flush=True)
    try:
        with open(BACKUP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
        if r.status_code in (200, 201, 409):
            return True
        r2 = requests.post(
            "https://huggingface.co/api/repos/create",
            headers=HEADERS,
            json={"type": "dataset", "name": name, "private": True},
            timeout=20,
        )
        return r2.status_code in (200, 201, 409)
    except Exception as e:
        log(f"[backup] ensure_dataset_repo warn: {e}")
        return False


def download_file(filename):
    url = f"https://huggingface.co/datasets/{HF_BACKUP_REPO}/resolve/main/{filename}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        if r.status_code == 200 and len(r.content) > 0:
            return r.content
    except Exception as e:
        log(f"[backup] download {filename} warn: {e}")
    return None


def _lfs_upload_objects(lfs_items):
    """Upload LFS objects via Hugging Face Git LFS Batch API."""
    if not lfs_items:
        return True
    batch_url = f"https://huggingface.co/datasets/{HF_BACKUP_REPO}.git/info/lfs/objects/batch"
    payload = {
        "operation": "upload",
        "transfers": ["basic"],
        "objects": [{"oid": it["oid"], "size": it["size"]} for it in lfs_items],
        "hash_algo": "sha256",
    }
    r = requests.post(
        batch_url,
        headers={
            **HEADERS,
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
        },
        json=payload,
        timeout=30,
    )
    if r.status_code != 200:
        log(f"[backup] LFS batch HTTP {r.status_code}: {r.text[:250]}")
        return False
    resp_objs = r.json().get("objects") or []
    by_oid = {it["oid"]: it for it in lfs_items}
    for obj in resp_objs:
        oid = obj.get("oid")
        if obj.get("error"):
            log(f"[backup] LFS obj error ({oid}): {obj['error']}")
            return False
        actions = obj.get("actions") or {}
        upload_act = actions.get("upload")
        if upload_act:
            raw = by_oid[oid]["bytes"]
            up_hdrs = dict(upload_act.get("header") or {})
            ur = requests.put(upload_act["href"], data=raw, headers=up_hdrs, timeout=60)
            if ur.status_code not in (200, 201, 204):
                log(f"[backup] LFS PUT failed HTTP {ur.status_code}: {ur.text[:200]}")
                return False
        verify_act = actions.get("verify")
        if verify_act:
            vhdrs = dict(verify_act.get("header") or {})
            vhdrs.setdefault("Content-Type", "application/json")
            vr = requests.post(
                verify_act["href"],
                json={"oid": oid, "size": by_oid[oid]["size"]},
                headers=vhdrs,
                timeout=30,
            )
            if vr.status_code not in (200, 201, 204):
                log(f"[backup] LFS verify warn HTTP {vr.status_code}: {vr.text[:200]}")
    return True


def upload_files(files_dict, commit_msg):
    """Commit files to HF Dataset supporting BOTH LFS (*.gz) and regular (*.json) files."""
    pre_url = f"https://huggingface.co/api/datasets/{HF_BACKUP_REPO}/preupload/main"
    pre_files = [
        {
            "path": path,
            "size": len(raw_bytes),
            "sample": base64.b64encode(raw_bytes[:512]).decode("ascii"),
        }
        for path, raw_bytes in files_dict.items()
    ]
    pr = requests.post(pre_url, headers=HEADERS, json={"files": pre_files}, timeout=30)
    if pr.status_code != 200:
        log(f"[backup] preupload failed HTTP {pr.status_code}: {pr.text[:250]}")
        return False

    mode_by_path = {}
    for item in (pr.json().get("files") or []):
        mode_by_path[item.get("path")] = item.get("uploadMode", "regular")

    lfs_items = []
    commit_lines = [json.dumps({"key": "header", "value": {"summary": commit_msg}})]
    for path, raw_bytes in files_dict.items():
        mode = mode_by_path.get(path, "lfs" if path.endswith(".gz") else "regular")
        if mode == "lfs":
            oid = hashlib.sha256(raw_bytes).hexdigest()
            size = len(raw_bytes)
            lfs_items.append({"path": path, "oid": oid, "size": size, "bytes": raw_bytes})
            commit_lines.append(json.dumps({
                "key": "lfsFile",
                "value": {"path": path, "algo": "sha256", "oid": oid, "size": size},
            }))
        else:
            commit_lines.append(json.dumps({
                "key": "file",
                "value": {
                    "content": base64.b64encode(raw_bytes).decode("ascii"),
                    "path": path,
                    "encoding": "base64",
                },
            }))

    if lfs_items and not _lfs_upload_objects(lfs_items):
        return False

    commit_url = f"https://huggingface.co/api/datasets/{HF_BACKUP_REPO}/commit/main"
    payload = "\n".join(commit_lines).encode("utf-8")
    cr = requests.post(
        commit_url,
        headers={**HEADERS, "Content-Type": "application/x-ndjson"},
        data=payload,
        timeout=60,
    )
    if cr.status_code not in (200, 201):
        log(f"[backup] commit failed HTTP {cr.status_code}: {cr.text[:250]}")
        return False
    return True


def restore():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        log("[backup] HF_TOKEN/HF_BACKUP_REPO not set; skipping remote restore.")
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
                log(f"[backup] Restored remote DB backup ({len(sql_bytes)} bytes) -> {out_path}")
                os.makedirs(DATA_DIR, exist_ok=True)
                for jf in ("pay_orders.json", "sepay_config.json", "sepay_processed.json"):
                    jb = download_file(jf)
                    if jb:
                        with open(os.path.join(DATA_DIR, jf), "wb") as f:
                            f.write(jb)
                return True
        except Exception as e:
            log(f"[backup] Failed to decompress remote backup: {e}")
    log("[backup] No remote backup found yet; will use bundled pokemon_dump.sql.")
    return False


def trigger_datastorage_flush():
    """Ask HeadlessContainer to send 'save' to DataStorageService so dirty player cache flushes to MariaDB."""
    try:
        os.makedirs(os.path.dirname(FLUSH_FLAG), exist_ok=True)
        with open(FLUSH_FLAG, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        time.sleep(2.5)
    except Exception:
        pass


def backup_once():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        log("[backup] Skipped: HF_TOKEN/HF_BACKUP_REPO not set")
        return False
    try:
        trigger_datastorage_flush()
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
            log(f"[backup] mysqldump failed: rc={proc.returncode} err={proc.stderr[:200]}")
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
            log(f"[backup] SUCCESS uploaded DB snapshot ({len(gz)/1024:.1f} KB gz) to {HF_BACKUP_REPO}")
        return ok
    except Exception as e:
        log(f"[backup] backup_once error: {e}")
        return False


def backup_loop():
    if not HF_TOKEN or not HF_BACKUP_REPO:
        return
    ensure_dataset_repo()
    interval = int(os.environ.get("BACKUP_INTERVAL_SEC", "180"))
    time.sleep(45)
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
