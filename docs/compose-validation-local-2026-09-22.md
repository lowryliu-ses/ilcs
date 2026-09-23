# Compose 本地完整部署验证记录

- 日期：2026-09-22
- 范围：镜像构建、Compose 展开、PostgreSQL 16、迁移、正式主数据初始化、API、执行器、nginx
- 数据隔离：独立临时目录、动态回环端口、独立 Compose 项目与 PG 卷

## 验证结果

1. `deploy/Dockerfile` 成功构建；镜像未包含测试数据库或 `api/tests`。
2. API、执行器和迁移任务以固定非特权用户 `ilcs`（UID/GID 10001）运行。
3. API、执行器和 nginx 根文件系统为只读，运行目录使用独立 tmpfs，
   `CapDrop=["ALL"]`，启用 `no-new-privileges`；nginx 以 UID/GID 101 监听容器内
   非特权端口 8080。
4. 网络分为 `frontend` 与 `backend`：nginx 不连接数据库网络，PostgreSQL 不连接前端
   网络，只有 API 同时连接两侧。
5. PostgreSQL 16 健康后，从空库依次执行迁移 `0001` 至 `0007_account_lifecycle`；历史映射
   `0003` 正确识别“无历史业务数据”，只推进版本，不创建占位组织、实验室或指标。
6. `seed --force --master-only` 精确创建 1 个组织、1 个默认实验室、1 个管理员和 1 条成员
   关系；服务身份、工位、批号、方法、指标等演示业务行均为 0。
7. API 健康检查返回 HTTP 200、`status=ok`，当前与期待迁移版本均为
   `0007_account_lifecycle`。
8. nginx 回环入口可登录管理员临时账号，返回令牌且 `must_change_password=true`。
9. nginx 响应包含 CSP、`X-Frame-Options: DENY`、COOP/CORP 等浏览器安全头；HSTS
   留给实际终止 TLS 的上游 HTTPS 网关配置。
10. API、executor、web、db 四个容器均运行；验证结束后容器、网络与 PG 卷全部删除。
11. 在完成并记录正式最小初始化断言后，仅向隔离临时库补入演示主数据，真实走完方案审批、
    任务分配、批次排程、人工/设备/等待/审核四类节点、LIMS 回传、逐条复核、报告审签发布、
    PDF 摘要校验和审计链；联调等待节点为 3 秒，由独立执行器到期唤醒。

## 复现

```bash
cd ilcs
bash scripts/validate-compose-stack.sh
```

脚本不读取正式 `deploy/.env`，不挂载仓库 `data/` 或 `secrets/`，也不连接目标服务器。
它证明当前制品可完成一套隔离首次部署，但不能替代目标机的端口、磁盘、权限、TLS、真实数据
和设备网络验证。
