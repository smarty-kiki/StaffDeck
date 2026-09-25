# packaging/fetch_runtime_python.py
# 用法: python packaging/fetch_runtime_python.py <dest_dir> [--expect-arch arm64|x86_64]
#
# 下载对应平台的 python-build-standalone（install_only 变体自带 pip），
# 预装通用技能高频包后，供 build 脚本拷进最终产物的 runtime/ 目录。
#
# 复用策略（避免每次构建都重新下载 + 重新 pip install）：
#   - 已解压的 <dest_dir>/python 只要版本对得上、自检能过，直接复用，不联网也不重装依赖。
#   - 下载好的 tar 包缓存在 <cache>/ 而不是 <dest_dir>/ 里。以前 tar 包和被清空的
#     dest_dir 放在一起，每次重建都会把缓存一起删掉，所以每次都得重新下载。
#   - STAFFDECK_RUNTIME_CACHE 可指定缓存目录；STAFFDECK_RUNTIME_REFRESH=1 强制重下。
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.error
import time
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# 已知稳定 release（执行前用 curl -sI 核实资产可达；失效则更新此处并同步 docs）
BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/20240415"
ASSETS = {
    ("Darwin", "arm64"): "cpython-3.11.9+20240415-aarch64-apple-darwin-install_only.tar.gz",
    ("Darwin", "x86_64"): "cpython-3.11.9+20240415-x86_64-apple-darwin-install_only.tar.gz",
    ("Linux", "x86_64"): "cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz",
    ("Windows", "AMD64"): "cpython-3.11.9+20240415-x86_64-pc-windows-msvc-install_only.tar.gz",
}
# 预装清单（第一版）：含 Word(python-docx) / Excel(openpyxl) 处理；不预装 pandas/numpy（体积大，用户需要时联网装）
PRELOAD = ["requests", "httpx", "beautifulsoup4", "lxml", "python-docx",
           "openpyxl", "python-dateutil"]

# 架构别名归一：Windows 返回 AMD64，mac/linux 返回 x86_64/arm64/aarch64
ARCH_ALIASES = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
    "arm64": "arm64", "aarch64": "arm64",
}

# 下载缓存：刻意放在 dest_dir 之外（dest_dir 重建时会被整个清空）+ 已被 .gitignore 忽略。
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".runtime-cache"


def _norm_arch(value: str) -> str:
    return ARCH_ALIASES.get(value.lower(), value.lower())


def _asset_python_version(asset: str) -> str:
    """从 cpython-3.11.9+20240415-aarch64-... 取出 3.11.9，用来判断已有运行时能否复用。"""
    return asset.split("cpython-", 1)[-1].split("+", 1)[0].split("-", 1)[0]


def _machine() -> str:
    machine = platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if machine:
        return machine
    if platform.system() == "Windows" and sys.maxsize > 2**32:
        return "AMD64"
    return machine


def _download(url: str, destination: Path, *, attempts: int = 5) -> None:
    """Download a release asset with retries for transient GitHub 5xx errors."""
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "StaffDeck-runtime-fetch/1.0", "Accept": "application/octet-stream"},
            )
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(destination)
            return
        except (OSError, urllib.error.HTTPError) as error:
            last_error = error
            temporary.unlink(missing_ok=True)
            if attempt == attempts:
                break
            delay = min(30, 2 ** (attempt - 1))
            print(f"下载暂时失败（第 {attempt}/{attempts} 次）：{error}；{delay}s 后重试", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"下载运行时失败（重试 {attempts} 次）：{url}: {last_error}") from last_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_dir() -> Path:
    override = os.environ.get("STAFFDECK_RUNTIME_CACHE", "").strip()
    return Path(override) if override else DEFAULT_CACHE_DIR


def _cached_asset(asset: str, *, refresh: bool = False) -> Path:
    """取回 tar 包：缓存还在且校验一致就直接用，否则才联网下载。

    缓存刻意放在 dest_dir 之外 —— dest_dir 每次重建都会被清空。
    """
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / asset
    digest_path = cache_dir / f"{asset}.sha256"
    if not refresh and archive.is_file() and digest_path.is_file():
        recorded = digest_path.read_text(encoding="utf-8").split()
        expected = recorded[0] if recorded else ""
        if expected and _sha256(archive) == expected:
            size_mib = archive.stat().st_size / 1048576
            print(f"复用已缓存的运行时包 {asset}（{size_mib:.1f} MiB，跳过下载）")
            return archive
        print(f"缓存 {asset} 校验不一致，重新下载", file=sys.stderr)
    print(f"下载 {BASE}/{asset} ...")
    _download(f"{BASE}/{asset}", archive)
    # 记下摘要，下次命中缓存时用来确认包没被截断/改坏。
    digest_path.write_text(f"{_sha256(archive)}  {asset}\n", encoding="utf-8")
    return archive


def _runtime_python_version(py: Path) -> str | None:
    if not py.is_file():
        return None
    probe = subprocess.run([str(py), "--version"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        return None
    for line in ((probe.stdout or "") + (probe.stderr or "")).splitlines():
        if line.strip().startswith("Python "):
            return line.strip()[len("Python "):]
    return None


def _validate_runtime(py: Path) -> int:
    """验证附带 Python 的 SSL 证书 + 关键包可用（否则技能里 https/word/excel 会失败）"""
    check = subprocess.run(
        [str(py), "-c",
         "import ssl, requests, docx, openpyxl; "
         "print(ssl.get_default_verify_paths().cafile or 'certifi')"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(f"附带 Python 自检失败（ssl/requests/docx/openpyxl）：{check.stderr}", file=sys.stderr)
        return 5
    print(f"runtime ready at {py} (ssl: {check.stdout.strip()})")
    return 0


def main(argv: list[str]) -> int:
    dest = argv[0]
    expect_arch = None
    if "--expect-arch" in argv:
        expect_arch = argv[argv.index("--expect-arch") + 1]

    key = (platform.system(), _machine())
    if key not in ASSETS:
        print(f"不支持的平台/架构: {key}", file=sys.stderr)
        return 3
    # 确保附带 Python 架构与 PyInstaller 产物架构一致（归一化后比较）
    if expect_arch and _norm_arch(expect_arch) != _norm_arch(key[1]):
        print(f"架构不匹配: 期望 {expect_arch}, 实际 {key[1]}", file=sys.stderr)
        return 4

    asset = ASSETS[key]
    dest_dir = Path(dest)
    py = dest_dir / "python" / ("python.exe" if key[0] == "Windows" else "bin/python3")
    refresh = os.environ.get("STAFFDECK_RUNTIME_REFRESH", "").strip() == "1"

    # 已解压的运行时版本对得上、自检也过，就直接用：不动 dest_dir、不联网、不重装依赖。
    if not refresh and _runtime_python_version(py) == _asset_python_version(asset):
        if _validate_runtime(py) == 0:
            print(f"复用已解压的运行时（跳过下载与 pip install）：{py}")
            return 0
        print("已有运行时自检未通过，改为完整重建", file=sys.stderr)

    # 只有真重建时才清空，保证干净解压（避免重复运行时 pip/site-packages 半升级损坏）
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(_cached_asset(asset, refresh=refresh)) as tar:
        tar.extractall(dest_dir)

    # install_only 变体自带 pip（24.0），直接装预装包；不升级 pip（升级易触发 resolvelib 半损坏）
    subprocess.run(
        [
            str(py), "-m", "pip", "install",
            "--no-cache-dir", "--disable-pip-version-check",
            "--timeout", "30", "--retries", "5",
            *PRELOAD,
        ],
        check=True,
    )

    return _validate_runtime(py)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
