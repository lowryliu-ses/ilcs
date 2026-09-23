#!/usr/bin/env bash
# 在隔离的本地 Docker 项目中验证正式 Compose 交付链路；不读取 deploy/.env，
# 不挂载仓库 data/secrets，也不连接目标服务器。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"
EXAMPLE_ENV="$ROOT/deploy/.env.example"
IMAGE=${ILCS_VALIDATION_IMAGE:-ilcs-api:validation}
PROJECT="ilcs-validation-$$"
DB_CONTAINER="$PROJECT-db"
API_CONTAINER="$PROJECT-api"
EXECUTOR_CONTAINER="$PROJECT-executor"
WEB_CONTAINER="$PROJECT-web"
VALIDATION_PASSWORD=${ILCS_VALIDATION_PASSWORD:-ValidationPass2026!}

for command in docker curl python3 awk sed; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "缺少验证命令：$command" >&2
    exit 2
  }
done
docker compose version >/dev/null
test -f "$COMPOSE_FILE" && test -f "$EXAMPLE_ENV"
# Compose 配置从 stdin 读取时，相对 env_file/build/volume 路径以当前目录解释。
cd "$ROOT/deploy"

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ilcs-stack-validation.XXXXXX")
DATA_DIR="$TMP_ROOT/data"
SECRETS_DIR="$TMP_ROOT/secrets"
mkdir -p "$DATA_DIR" "$SECRETS_DIR"
chmod 0777 "$DATA_DIR"
chmod 0755 "$SECRETS_DIR"

# 端口只在回环地址上短暂暴露。获取端口与启动之间仍可能有极小竞争，失败时脚本明确退出。
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

compose_config() {
  sed \
    -e "s/container_name: ilcs-db/container_name: $DB_CONTAINER/" \
    -e "s/container_name: ilcs-api/container_name: $API_CONTAINER/" \
    -e "s/container_name: ilcs-executor/container_name: $EXECUTOR_CONTAINER/" \
    -e "s/container_name: ilcs-web/container_name: $WEB_CONTAINER/" \
    -e "s#../data:/data#$DATA_DIR:/data#g" \
    -e "s#../secrets:/run/secrets/ilcs:ro#$SECRETS_DIR:/run/secrets/ilcs:ro#g" \
    -e "s#\"\${ILCS_WEB_BIND:-127.0.0.1}:8090:8080\"#\"127.0.0.1:$PORT:8080\"#" \
    -e "s#image: ilcs-api:latest#image: $IMAGE#g" \
    -e 's/env_file: .env/env_file: .env.example/g' \
    "$COMPOSE_FILE" | awk -v password="$VALIDATION_PASSWORD" '
      { print }
      /env_file: .env.example/ {
        print "    environment:"
        print "      ILCS_ENVIRONMENT: development"
        # 隔离栈只有模拟工位：显式打开模拟心跳。正式样例配置里它是 0，正式环境不允许打开
        print "      ILCS_EXECUTOR_SIMULATE_HEARTBEAT: \"1\""
        print "      ILCS_INITIAL_PASSWORD: " password
      }
    '
}

cleanup() {
  set +e
  compose_config | docker compose -p "$PROJECT" -f - down -v >/dev/null 2>&1
  case "$TMP_ROOT" in
    */ilcs-stack-validation.*) rm -rf -- "$TMP_ROOT" ;;
  esac
}
trap cleanup EXIT INT TERM

if [ "${ILCS_SKIP_BUILD:-0}" != "1" ]; then
  docker build -f "$ROOT/deploy/Dockerfile" -t "$IMAGE" "$ROOT"
fi

compose_config | docker compose -p "$PROJECT" -f - config --quiet
compose_config | docker compose -p "$PROJECT" -f - up -d --no-build db
db_ready=0
db_ready_streak=0
for _ in $(seq 1 60); do
  if docker exec "$DB_CONTAINER" pg_isready -U ilcs -d ilcs >/dev/null 2>&1; then
    db_ready_streak=$((db_ready_streak + 1))
    if [ "$db_ready_streak" -ge 3 ]; then
      db_ready=1
      break
    fi
  else
    # initdb 会短暂启动再关闭临时服务器；一次成功不能视为稳定就绪。
    db_ready_streak=0
  fi
  sleep 1
done
if [ "$db_ready" != "1" ]; then
  echo "PostgreSQL 在 60 秒内未就绪" >&2
  docker logs "$DB_CONTAINER" >&2 || true
  exit 1
fi
docker exec "$DB_CONTAINER" pg_isready -U ilcs -d ilcs

compose_config | docker compose -p "$PROJECT" --profile tools -f - run --rm migrate
compose_config | docker compose -p "$PROJECT" --profile tools -f - run --rm migrate \
  python scripts/migrate.py seed --force --master-only
