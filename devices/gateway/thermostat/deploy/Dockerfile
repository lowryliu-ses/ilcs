# 模拟的恒温循环器 / 制冷搅拌网关（假冷水机 + 4 块假搅拌板）镜像：只带网关 SDK 与本模块，不带 ILCS、不带 pyserial
# （假设备走 TCP）。构建上下文是 ILCS 仓库根目录：
#   docker build -f devices/gateway/thermostat/deploy/Dockerfile -t ilcs-thermostat:latest .
# 只用来跑 --simulate：接真机时网关跑在接冷水机的那台电脑上（串口 / 串口服务器 / 冷水机网口），见 deploy/README.md。
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
COPY --chown=gateway:gateway devices/gateway/thermostat /opt/gateway/module

USER gateway
WORKDIR /opt/gateway/module
# 作业台账与作业记录（/var/lib/ilcs-gateway）、证书与令牌（/run/secrets/gateway）都要挂持久卷
ENV PYTHONPATH=/opt/gateway/sdk GATEWAY_STATE_DIR=/var/lib/ilcs-gateway GATEWAY_SECRETS=/run/secrets/gateway \
    GATEWAY_SIMULATE=1 GATEWAY_CONFIG=/opt/gateway/module/simulator/thermostat-sim.json
CMD ["python", "gateway.py"]
