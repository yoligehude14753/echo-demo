# 字体资源

## NotoSansSC-Regular.ttf

用于 `fpdf2` PDF 产物生成时渲染简体中文。

- 来源：[`googlefonts/noto-cjk`](https://github.com/googlefonts/noto-cjk)
- 直链：`https://github.com/googlefonts/noto-cjk/raw/main/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf`
- 版本：Noto Sans CJK SC（Variable Subset TTF，2.004+）
- 许可：[SIL Open Font License 1.1 (OFL)](https://openfontlicense.org/) — 允许嵌入、再分发、商业使用，无附加条件
- 用途：仅在 PDF 生成 skill 内 `pdf.add_font("noto", "", <path>)` 加载；不参与代码 import

## 注入路径

`SkillExecutor` 在执行 PDF 代码时通过环境变量 `ECHODESK_PDF_FONT_PATH` 把字体绝对路径传给子进程，
LLM 生成的代码用 `os.environ["ECHODESK_PDF_FONT_PATH"]` 读取，避免硬编码路径。

## 大小

约 17.7 MB。CJK 字形数量大但 demo 内可接受；后续如需进一步瘦身，可换用按 GB2312 子集化的版本。
