# 模拟的电芯检测仪表（Keithley 2450 / 2400、Hioki BT3562）镜像：只带模拟设备的统一控制口（devices/simulators/common）
# 与本模块，不带 ILCS。构建上下文是只含这两个目录的临时目录（或 ILCS 仓库根目录）：
#   docker build -f devices/gateway/scpi-cell-meter/deploy/Dockerfile -t ilcs-scpi-cell-meter:latest <上下文>
# 只用来模拟：真仪表由 ILCS 的 line_command_v1 直接连（LAN / 串口服务器），不需要任何网关进程。
FROM python:3.11-slim

# 和 ILCS 容器同一个 UID：控制口令牌写进共享的 secrets/simctl，ILCS 按属主只读的文件读得到
ARG SIM_UID=10001
RUN groupadd --gid "$SIM_UID" sim \
    && useradd --uid "$SIM_UID" --gid "$SIM_UID" --no-create-home --shell /usr/sbin/nologin sim

WORKDIR /opt/sim
COPY --chown=sim:sim devices/simulators/__init__.py /opt/sim/devices/simulators/__init__.py
COPY --chown=sim:sim devices/simulators/common /opt/sim/devices/simulators/common
COPY --chown=sim:sim devices/gateway/scpi-cell-meter/simulator /opt/sim/devices/gateway/scpi-cell-meter/simulator

USER sim
ENV PYTHONUNBUFFERED=1 SIM_CONTROL_PORT=9900
CMD ["python", "devices/gateway/scpi-cell-meter/simulator/server.py"]
