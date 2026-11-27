#!/bin/bash
set -e

export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export MYSQL_SOCK=/tmp/mysqld.sock
export PKM_DATA_DIR=/home/user/data
export PKM_CLIENT_ROOT=/app/pgame/client/pkm_client
export PKM_GM_ROOT=/app/pgame/Server/ManagementService/web
export PKM_READY_FILE=/tmp/pkm/ready
export PORT="${PORT:-10000}"

mkdir -p /home/user/mysqldata /home/user/data /tmp/pkm
rm -f "$PKM_READY_FILE"

on_shutdown() {
  echo "=== [SIGTERM] Saving final DB snapshot before shutdown... ==="
  python3 /app/backup_sync.py once || true
  pkill -TERM mono 2>/dev/null || true
  mysqladmin --socket=/tmp/mysqld.sock -u root -pqweqwe shutdown 2>/dev/null || true
  exit 0
}
trap on_shutdown SIGTERM SIGINT

echo "=== [0/5] Starting Unified Proxy on 0.0.0.0:${PORT} (instant health check) ==="
python3 /app/cloud_proxy.py &
PROXY_PID=$!

echo "=== [1/5] Downloading private game bundle (if not present) ==="
if [ ! -d "/app/pgame/Run" ]; then
  if [ -z "$HF_TOKEN" ] || [ -z "$HF_BACKUP_REPO" ]; then
    echo "ERROR: HF_TOKEN and HF_BACKUP_REPO env vars are required!"
    sleep 3600
    exit 1
  fi
  echo "Streaming pgame_bundle.tar.gz from private repo ${HF_BACKUP_REPO} (zero /tmp RAM usage)..."
  curl -fL --retry 3 -H "Authorization: Bearer ${HF_TOKEN}" \
    "https://huggingface.co/datasets/${HF_BACKUP_REPO}/resolve/main/pgame_bundle.tar.gz" \
    | tar -xz -C /app
  ln -sfn client /app/pgame/Client
  echo "Bundle extracted."
fi

echo "=== [2/5] Starting MariaDB (512MB-tuned, non-root UID $(id -u)) ==="
if [ ! -d "/home/user/mysqldata/mysql" ]; then
  mysql_install_db --datadir=/home/user/mysqldata --auth-root-authentication-method=normal >/tmp/pkm/mysql_init.log 2>&1
fi

mariadbd \
  --datadir=/home/user/mysqldata \
  --socket=/tmp/mysqld.sock \
  --pid-file=/tmp/mysqld.pid \
  --bind-address=127.0.0.1 \
  --port=3306 \
  --lower-case-table-names=1 \
  --character-set-server=utf8mb4 \
  --collation-server=utf8mb4_general_ci \
  --skip-character-set-client-handshake \
  --innodb-buffer-pool-size=8M \
  --innodb-log-buffer-size=1M \
  --key-buffer-size=256K \
  --aria-pagecache-buffer-size=512K \
  --tmp-table-size=2M \
  --max-heap-table-size=2M \
  --performance-schema=OFF \
  --table-open-cache=32 \
  --table-definition-cache=128 \
  --thread-cache-size=4 \
  --max-connections=20 \
  >/tmp/pkm/mariadbd.log 2>&1 &

for i in $(seq 1 30); do
  if mysqladmin --socket=/tmp/mysqld.sock ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "=== [3/5] Configuring DB user & restoring latest database ==="
mysql --socket=/tmp/mysqld.sock -u root -e "
  CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY 'qweqwe';
  ALTER USER 'root'@'localhost' IDENTIFIED BY 'qweqwe';
  ALTER USER 'root'@'127.0.0.1' IDENTIFIED BY 'qweqwe';
  GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;
  FLUSH PRIVILEGES;
" >/dev/null 2>&1 || true

for f in pay_orders.json sepay_config.json sepay_processed.json; do
  if [ ! -f "/home/user/data/$f" ] && [ -f "/app/pgame/$f" ]; then
    cp "/app/pgame/$f" "/home/user/data/$f"
  fi
done

if python3 /app/backup_sync.py restore; then
  SQL_SRC="/tmp/restored_pokemon.sql"
else
  SQL_SRC="/app/pgame/pokemon_dump.sql"
fi

TABLE_CNT=$(mysql --socket=/tmp/mysqld.sock -u root -pqweqwe -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='pokemon';" 2>/dev/null || echo 0)
if [ "${TABLE_CNT:-0}" -lt 5 ]; then
  echo "Importing SQL from $SQL_SRC ..."
  sed -E 's/CHARSET=utf8\b/CHARSET=utf8mb4/g; s/CHARSET=utf8mb3/CHARSET=utf8mb4/g; s/utf8_general_ci/utf8mb4_general_ci/g; s/utf8mb3_general_ci/utf8mb4_general_ci/g' "$SQL_SRC" \
    | mysql --socket=/tmp/mysqld.sock -u root -pqweqwe
  mysql --socket=/tmp/mysqld.sock -u root -pqweqwe pokemon -e "ALTER DATABASE pokemon CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
  echo "Database import complete."
fi
rm -f /tmp/restored_pokemon.sql 2>/dev/null || true
# [VN19_EVENTS] Clean invalid empty-content rows & enable 7-day login (1), Level-up (109), Prime First Charge 20k (124) with content=NULL
mysql --socket=/tmp/mysqld.sock -u root -pqweqwe pokemon -e "
  DELETE FROM t_activities WHERE sid=1 AND (type=101 OR content='');
  INSERT INTO t_activities (sid, type, name, sort, p0, start, end, state, content, modify)
  SELECT 1, 1, 'Dang Nhap 7 Ngay', 1, 0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 0, NULL, 0
  FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM t_activities WHERE sid=1 AND type=1);
  INSERT INTO t_activities (sid, type, name, sort, p0, start, end, state, content, modify)
  SELECT 1, 109, 'Qua Thang Cap', 2, 0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 0, NULL, 0
  FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM t_activities WHERE sid=1 AND type=109);
  INSERT INTO t_activities (sid, type, name, sort, p0, start, end, state, content, modify)
  SELECT 1, 124, 'Nap Dau Chi Ton', 3, 0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 0, NULL, 0
  FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM t_activities WHERE sid=1 AND type=124);
" >/dev/null 2>&1 || true

echo "=== [4/5] Compiling & Launching HeadlessServiceManager (zero WinForms/Xvfb, saves 120MB RAM) ==="
export MONO_GC_PARAMS="nursery-size=1m,soft-heap-limit=180m"
rm -f /tmp/lsc.log /tmp/*Service.log 2>/dev/null || true
cd /app/pgame/Run
mcs -r:EasyGame.Core.dll -r:LocalServiceContainer.exe -r:ServiceManage.Interface.dll -r:System.Xml.dll -r:System.Core.dll -out:HeadlessContainer.exe /app/HeadlessContainer.cs >>/tmp/lsc.log 2>&1
mono HeadlessContainer.exe Pokemon >>/tmp/lsc.log 2>&1 &

for i in $(seq 1 180); do
  if curl -sS -m 2 http://127.0.0.1:18000/login/servers >/dev/null 2>&1 && \
     ss -lnt 2>/dev/null | grep -q ":18001 " && \
     ss -lnt 2>/dev/null | grep -q ":18002 "; then
    echo ">>> ALL GAME SERVER SERVICES READY AFTER ${i}s!"
    touch "$PKM_READY_FILE"
    break
  fi
  sleep 1
done

echo "=== [5/5] Auto-Backup handled periodically by cloud_proxy (zero idle RAM overhead) ==="
wait $PROXY_PID
