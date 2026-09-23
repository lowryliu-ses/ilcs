#!/usr/bin/env bash
# 把部署环境重置成「种子主数据 + 一条跑完整流程的批次」。
#
# 为什么要有这个脚本：演示环境用久了会堆下点测残留——重复的修订草稿、被点开又没锁回去
# 的实验矩阵、历次冒烟留下的半截批次。这些在界面上和真实记录长得一模一样，看的人分不清。
# 种子只建主数据（组织/用户/人员资质/设备/能力/工位/方法/方案/物料/指标/SOP/服务身份），
# 不建任何批次，所以「换一个空库再迁移播种」就是最干净的基线，比逐表 DELETE 少踩很多
# 外键顺序的坑。
#
# 建库与播种都是这个脚本的事，不是应用启动的事：api 启动只校验库版本。所以换库之后必须
# 先跑 migrate，否则 api 会以 schema_mismatch 起不来——这是刻意的，不是故障。
#
# 用法（在部署机上）：
#   sudo bash /opt/ilcs/scripts/reset-demo.sh                 # 重置并跑一轮完整流程
#   sudo bash /opt/ilcs/scripts/reset-demo.sh --keep-data     # 只跑流程，不动现有数据
#
# 恢复：停掉 api/executor，把 pg-dump-<时间戳>.sql 恢复到空 PG 库，并把
#       files-<时间戳>/ 恢复为 /opt/ilcs/data/files/，再启动。
set -euo pipefail

ROOT=${ILCS_ROOT:-/opt/ilcs}
BACKUP_ROOT=${ILCS_BACKUP_ROOT:-/opt/ilcs-backup}
BASE_URL=${ILCS_BASE_URL:-http://127.0.0.1:8090}
BATCH_NOTE=${ILCS_BATCH_NOTE:-首轮完整流程验证}
KEEP_DATA=${1:-}
STAMP=$(date +%Y%m%d-%H%M%S)

cd "$ROOT/deploy"

if [ "$KEEP_DATA" != "--keep-data" ]; then
  echo "==> 停 api / executor，冻结演示数据写入"
  docker compose stop api executor

  echo "==> 逻辑备份 PostgreSQL 与附件"
  mkdir -p "$BACKUP_ROOT"
  docker compose exec -T db sh -ec 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    > "$BACKUP_ROOT/pg-dump-$STAMP.sql"
  if [ -d "$ROOT/data/files" ]; then
    cp -a "$ROOT/data/files" "$BACKUP_ROOT/files-$STAMP"
  fi
  echo "    $BACKUP_ROOT/pg-dump-$STAMP.sql"

  echo "==> 重建空的 PostgreSQL 演示库"
  docker compose exec -T db sh -ec \
    'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
  # 数据库内的文件引用将重新播种，旧附件不能留在新库旁边冒充有效文件。
  if [ -d "$ROOT/data/files" ]; then
    mv "$ROOT/data/files" "$BACKUP_ROOT/retired-files-$STAMP"
  fi

  echo "==> 建结构（空库从头建）"
  docker compose run --rm migrate
  echo "==> 播种主数据"
  docker compose run --rm migrate python scripts/migrate.py seed --force

  echo "==> 启 api / executor"
  docker compose start api executor
fi

echo "==> 等 API 就绪（库版本不匹配时 health 会说 schema_mismatch）"
for _ in $(seq 1 30); do
  if curl -fsS "$BASE_URL/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "$BASE_URL/api/health"; echo

echo "==> 跑一轮首期完整流程：方案审批 → 任务 → 批次 → 四类节点 → 检测回传 → 数据复核 → 报告发布"
# 用宿主机的 python3 打 8090：smoke.py 只走 HTTP
ILCS_BATCH_NOTE="$BATCH_NOTE" python3 "$ROOT/scripts/smoke.py" "$BASE_URL"

echo
echo "完成。备份在 $BACKUP_ROOT/。回退需恢复 pg-dump-$STAMP.sql 与对应 files-$STAMP/。"
