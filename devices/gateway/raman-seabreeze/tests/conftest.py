"""设备模块的测试不连 ILCS，也不要求放在 ILCS 仓库里：网关 SDK（ilcs_gateway）就在模块的上一级目录
（模块放在别处时用 PYTHONPATH 指过去）。

与 ILCS 的一致性测试（跑 ILCS 的接入验收清单、按 ILCS 的口径核对 profile.json）要找到 ILCS 仓库的 api/：设环境变量
ILCS_REPO 指向 ILCS 仓库根目录，或者把设备仓库和 ILCS 仓库放在同一个目录下；找不到就跳过这几项，模块自测照跑。
"""
from pathlib import Path
import sys

MODULE = Path(__file__).resolve().parents[1]
for path in (MODULE, MODULE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