bootstrap_counts=$(docker exec "$DB_CONTAINER" psql -U ilcs -d ilcs -Atc \
  "SELECT (SELECT count(*) FROM organizations)||','||
          (SELECT count(*) FROM labs)||','||
          (SELECT count(*) FROM users)||','||
          (SELECT count(*) FROM memberships)||','||
          (SELECT count(*) FROM service_identities)||','||
          (SELECT count(*) FROM stations)||','||
          (SELECT count(*) FROM lots)||','||
          (SELECT count(*) FROM recipes)||','||
          (SELECT count(*) FROM metric_definitions)")
test "$bootstrap_counts" = "1,1,1,1,0,0,0,0,0"
echo "production_bootstrap=org:1 lab:1 admin:1 demo_business_rows:0"
compose_config | docker compose -p "$PROJECT" -f - up -d --no-build api executor web

health=""
for _ in $(seq 1 45); do
  health=$(curl -sS "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)
  if printf '%s' "$health" | grep -q '"status":"ok"'; then
    break
  fi
  sleep 1
done
printf 'health=%s\n' "$health"
printf '%s' "$health" | grep -q '"status":"ok"'

headers=$(curl -sSI "http://127.0.0.1:$PORT/")
printf '%s' "$headers" | grep -qi '^Content-Security-Policy:'
printf '%s' "$headers" | grep -qi '^X-Frame-Options: DENY'
printf '%s' "$headers" | grep -qi '^Cross-Origin-Opener-Policy: same-origin'
echo "browser_security_headers=ok"

login=$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$VALIDATION_PASSWORD\"}" \
  "http://127.0.0.1:$PORT/api/auth/login")
printf '%s' "$login" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload.get("access_token")
assert payload.get("user", {}).get("must_change_password") is True
print("login=ok must_change_password=true")
'

test "$(docker inspect -f '{{.Config.User}}' "$API_CONTAINER")" = "ilcs"
test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$API_CONTAINER")" = "true"
test "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$API_CONTAINER")" = '["ALL"]'
printf 'api_security=user:%s readonly:%s cap_drop:%s\n' \
  "$(docker inspect -f '{{.Config.User}}' "$API_CONTAINER")" \
  "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$API_CONTAINER")" \
  "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$API_CONTAINER")"
test "$(docker inspect -f '{{.Config.User}}' "$WEB_CONTAINER")" = "101:101"
test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$WEB_CONTAINER")" = "true"
test "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$WEB_CONTAINER")" = '["ALL"]'
printf 'web_security=user:%s readonly:%s cap_drop:%s\n' \
  "$(docker inspect -f '{{.Config.User}}' "$WEB_CONTAINER")" \
  "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$WEB_CONTAINER")" \
  "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$WEB_CONTAINER")"

web_networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' \
  "$WEB_CONTAINER")
db_networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' \
  "$DB_CONTAINER")
case "$web_networks" in
  *frontend*) ;;
  *) echo "web 未连接 frontend 网络" >&2; exit 1 ;;
esac
case "$web_networks" in
  *backend*) echo "web 不应连接 backend 网络" >&2; exit 1 ;;
esac
case "$db_networks" in
  *backend*) ;;
  *) echo "db 未连接 backend 网络" >&2; exit 1 ;;
esac
case "$db_networks" in
  *frontend*) echo "db 不应连接 frontend 网络" >&2; exit 1 ;;
esac
printf 'network_isolation=web:%s db:%s\n' "$web_networks" "$db_networks"

web_health=""
for _ in $(seq 1 30); do
  web_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$WEB_CONTAINER")
  if [ "$web_health" = "healthy" ]; then
    break
  fi
  sleep 1
done
if [ "$web_health" != "healthy" ]; then
  echo "nginx 健康检查未通过：${web_health:-missing}" >&2
  docker logs "$WEB_CONTAINER" >&2 || true
  exit 1
fi

compose_config | docker compose -p "$PROJECT" -f - ps

# 正式最小初始化已在上面按精确行数验收。隔离库没有生产数据，接着补入演示主数据，
# 用同一套 PG/API/executor/nginx 制品跑真正的首期业务闭环；不以“到等待节点为止”冒充完成。
compose_config | docker compose -p "$PROJECT" --profile tools -f - run --rm migrate \
  python scripts/migrate.py seed --force
ILCS_BATCH_NOTE="隔离 PostgreSQL 全链路验收 $PROJECT" \
  python3 "$ROOT/scripts/smoke.py" "http://127.0.0.1:$PORT"

echo "Compose 全栈与端到端业务闭环验证通过；退出时将清理项目 $PROJECT"
