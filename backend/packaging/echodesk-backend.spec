# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 EchoDesk backend 冻结成自带二进制（onedir）。

构建（必须在 backend/ 目录下运行，使 `app` 包可被解析）：
    cd backend
    .venv/bin/pyinstaller --noconfirm packaging/echodesk-backend.spec

产物：dist/echodesk-backend/echodesk-backend（+ _internal/ 依赖目录）
Electron 把整个 dist/echodesk-backend/ 作为 extraResources 打进安装包。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# backend 目录（spec 在 backend/packaging/ 下）加入搜索路径，确保 `app` 可解析
BACKEND_DIR = os.path.abspath(os.path.join(os.getcwd()))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

hiddenimports = []
hiddenimports += collect_submodules("app")
hiddenimports += collect_submodules("uvicorn")
# speechbrain 的 hyperparams.yaml 按字符串动态 import 类（lobes.features.Fbank 等），
# 静态分析看不到 → 必须全量收 speechbrain 子模块，否则运行时 "no such class"。
hiddenimports += collect_submodules("speechbrain")
# scipy 被 speechbrain (scipy.stats.lognorm) 间接 import；其编译扩展(.so)必须全收，
# 否则冻结后 "scipy install seems to be broken (extension modules cannot be imported)"。
hiddenimports += collect_submodules("scipy")
hiddenimports += ["aiosqlite", "backports", "backports.tarfile"]

datas = []
datas += collect_data_files("speechbrain")
datas += collect_data_files("hyperpyyaml")
datas += collect_data_files("scipy")
# app 包内的非 .py 资源：迁移 .sql、字体 .ttf、ppt_ib_deck 资产等（排除 node_modules/缓存）
datas += collect_data_files(
    "app",
    include_py_files=False,
    excludes=["**/node_modules/**", "**/__pycache__/**", "**/.DS_Store"],
)

binaries = []
binaries += collect_dynamic_libs("speechbrain")
binaries += collect_dynamic_libs("scipy")

a = Analysis(
    ["run_server.py"],
    pathex=[BACKEND_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "torchvision"],
    noarchive=False,
    # speechbrain 1.0 两个冻结难点：① __init__ 用 lazy_export → os.listdir(包目录)；
    # ② hyperparams.yaml 按字符串动态 import 类（如 speechbrain.lobes.features.Fbank）。
    # 纯 'py' 模式把整个 speechbrain/hyperpyyaml 当**磁盘源码**收（像正常 pip 安装的包），
    # listdir 扫得到、importlib 按字符串也 import 得到，彻底避开 PYZ split-brain。
    module_collection_mode={
        "speechbrain": "py",
        "hyperpyyaml": "py",
    },
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="echodesk-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="echodesk-backend",
)
