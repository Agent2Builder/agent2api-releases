#!/usr/bin/env bash
# 收集并校验一次构建 run 的发布附件。
#
# 用法：bash .github/scripts/collect-assets.sh <解包目录>
#
# 产出（写进解包目录，发布与镜像两步共用同一份结果）：
#   assets.txt   公开附件的相对路径（3.5.0-beta.8 起为 14 份，含安卓 APK 与 iOS IPA），每行一个
#   version.txt  从桌面端包名里取出的版本号（不带 v 前缀）
#
# 任一条不满足就非 0 退出。发布出去少一份附件的症状是「Release 建出来了，
# 某类用户却拿不到包」，属于最难查的静默故障 —— 所以这里宁可不发。
#
# ── 为什么还要校验 sha256 ─────────────────────────────────────
# 面板拿 .sha256 判断安装包是否完整（容器内一键更新先校验再替换文件）。工件
# 坏了、或上传时被截断，只有在这里拦得住。
#
# ── 为什么不用 `sha256sum -c` ─────────────────────────────────
# Windows job 上那份 .sha256 是 PowerShell 生成的，行尾是 CRLF，`-c` 会把
# `\r` 当成文件名的一部分，报 “‘xxx.zip\r’: No such file or directory”
# （2026-10-30 本地实测）。这里只取首个字段比对，末尾多什么字符都不影响。
set -euo pipefail

dist="${1:?用法: collect-assets.sh <解包目录>}"
cd "$dist"
shopt -s nullglob

# ── 先确认桌面端与服务端十份基准附件齐备，再按版本决定是否纳入手机端 ──
#
# linux-desktop 的上传路径是 target/release/bundle/{deb,appimage}/*，包根取两者的
# 最近公共祖先，于是解出来是 linux-desktop/{deb,appimage}/… —— 不能按平层扫
# （2026-10-17 实测：Release 建出来了，却没有 deb / AppImage 两个附件）。
includes_mobile() {
  local core="${1%%+*}" pre=""
  if [[ "$core" == *-* ]]; then
    pre="${core#*-}"
    core="${core%%-*}"
  fi
  local maj min pat
  IFS=. read -r maj min pat <<< "$core"
  if [ "$((10#$maj))" -ne 3 ]; then
    if [ "$((10#$maj))" -gt 3 ]; then return 0; fi
    return 1
  fi
  if [ "$((10#$min))" -ne 5 ]; then
    if [ "$((10#$min))" -gt 5 ]; then return 0; fi
    return 1
  fi
  if [ "$((10#$pat))" -ne 0 ]; then
    if [ "$((10#$pat))" -gt 0 ]; then return 0; fi
    return 1
  fi
  [ -z "$pre" ] && return 0
  local -a parts
  IFS=. read -ra parts <<< "$pre"
  local first="${parts[0]:-}"
  [[ "$first" =~ ^[0-9]+$ ]] && return 1
  [[ "$first" < "beta" ]] && return 1
  [[ "$first" > "beta" ]] && return 0
  local second="${parts[1]:-}"
  [ -z "$second" ] && return 1
  if [[ "$second" =~ ^[0-9]+$ ]]; then
    if [ "$((10#$second))" -ge 8 ]; then return 0; fi
    return 1
  fi
  return 0
}

assets=()
for f in windows-nsis/*.exe \
         macos-universal/*.dmg \
         linux-desktop/*/*.deb \
         linux-desktop/*/*.AppImage \
         linux-server-amd64/* \
         linux-server-arm64/* \
         windows-server-amd64/*; do
  assets+=("$f")
done

if [ "${#assets[@]}" -ne 10 ]; then
  echo "::error::桌面端与服务端附件不是十份（实际 ${#assets[@]} 份），拒绝发布："
  printf '  %s\n' "${assets[@]}"
  exit 1
fi

# ── 版本号：四份桌面端包名里嵌的是同一个，互相矛盾说明工件混了 ──
version=""
for name in windows-nsis/*.exe macos-universal/*.dmg linux-desktop/*/*.deb linux-desktop/*/*.AppImage; do
  base="${name##*/}"
  if [[ "$base" != Agent2API_* ]]; then
    echo "::error::产物名不符合 Agent2API_<版本>_… 的约定：$base"
    exit 1
  fi
  rest="${base#Agent2API_}"
  v="${rest%%_*}"
  if [ -z "$v" ] || [ "$v" = "$rest" ]; then
    echo "::error::从产物名里取不到版本号：$base"
    exit 1
  fi
  if [ -z "$version" ]; then
    version="$v"
  elif [ "$version" != "$v" ]; then
    echo "::error::产物名里的版本号不一致：$version 与 $v（$base）"
    exit 1
  fi
done

sha_checks=(linux-server-amd64/*.sha256 linux-server-arm64/*.sha256 windows-server-amd64/*.sha256)
if includes_mobile "$version"; then
  for mobile in "android-apk/Agent2API-${version}.apk" \
                "android-apk/Agent2API-${version}.apk.sha256" \
                "ios-ipa/Agent2API-${version}.ipa" \
                "ios-ipa/Agent2API-${version}.ipa.sha256"; do
    [ -f "$mobile" ] || { echo "::error::缺少手机端发布附件：$mobile"; exit 1; }
    assets+=("$mobile")
  done
  for extra in android-apk/* ios-ipa/*; do
    case "$extra" in
      "android-apk/Agent2API-${version}.apk"|"android-apk/Agent2API-${version}.apk.sha256"|\
      "ios-ipa/Agent2API-${version}.ipa"|"ios-ipa/Agent2API-${version}.ipa.sha256") ;;
      *) echo "::error::手机端产物目录存在预期之外的文件：$extra"; exit 1 ;;
    esac
  done
  sha_checks+=(android-apk/*.sha256 ios-ipa/*.sha256)
fi

# ── 服务端与手机端包的 sha256 必须与包本身对得上 ────────────────
for sum in "${sha_checks[@]}"; do
  pkg="${sum%.sha256}"
  [ -f "$pkg" ] || { echo "::error::$sum 存在但对应的包不在"; exit 1; }
  want="$(tr -d '\r' < "$sum" | awk 'NR == 1 { print $1 }')"
  got="$(sha256sum "$pkg" | awk '{ print $1 }')"
  if [ "$want" != "$got" ]; then
    echo "::error::${pkg##*/} 的 sha256 对不上（校验文件 $want，实际 $got）"
    exit 1
  fi
  echo "  OK ${pkg##*/}"
done

printf '%s\n' "${assets[@]}" > assets.txt
printf '%s\n' "$version" > version.txt
echo "→ 附件 ${#assets[@]} 份，版本 $version"
printf '  %s\n' "${assets[@]}"
