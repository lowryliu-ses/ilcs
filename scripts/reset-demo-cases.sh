#!/usr/bin/env bash
# 把演示环境重置成「种子主数据 + 四个跑完的参考案例」（见 docs/操作案例.md）。
#
# 做法与 reset-demo.sh 相同（备份 → 空库 → 迁移 → 播种），之后：
# - 删掉种子里的全部演示流程、方案与报警：案例只保留主数据，流程与方案由案例自己建；
# - 按设备仓库（ilcs-devices）的 simulators/pilot-devices.json 把 ST-01-A、ST-06（SiLA 2）、ST-07（HTTPS 网关）接到
#   外部模拟设备（模拟设备由设备仓库的 deploy/compose.yml 起），
#   等执行器探测在线；其余示例工位（含 AGV、机械臂 ARM-01）用内置模拟——PLC 点表、车队 REST、串口命令的驱动已移出
#   ILCS，要接这类模拟设备在驱动宿主（ilcs-devices/host）里登记；
# - 用 load-demo-cases.py 走和界面相同的 HTTP 接口把四个案例真实跑一遍：
#     案例 A 注液（ST-05 → ST-06），案例 B 循环测试（ST-07），
#     案例 C 注液 → 循环测试串行（托盘由 AGV 在工位间转运，注液时手套箱机械臂协同上下料），
#     案例 D 分批（20 个扣电按每批 8 位拆成 3 个子任务、3 个批次，父任务出一份合并报告）。
#   签名用演示账号口令逐次签署，与在界面上签的一样。
#
# 用法（部署机上）：  ILCS_BASE_URL=http://10.10.106.51:8090 bash /opt/ilcs/scripts/reset-demo-cases.sh
#                     （BASE_URL 是 nginx 实际监听的地址；106.51 上只绑在 10.10.106.51:8090）
# 本地 Docker：       ILCS_ROOT=$PWD ILCS_BACKUP_ROOT=$PWD/data/backup bash scripts/reset-demo-cases.sh
# 设备仓库缺省在 ILCS 旁边（$ILCS_ROOT/../ilcs-devices，部署机上是 /opt/ilcs-devices），别处用 ILCS_DEVICES 指过去。
# 只导案例、不换库：  bash scripts/reset-demo-cases.sh --keep-data   （库里已有同名案例时会重复建一套）
#
# 恢复：停掉 api/executor，把 pre-demo-cases-<时间戳>/ilcs.dump 用 pg_restore 恢复到空库，
#       并把同目录的 files/ 恢复为 data/files/，再启动。
set -euo pipefail

ROOT=${ILCS_ROOT:-/opt/ilcs}
DEVICES=${ILCS_DEVICES:-$ROOT/../ilcs-devices}
PRESET="$DEVICES/simulators/pilot-devices.json"
BACKUP_ROOT=${ILCS_BACKUP_ROOT:-/opt/ilcs-backup}
BASE_URL=${ILCS_BASE_URL:-http://127.0.0.1:8090}
KEEP_DATA=${1:-}
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_ROOT/pre-demo-cases-$STAMP"

[ -f "$PRESET" ] || { echo "找不到设备仓库的试点预设 $PRESET：设 ILCS_DEVICES 指向 ilcs-devices 的检出" >&2; exit 1; }
# 试点工位（ARM-01 不在 compose 里，库里有就用内置模拟）
PILOT_STATIONS=$(python3 -c 'import json, sys; print(",".join(sorted(json.load(open(sys.argv[1], encoding="utf-8"))["stations"])))' "$PRESET")

cd "$ROOT/deploy"

if [ "$KEEP_DATA" != "--keep-data" ]; then
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
  # 旧附件属于旧库里的记录，不能留在新库旁边冒充有效文件
  if [ -d "$ROOT/data/files" ]; then
    mv "$ROOT/data/files" "$BACKUP/retired-files"
    mkdir -p "$ROOT/data/files"
    chmod 777 "$ROOT/data/files"
  fi

  echo "==> 建结构并播种主数据"
  docker compose run --rm migrate
  docker compose run --rm migrate python scripts/migrate.py seed --force

  echo "==> 删掉种子里的演示流程、方案与报警（只留主数据）"
  docker compose run --rm -T migrate python - <<'PY'
import sys
sys.path.insert(0, "/opt/ilcs/api")
from app.core.db import SessionLocal
from app.models import Alarm, Batch, Plan, PlanVersion, Recipe

with SessionLocal() as db:
    if db.query(Batch).count():
        raise SystemExit("库里已有批次：只在刚播种的空库上清理演示设计数据")
    versions = db.query(PlanVersion).delete()
    plans = db.query(Plan).delete()
    recipes = db.query(Recipe).delete()
    alarms = db.query(Alarm).delete()
    db.commit()
print(f"  已删除：方案 {plans}（版本 {versions}）、流程 {recipes}、演示报警 {alarms}")
PY

  echo "==> 启 api / executor 与模拟设备（镜像更新过就按新镜像重建容器；模拟设备在设备仓库里起）"
  docker compose up -d
  docker compose -f "$DEVICES/deploy/sim.yml" --profile pilot up -d --build

  echo "==> 等 API 就绪"
  for _ in $(seq 1 60); do
    if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" 2>/dev/null; then break; fi
    sleep 2
  done

  echo "==> 全部示例工位按预设接到外部模拟设备（$PRESET）"
  # api 容器里没有设备仓库：预设从标准输入喂进去
  docker compose exec -T api python ../scripts/configure-pilot-adapters.py apply --preset - --skip-missing < "$PRESET"
fi

echo "==> 等执行器探测到外部模拟设备在线"
docker compose exec -T -e PILOT_STATIONS="$PILOT_STATIONS" api python - <<'PY'
import json, os, time, urllib.request
stations = set(os.environ["PILOT_STATIONS"].split(",")) - {"ARM-01"}
for _ in range(60):
    gate = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/gate"))
    blocked = {k for k in gate.get("blocked_stations") or {} if k in stations}
    if gate["open"] and not blocked:
        print("  在线"); break
    time.sleep(3)
else:
    raise SystemExit(f"外部模拟设备未全部上线：{sorted(blocked)} {gate['reasons']}")
PY

echo "==> 导入四个参考案例（约 12 分钟：设备按模拟时长真实执行）"
# 只走 HTTP：宿主机的 python3 打 nginx 端口即可
python3 "$ROOT/scripts/load-demo-cases.py" "$BASE_URL" --pilot-devices="$PRESET"

echo
if [ "$KEEP_DATA" != "--keep-data" ]; then
  echo "完成。备份在 $BACKUP/。"
else
  echo "完成。"
fi
