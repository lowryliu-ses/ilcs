"""设备模块的测试不连 ILCS 数据库：只要仓库里的 devices/gateway/ilcs_gateway 与 api/（验收清单）。

模块放在 ILCS 仓库的 devices/gateway/ 下时自动找到；放在别处时设环境变量 ILCS_REPO 指向 ILCS 仓库根目录。
"""
import os
from pathlib import Path
import sys

MODULE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else MODULE.parents[2]
for path in (MODULE, REPO / "devices" / "gateway"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
