# 驱动宿主镜像：从仓库根目录构建
#   docker build -f devices/host/deploy/Dockerfile -t ilcs-driver-host:latest .
# 只放驱动宿主、SiLA 契约与插件依赖；现场配置（sites/<现场>）、令牌、证书、台账都在运行时挂进来。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home ilcs
WORKDIR /opt/ilcs/devices/host
COPY devices/host/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=ilcs:ilcs devices/contracts /opt/ilcs/devices/contracts
COPY --chown=ilcs:ilcs devices/host/ilcs_host ./ilcs_host
RUN mkdir -p /data/state && chown -R ilcs:ilcs /data
USER ilcs
ENTRYPOINT ["python", "-m", "ilcs_host", "--site", "/site"]
