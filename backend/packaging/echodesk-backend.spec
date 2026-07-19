# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

BACKEND_ROOT = Path(SPECPATH).parent
hiddenimports = collect_submodules("app")
datas = collect_data_files(
    "app",
    excludes=["adapters/skill/assets/ppt_ib_deck"],
)
binaries = []

# SpeechBrain 1.1 resolves lazy exports by scanning its on-disk Python source
# directories at import time.  Keep those small source files as data so the
# frozen CPU diarizer has the same package layout without unpacking a model.
datas.extend(collect_data_files("speechbrain", include_py_files=True))

# EchoDesk's packaged diarizer is deliberately CPU eager-only.  SpeechBrain's
# inference interface imports the core ``torch.distributed`` API even for one
# local process, so that module must remain available.  Dynamo/Inductor are
# reached only by ``torch.compile`` (disabled at both process and ECAPA
# boundaries), while Triton/NVIDIA are accelerator runtimes absent from the
# official CPU wheel selected by the hashed packaging lock.
UNUSED_ACCELERATOR_MODULES = [
    "nvidia",
    "torch._dynamo",
    "torch._inductor",
    "triton",
]

# Artifact scripts are generated after the application has been frozen, so
# PyInstaller cannot discover these imports through normal static analysis.
# Collect their complete runtime explicitly, including package metadata and
# native extensions used by lxml/Pillow/fontTools dependencies.
for artifact_package in ("pydantic_settings", "docx", "openpyxl", "fpdf", "pdfplumber"):
    package_datas, package_binaries, package_hiddenimports = collect_all(
        artifact_package,
        include_py_files=False,
    )
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

datas.append(
    (
        str(BACKEND_ROOT / "app" / "adapters" / "repo" / "migrations"),
        "app/adapters/repo/migrations",
    )
)

PPT_RUNTIME_DIR = BACKEND_ROOT / "app" / "adapters" / "skill" / "assets" / "ppt_ib_deck"
for node_package in ("docxtemplater", "pizzip", "pptxgenjs"):
    package_manifest = PPT_RUNTIME_DIR / "node_modules" / node_package / "package.json"
    if not package_manifest.is_file():
        raise SystemExit(
            f"missing packaged PPT runtime dependency: {package_manifest}; "
            "run npm ci in app/adapters/skill/assets/ppt_ib_deck before PyInstaller"
        )
datas.append((str(PPT_RUNTIME_DIR), "app/adapters/skill/assets/ppt_ib_deck"))

hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    [str(Path(SPECPATH) / "entrypoint.py")],
    pathex=[str(BACKEND_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # MarkItDown declares optional WAV/MP3 transcription through
    # SpeechRecognition, but EchoDesk's RAG contract intentionally excludes
    # audio files.  Collecting it would bundle an obsolete x86_64 flac-mac
    # helper into every platform build and break the arm64 release boundary.
    excludes=["funasr", "speech_recognition", *UNUSED_ACCELERATOR_MODULES],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="echodesk-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
