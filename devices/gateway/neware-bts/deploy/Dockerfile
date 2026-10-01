# 模拟的 Neware 网关（假 BTS）镜像：只带网关 SDK 与本模块，不带 ILCS、不带 aurora-neware。构建上下文是 ILCS 仓库根目录：
#   docker build -f devices/gateway/neware-bts/deploy/Dockerfile -t ilcs-neware-bts:latest .
# 只用来跑 --simulate：接真机时网关要和 BTS 在同一台 Windows 机器上（工步文件由 BTS 读），见 windows-service.md。
FROM python:3.11-slim

# 和 ILCS 容器同一个 UID：证书、令牌写进共享的凭据目录，ILCS 按属主只读的文件读得到
ARG GATEWAY_UID=10001
RUN groupadd --gid "$GATEWAY_UID" gateway \
    && useradd --uid "$GATEWAY_UID" --gid "$GATEWAY_UID" --no-create-home --shell /usr/sbin/nologin gateway \
    && mkdir -p /var/lib/ilcs-gateway /run/secrets/gateway \
    && chown gateway:gateway /var/lib/ilcs-gateway /run/secrets/gateway
# 自签证书要 cryptography
RUN pip install --no-cache-dir cryptography==50.0.1

WORKDIR /opt/gateway
COPY --chown=gateway:gateway devices/gateway/ilcs_gateway /opt/gateway/sdk/ilcs_gateway
COPY --chown=gateway:gateway devices/gateway/neware-bts /opt/gateway/module

USER gateway
WORKDIR /opt/gateway/module
# 台账（/var/lib/ilcs-gateway）与证书、令牌（/run/secrets/gateway）都要挂持久卷
ENV PYTHONPATH=/opt/gateway/sdk GATEWAY_STATE_DIR=/var/lib/ilcs-gateway GATEWAY_SECRETS=/run/secrets/gateway \
    GATEWAY_SIMULATE=1 GATEWAY_CONFIG=/opt/gateway/module/simulator/neware-sim.json
CMD ["python", "gateway.py"]
