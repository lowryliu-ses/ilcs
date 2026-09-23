#!/usr/bin/env bash
# 把演示环境重置成「种子主数据 + 试点操作案例跑完的结果」。
#
# 与 reset-demo.sh 相同的换库做法（备份 → 空库 → 迁移 → 播种），之后：
# - 删掉种子里的演示方法与方案（R-205 除外）和演示报警；
# - 把 ST-06/ST-07 接回两台外部 SiLA 2 模拟设备，等执行器探测在线；
# - 用 load-pilot-case.py 按 docs/试点操作案例.md 真实跑一遍（签名由脚本代签并在审计中注明）。
#
# 用法（部署机上）：  sudo bash /opt/ilcs/scripts/reset-pilot-case.sh
# 本地 Docker：       ILCS_ROOT=$PWD ILCS_BACKUP_ROOT=$PWD/data/backup bash scripts/reset-pilot-case.sh
#
# 恢复：停掉 api/executor，把 pre-pilot-case-<时间戳>/ilcs.dump 用 pg_restore 恢复到空库，
#       并把同目录的 files/ 恢复为 data/files/，再启动。
set -euo pipefail

ROOT=${ILCS_ROOT:-/opt/ilcs}
BACKUP_ROOT=${ILCS_BACKUP_ROOT:-/opt/ilcs-backup}
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_ROOT/pre-pilot-case-$STAMP"

cd "$ROOT/deploy"

echo "==> 停 api / executor，冻结写入"
docker compose stop api executor

echo "==> 备份 PostgreSQL 与附件 → $BACKUP"
mkdir -p "$BACKUP"
docker compose exec -T db sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP/ilcs.dump"
if [ -d "$ROOT/data/files" ]; then
  cp -a "$ROOT/data/files" "$BACKUP/files"
fi
ls -l "$BACKUP"

echo "==> 重建空库"
docker compose exec -T db sh -ec \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
if [ -d "$ROOT/data/files" ]; then
  mv "$ROOT/data/files" "$BACKUP/retired-files"
  mkdir -p "$ROOT/data/files"
  chmod 777 "$ROOT/data/files"
fi

echo "==> 建结构并播种主数据"
docker compose run --rm migrate
docker compose run --rm migrate python scripts/migrate.py seed --force

echo "==> 启 api / executor 与 SiLA 2 模拟设备"
docker compose start api executor
docker compose --profile pilot up -d sila-sim-lh sila-sim-cycler

echo "==> 等 API 就绪"
for _ in $(seq 1 60); do
  if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" 2>/dev/null; then break; fi
  sleep 2
done

echo "==> ST-06 / ST-07 接回 SiLA 2 模拟设备"
docker compose exec -T api python ../scripts/configure-pilot-adapters.py apply \
  --station ST-06=sila-sim-lh:50052:SIM-LH-01 \
  --station ST-07=sila-sim-cycler:50053:SIM-CYC-01 --channels ST-07=8

echo "==> 等执行器探测在线"
docker compose exec -T api python - <<'PY'
import json, time, urllib.request
for _ in range(40):
    gate = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/gate"))
    blocked = {k for k in gate.get("blocked_stations") or {} if k in {"ST-06", "ST-07"}}
    if gate["open"] and not blocked:
        print("  在线"); break
    time.sleep(3)
else:
    raise SystemExit(f"SiLA 设备未上线：{gate}")
PY

echo "==> 导入试点案例"
docker compose exec -T api python ../scripts/load-pilot-case.py --prune-demo

echo
echo "完成。备份在 $BACKUP/。"
