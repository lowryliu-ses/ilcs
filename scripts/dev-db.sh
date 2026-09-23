#!/usr/bin/env bash
# 本机开发与测试用的 PostgreSQL 16（与正式 Compose 同版本）。
#
# ILCS 只支持 PostgreSQL：行锁、SKIP LOCKED、advisory lock、部分唯一索引与精确小数都依赖它，
# 开发与测试也必须跑在同一种库上。本脚本幂等：已在运行就直接返回，停着就启动，没有就新建。
#
#   开发库：postgresql+psycopg2://ilcs:ilcs-dev@127.0.0.1:55432/ilcs      （应用默认连接）
#   测试库：postgresql+psycopg2://ilcs:ilcs-dev@127.0.0.1:55432/ilcs_test （pytest 默认连接）
#
# 数据放在 Docker 卷 ilcs_dev_pg；彻底清空：docker rm -f ilcs-dev-pg && docker volume rm ilcs_dev_pg
set -euo pipefail

NAME=${ILCS_DEV_DB_CONTAINER:-ilcs-dev-pg}
PORT=${ILCS_DEV_DB_PORT:-55432}
IMAGE=postgres:16-alpine

command -v docker >/dev/null 2>&1 || { echo "需要 Docker" >&2; exit 2; }

state=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || true)
case "$state" in
  running) ;;
  exited|created) docker start "$NAME" >/dev/null ;;
  "")
    docker run -d --name "$NAME" --restart unless-stopped \
      -e POSTGRES_USER=ilcs -e POSTGRES_PASSWORD=ilcs-dev -e POSTGRES_DB=ilcs \
      -p "127.0.0.1:$PORT:5432" -v ilcs_dev_pg:/var/lib/postgresql/data \
      "$IMAGE" >/dev/null
    ;;
  *) echo "容器 $NAME 状态异常：$state" >&2; exit 1 ;;
esac

for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U ilcs -d ilcs >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -U ilcs -d ilcs >/dev/null

# 测试库单独一个：pytest 每次会话会 DROP 它的 public schema，不能和开发库共用
if ! docker exec "$NAME" psql -U ilcs -d ilcs -Atc "SELECT 1 FROM pg_database WHERE datname = 'ilcs_test'" | grep -q 1; then
  docker exec "$NAME" createdb -U ilcs ilcs_test
fi

echo "PostgreSQL 就绪：127.0.0.1:$PORT（开发库 ilcs，测试库 ilcs_test）"
