FROM python:3.11-slim

WORKDIR /opt/ilcs
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# 固定非特权身份，便于宿主持久目录和只读设备凭据按 UID 授权。
ARG ILCS_UID=10001
RUN groupadd --gid "$ILCS_UID" ilcs \
    && useradd --uid "$ILCS_UID" --gid "$ILCS_UID" --no-create-home \
       --shell /usr/sbin/nologin ilcs

COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY --chown=ilcs:ilcs api /opt/ilcs/api
COPY --chown=ilcs:ilcs executor /opt/ilcs/executor
# 迁移脚本必须进镜像：迁移是部署的独立步骤，在容器里执行
COPY --chown=ilcs:ilcs scripts /opt/ilcs/scripts
# 设备契约（SiLA 2 特性、Modbus 寄存器表、OPC UA 节点）与外部模拟设备：驱动与模拟器读同一份定义
COPY --chown=ilcs:ilcs contracts /opt/ilcs/contracts
COPY --chown=ilcs:ilcs simulators /opt/ilcs/simulators

WORKDIR /opt/ilcs/api
USER ilcs
# 启动只校验库版本；不兼容时健康检查报 schema_mismatch，业务接口一律 503
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
