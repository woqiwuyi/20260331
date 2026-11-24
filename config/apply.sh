#!/usr/bin/env bash
# 在 OpenWrt 源码根目录的父目录中调用：
#   bash build-repo/config/apply.sh
# 第一个参数可指定 openwrt 目录，默认 ../openwrt
set -euo pipefail

OPENWRT_DIR="${1:-../openwrt}"
cd "$OPENWRT_DIR"

# 用 oldconfig 让 Kconfig 重新解析。切换 target 时会产生大量新符号询问，
# 用 yes '' 自动回车的默认值。注意：pipefail 下 yes 会因 SIGPIPE 返回 1，
# 因此在调用期间临时关闭 pipefail，使管道退出码取 make oldconfig 的真实结果。
oldconfig() {
  set +o pipefail
  yes '' | make oldconfig >/dev/null
  local rc=$?
  set -o pipefail
  return $rc
}

# 1) 重置目标平台选择
sed -i '/^CONFIG_TARGET_/d' .config
cat >> .config <<'EOF'
CONFIG_TARGET_ramips=y
CONFIG_TARGET_ramips_mt7621=y
CONFIG_TARGET_ramips_mt7621_DEVICE_d-team_newifi-d2=y
EOF

oldconfig

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
oldconfig

echo "apply.sh: .config updated"
