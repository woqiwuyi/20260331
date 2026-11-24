#!/usr/bin/env bash
# 在 OpenWrt 源码根目录的父目录中调用：
#   bash build-repo/config/apply.sh
# 第一个参数可指定 openwrt 目录，默认 ../openwrt
set -euo pipefail

OPENWRT_DIR="${1:-../openwrt}"
cd "$OPENWRT_DIR"

# 1) 重置目标平台选择
sed -i '/^CONFIG_TARGET_/d' .config
cat >> .config <<'EOF'
CONFIG_TARGET_ramips=y
CONFIG_TARGET_ramips_mt7621=y
CONFIG_TARGET_ramips_mt7621_DEVICE_d-team_newifi-d2=y
EOF

# 让 Kconfig 先按目标重新解析（新符号全部取默认值）
yes '' | make oldconfig >/dev/null

# 2) 应用自定义配置：先删旧值，再写新值，避免重复定义
CUSTOM="../build-repo/config/custom.config"
if [ ! -f "$CUSTOM" ]; then
  CUSTOM="$(cd "$(dirname "$0")" && pwd)/custom.config"
fi

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
  esac
  key="${line%%=*}"
  # 转义 key 中点、横线等正则元字符（这里主要是点）
  key_esc=$(printf '%s' "$key" | sed 's/[.[\*^$()+?{}|]/\\&/g')
  sed -i "/^${key_esc}=/d" .config
  echo "$line" >> .config
done < "$CUSTOM"

# 3) 再解析一次，处理依赖关系
yes '' | make oldconfig >/dev/null

echo "apply.sh: .config updated"
