#!/bin/sh
# 在应用的受限 shell 中使用已有运行时，不修改用户的 shell 配置。
set -eu

runtime_root="${CODEX_RUNTIME_ROOT:-${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies}"

if ! command -v node >/dev/null 2>&1; then
    if [ ! -x "$runtime_root/node/bin/node" ]; then
        echo "没有找到 Node.js；请安装 Node，或用 CODEX_RUNTIME_ROOT 指定运行时目录。" >&2
        exit 127
    fi
    PATH="$runtime_root/node/bin:$PATH"
fi

# macOS 的系统 Git 可能只是依赖 Xcode 的启动器；优先使用随附的独立 Git。
if [ -x "$runtime_root/bin/fallback/git" ]; then
    PATH="$runtime_root/bin/fallback:$PATH"
fi
export PATH

if [ "$#" -eq 0 ]; then
    echo "用法：sh scripts/with-runtime.sh <命令> [参数...]" >&2
    exit 2
fi

exec "$@"
