# 设备模块镜像：只带 SDK 与本模块，不带 ILCS。构建上下文是 ILCS 仓库根目录：
#   docker build -f devices/gateway/sample-cycler/deploy/Dockerfile -t ilcs-module-sample-cycler .
# 厂家 SDK 只有 Windows DLL 时不走容器：见 windows-service.md。
FROM python:3.11-slim

ARG GATEWAY_UID=10001
RUN groupadd --gid "$GATEWAY_UID" gateway \
    && useradd --uid "$GATEWAY_UID" --gid "$GATEWAY_UID" --no-create-home --shell /usr/sbin/nologin gateway
# 自签证书要 cryptography；现场签发证书时可以不装
RUN pip install --no-cache-dir cryptography==50.0.1

WORKDIR /opt/gateway
COPY --chown=gateway:gateway devices/gateway/ilcs_gateway /opt/gateway/sdk/ilcs_gateway
COPY --chown=gateway:gateway devices/gateway/sample-cycler /opt/gateway/module

USER gateway
WORKDIR /opt/gateway/module
# 台账（/var/lib/ilcs-gateway）与证书、令牌（/run/secrets/gateway）都要挂持久卷
ENV PYTHONPATH=/opt/gateway/sdk GATEWAY_STATE_DIR=/var/lib/ilcs-gateway GATEWAY_SECRETS=/run/secrets/gateway
CMD ["python", "gateway.py"]
