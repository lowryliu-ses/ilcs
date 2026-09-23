#!/usr/bin/env bash
# 为正式 ILCS 生成数据库 + 文件一致备份，并立即恢复到隔离数据库做可启动性核对。
#
# 用法（部署机）：
#   sudo bash /opt/ilcs/scripts/backup-and-recovery-drill.sh
#
# 脚本只短暂停止 api/executor 取得一致快照；备份完成后立即恢复服务，恢复演练在独立
# ilcs_restore_* 数据库和 /opt/ilcs/data/recovery-drill-* 目录进行，不改正式库。
set -euo pipefail
umask 077

ROOT=${ILCS_ROOT:-/opt/ilcs}
BACKUP_ROOT=${ILCS_BACKUP_ROOT:-/opt/ilcs-backup}
STAMP=$(date +%Y%m%d-%H%M%S)
DRILL_DB="ilcs_restore_${STAMP//-/}"
DRILL_NAME="recovery-drill-$STAMP"
DRILL_DIR="$ROOT/data/$DRILL_NAME"
BACKUP_DIR="$BACKUP_ROOT/backup-$STAMP"
REPORT="$BACKUP_DIR/recovery-report.md"
STOPPED=0
DRILL_CREATED=0
SERVICES_TO_RESTART=()

case "$ROOT" in
  ""|"/"|"/opt") echo "ILCS_ROOT 过于宽泛，拒绝执行：$ROOT" >&2; exit 2 ;;
esac
case "$BACKUP_ROOT" in
  ""|"/"|"/opt") echo "ILCS_BACKUP_ROOT 过于宽泛，拒绝执行：$BACKUP_ROOT" >&2; exit 2 ;;
esac

if [ ! -f "$ROOT/deploy/docker-compose.yml" ]; then
  echo "找不到部署文件：$ROOT/deploy/docker-compose.yml" >&2
  exit 2
fi
ROOT=$(cd "$ROOT" && pwd -P)
case "$ROOT" in
  ""|"/"|"/opt") echo "ILCS_ROOT 解析后过于宽泛，拒绝执行：$ROOT" >&2; exit 2 ;;
esac

mkdir -p "$BACKUP_ROOT"
BACKUP_ROOT=$(cd "$BACKUP_ROOT" && pwd -P)
case "$BACKUP_ROOT" in
  ""|"/"|"/opt") echo "ILCS_BACKUP_ROOT 解析后过于宽泛，拒绝执行：$BACKUP_ROOT" >&2; exit 2 ;;
esac
exec 9>"$BACKUP_ROOT/.ilcs-backup.lock"
if ! flock -n 9; then
  echo "已有 ILCS 备份/恢复演练在运行" >&2
  exit 3
fi

cleanup() {
  set +e
  if [ "$DRILL_CREATED" = "1" ]; then
    cd "$ROOT/deploy" && docker compose exec -T \
      -e ILCS_DRILL_DB="$DRILL_DB" db sh -ec \
      'dropdb -U "$POSTGRES_USER" --if-exists "$ILCS_DRILL_DB"'
  fi
  if [[ "$DRILL_DIR" == "$ROOT/data/recovery-drill-"* ]] && [ -d "$DRILL_DIR" ]; then
    rm -rf -- "$DRILL_DIR"
  fi
  if [ "$STOPPED" = "1" ] && [ "${#SERVICES_TO_RESTART[@]}" -gt 0 ]; then
    cd "$ROOT/deploy" && docker compose start "${SERVICES_TO_RESTART[@]}"
  fi
}
trap cleanup EXIT INT TERM

mkdir -p "$BACKUP_DIR" "$DRILL_DIR/files"
cd "$ROOT/deploy"

echo "==> 记录服务状态并冻结写入"
if docker compose ps --services --filter status=running api | grep -Fxq api; then
  SERVICES_TO_RESTART+=(api)
fi
if docker compose ps --services --filter status=running executor | grep -Fxq executor; then
  SERVICES_TO_RESTART+=(executor)
fi
if [ "${#SERVICES_TO_RESTART[@]}" -gt 0 ]; then
  docker compose stop "${SERVICES_TO_RESTART[@]}"
  STOPPED=1
else
  echo "api/executor 原本均未运行；保持停机状态"
fi

echo "==> 导出 PostgreSQL 一致快照"
docker compose exec -T db sh -ec \
  'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$BACKUP_DIR/database.dump"

echo "==> 归档受控文件并生成逐文件摘要"
if [ -d "$ROOT/data/files" ]; then
  (cd "$ROOT/data/files" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) \
    > "$BACKUP_DIR/files.sha256"
  tar -C "$ROOT/data/files" -czf "$BACKUP_DIR/files.tar.gz" .
else
  : > "$BACKUP_DIR/files.sha256"
  tar -C "$DRILL_DIR/files" -czf "$BACKUP_DIR/files.tar.gz" .
fi
(cd "$BACKUP_DIR" && sha256sum database.dump files.tar.gz files.sha256 > artifacts.sha256)

echo "==> 一致快照完成，恢复原先运行的服务"
if [ "${#SERVICES_TO_RESTART[@]}" -gt 0 ]; then
  docker compose start "${SERVICES_TO_RESTART[@]}"
  STOPPED=0
fi

echo "==> 恢复前校验备份制品摘要"
(cd "$BACKUP_DIR" && sha256sum -c artifacts.sha256)

echo "==> 在隔离数据库恢复备份"
docker compose exec -T -e ILCS_DRILL_DB="$DRILL_DB" db sh -ec \
  'createdb -U "$POSTGRES_USER" "$ILCS_DRILL_DB"'
DRILL_CREATED=1
docker compose exec -T -e ILCS_DRILL_DB="$DRILL_DB" db sh -ec \
  'pg_restore -U "$POSTGRES_USER" -d "$ILCS_DRILL_DB" --exit-on-error' \
  < "$BACKUP_DIR/database.dump"

echo "==> 恢复并逐文件校验附件"
tar -C "$DRILL_DIR/files" -xzf "$BACKUP_DIR/files.tar.gz"
(cd "$DRILL_DIR/files" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) \
  > "$DRILL_DIR/files.sha256"
diff -u "$BACKUP_DIR/files.sha256" "$DRILL_DIR/files.sha256"

echo "==> 用当前应用版本核对恢复库"
{
  echo "# ILCS 备份恢复演练"
  echo
  echo "- 时间：$STAMP"
  echo "- 隔离恢复库：$DRILL_DB"
  echo "- 数据库备份：database.dump"
  echo "- 文件备份：files.tar.gz"
  echo "- 快照前运行且已恢复的服务：${SERVICES_TO_RESTART[*]:-无}"
  echo
  echo '```text'
} > "$REPORT"
docker compose run --rm \
  -e ILCS_DRILL_DB="$DRILL_DB" \
  -e ILCS_FILE_ROOT="/data/$DRILL_NAME/files" \
  migrate sh -ec '
    export ILCS_DATABASE_URL="${ILCS_DATABASE_URL%/*}/$ILCS_DRILL_DB"
    python scripts/migrate.py status
    python scripts/migrate.py verify
  ' | tee -a "$REPORT"
echo '```' >> "$REPORT"
echo >> "$REPORT"
echo "结论：数据库版本、业务核对与文件摘要均通过。" >> "$REPORT"

echo "完成：$BACKUP_DIR"
echo "恢复演练报告：$REPORT"
