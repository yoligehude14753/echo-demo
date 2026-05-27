"""``python -m minutes_kit.demo`` — 启动 demo dev server。

实际实现在仓库 demo/server.py（不放在包内，强调 demo 是验证工具不是核心代码）。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    # 把 demo/ 目录加进 path，再 import server
    demo_dir = Path(__file__).resolve().parent.parent.parent.parent / "demo"
    if not demo_dir.exists():
        # 安装到 site-packages 时找不到 demo/，提示用户怎么跑
        print(
            "minutes_kit.demo 仅在开发 checkout 下可用。请 cd 到仓库根目录后执行：\n"
            "    cd minutes_kit && python demo/server.py",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.path.insert(0, str(demo_dir))
    from server import main as server_main  # type: ignore[import-not-found]
    server_main()


if __name__ == "__main__":
    main()
