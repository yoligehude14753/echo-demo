#!/usr/bin/env bash
# 把 playwright 跑出来的 webm 视频整理成干净的命名 + 转 mp4
# 用法：bash scripts/collect-scenario-videos.sh
# 产物：test-results/scenario-videos/sNN-名字.{webm,mp4}

set -eo pipefail

SRC="test-results/scenarios"
DST="test-results/scenario-videos"

if [ ! -d "$SRC" ]; then
    echo "ERROR: $SRC 不存在，先跑 npm run scenarios"
    exit 1
fi

rm -rf "$DST"
mkdir -p "$DST"

# 不用 bash 关联数组以避 UTF-8 key 报错；用纯前缀匹配
rename_dir() {
    case "$1" in
        s01_first_run_and_about*)          echo "s01-首次启动引导+关于对话框" ;;
        s02_status_pills-S02-*)            echo "s02a-诊断pill巡检-全绿态" ;;
        s02_status_pills-S02b*)            echo "s02b-麦克风denied深链" ;;
        s03_settings_remote_config*)       echo "s03-设置面板远端配置+回放引导" ;;
        s04_meeting_and_artifact*)         echo "s04-生成HTML命令链路" ;;
        s05_sad_paths_and_reconnec-40cde*) echo "s05a-生成失败错误处理" ;;
        s05_sad_paths_and_reconnec-53f5a*) echo "s05b-WebSocket断线重连" ;;
        s06_degraded_state-S06a*)          echo "s06a-heyi降级红pill" ;;
        s06_degraded_state-S06b*)          echo "s06b-Yunwu缺key橙pill" ;;
        *)                                  echo "" ;;
    esac
}

found=0
for dir in "$SRC"/*/; do
    base=$(basename "$dir")
    target=$(rename_dir "$base")
    if [ -z "$target" ]; then
        echo "WARN: 没有命名映射，跳过: $base"
        continue
    fi
    video="$dir/video.webm"
    if [ ! -f "$video" ]; then
        echo "WARN: 没有 video.webm: $dir"
        continue
    fi
    cp "$video" "$DST/$target.webm"
    # 转 mp4（H.264 + yuv420p，QuickTime / iOS / 微信都吃）
    ffmpeg -loglevel error -y -i "$video" -c:v libx264 -crf 20 -pix_fmt yuv420p \
        -movflags +faststart "$DST/$target.mp4"
    echo "  ✓ $target.{webm,mp4}"
    found=$((found + 1))
done

echo
echo "完成：$found 个场景视频已收集到 $DST/"
ls -lh "$DST/" | awk 'NR>1 {printf "  %-10s %s\n", $5, $NF}'
