"""Prepare a relocatable Node + Anthropic Sandbox Runtime bundle.

The resulting directory contains ``bin/node`` and the SRT package. Runtime
code invokes the package entrypoint with the bundled Node binary, so the
installed app does not depend on a user's global npm installation.

复用策略（避免每次构建都重新下载 Node + 重跑 npm ci）：
  - 目标目录里已经有 ``bin/node``、依赖版本与评审过的 lockfile 一致、补丁也在位时，
    直接复用整个 bundle，不再联网也不再 npm ci。
  - Node 的 tar 包缓存在 <cache>/ 里，重装依赖时才需要用；缓存被截断会自动重下一次。
  - STAFFDECK_RUNTIME_CACHE 可指定缓存目录；STAFFDECK_RUNTIME_REFRESH=1 强制重装。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PACKAGE = "@anthropic-ai/sandbox-runtime@0.0.67"
MANIFEST_DIR = Path(__file__).resolve().parent
PACKAGE_JSON = MANIFEST_DIR / "sandbox-runtime-package.json"
PACKAGE_LOCK = MANIFEST_DIR / "sandbox-runtime-package-lock.json"
NODE_VERSION = os.environ.get("STAFFDECK_NODE_VERSION", "v22.14.0")
# Node tar 包缓存：与会被重装的 bundle 目录分开，且已被 .gitignore 忽略。
DEFAULT_CACHE_DIR = MANIFEST_DIR / ".runtime-cache"
PATCH_MARKER = "staffdeck-allow-all-domains-patch-v1"
PATCHED_SHA256 = {
    "sandbox-config.js": "17a9bdd4cce375bb098f9c02eb564cf80806079571d4ff784e2af7d27db446bb",
    "sandbox-manager.js": "dca5176508d6ee31807e2138eea82bcbb6f6e5e40c67aff2ac9958d3bc893b4c",
}
SRT_INTEGRITY = "sha512-4doSyr6KNdc/4zARMXYEawhFu3z6bPQjgKRq3lKp6dbgEYVMv39oaLJ28QsDc7TmLvrLqzHW+VzD2LAXxvnw8A=="
NODE_SHA256 = {
    "node-v22.14.0-darwin-arm64.tar.gz": "e9404633bc02a5162c5c573b1e2490f5fb44648345d64a958b17e325729a5e42",
    "node-v22.14.0-darwin-x64.tar.gz": "6698587713ab565a94a360e091df9f6d91c8fadda6d00f0cf6526e9b40bed250",
    "node-v22.14.0-linux-x64.tar.gz": "9d942932535988091034dc94cc5f42b6dc8784d6366df3a36c4c9ccb3996f0c2",
    "node-v22.14.0-linux-arm64.tar.gz": "8cf30ff7250f9463b53c18f89c6c606dfda70378215b2c905d0a9a8b08bd45e0",
    "node-v22.14.0-win-arm64.zip": "2d71f5f9b2fffa33baa108c07d74b0d24e0c3dd8f441d567772ae0e3dd4b1a22",
    "node-v22.14.0-win-x64.zip": "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217",
}


def _apply_allow_all_domains_patch(destination: Path) -> None:
    """Apply the reviewed upstream PR #283 behavior to the bundled dist files."""
    package_root = destination / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    config_path = package_root / "dist" / "sandbox" / "sandbox-config.js"
    manager_path = package_root / "dist" / "sandbox" / "sandbox-manager.js"
    if not config_path.is_file() or not manager_path.is_file():
        raise SystemExit("SRT bundle layout is unexpected; refusing to patch it.")
    config = config_path.read_text(encoding="utf-8")
    manager = manager_path.read_text(encoding="utf-8")
    if PATCH_MARKER in config and PATCH_MARKER in manager:
        _verify_patched_dist(config_path, manager_path)
        return
    config_anchor = "    allowUnixSockets: z\n"
    config_insert = (
        "    // " + PATCH_MARKER + "\n"
        "    allowAllDomains: z.boolean().optional(),\n"
    )
    manager_anchor = "    // Check allowed domains\n"
    manager_insert = (
        "    // " + PATCH_MARKER + "\n"
        "    if (config.network.allowAllDomains) {\n"
        "        logForDebugging(`Allowed by allowAllDomains: ${host}:${port}`);\n"
        "        return true;\n"
        "    }\n"
    )
    if config.count(config_anchor) != 1 or manager.count(manager_anchor) != 1:
        raise SystemExit("SRT patch anchors changed; refusing an unverified patch.")
    config_path.write_bytes(
        config.replace(config_anchor, config_insert + config_anchor).encode("utf-8")
    )
    manager_path.write_bytes(
        manager.replace(manager_anchor, manager_insert + manager_anchor).encode("utf-8")
    )
    if PATCH_MARKER not in config_path.read_text(encoding="utf-8") or PATCH_MARKER not in manager_path.read_text(encoding="utf-8"):
        raise SystemExit("SRT patch verification failed.")
    _verify_patched_dist(config_path, manager_path)


def _verify_patched_dist(config_path: Path, manager_path: Path) -> None:
    for path in (config_path, manager_path):
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != PATCHED_SHA256[path.name]:
            raise SystemExit(f"Patched SRT dist hash mismatch: {path.name}")


def _cache_dir() -> Path:
    override = os.environ.get("STAFFDECK_RUNTIME_CACHE", "").strip()
    return Path(override) if override else DEFAULT_CACHE_DIR


def _node_file_name() -> str:
    """按当前平台给出 bundle 里 Node 可执行文件的名字。"""
    return "node.exe" if platform.system().lower() == "windows" else "node"


def _bundle_is_ready(destination: Path) -> bool:
    """判断已有 bundle 能否直接复用：Node 在位、依赖与 lockfile 一致、补丁已打。

    这三项分别由 ``_verify_srt_integrity``（逐包版本比对评审过的 lockfile）和
    ``_apply_allow_all_domains_patch``（幂等 + 补丁后文件哈希校验）把关，
    所以复用和重新 npm ci 一样可信。
    """
    node = destination / "bin" / _node_file_name()
    if not node.is_file():
        return False
    # 清单被改过（例如升了 SRT 版本）就必须重建，不能复用旧依赖。
    for manifest, name in ((PACKAGE_JSON, "package.json"), (PACKAGE_LOCK, "package-lock.json")):
        installed = destination / name
        if not installed.is_file() or installed.read_bytes() != manifest.read_bytes():
            return False
    try:
        _verify_srt_integrity(destination)
        _apply_allow_all_domains_patch(destination)
    except SystemExit:
        return False
    return _node_version(node) == NODE_VERSION


def _node_version(node: Path) -> str | None:
    probe = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        return None
    return ((probe.stdout or "") + (probe.stderr or "")).strip() or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    destination = args.destination.resolve()
    refresh = os.environ.get("STAFFDECK_RUNTIME_REFRESH", "").strip() == "1"
    # 复用优先：bundle 校验通过就不再依赖 npm，也不再联网。
    if not refresh and _bundle_is_ready(destination):
        print(f"复用已准备好的 SRT runtime（跳过 Node 下载与 npm ci）：{destination}")
        return 0

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required to prepare the SRT runtime bundle.")

    destination.mkdir(parents=True, exist_ok=True)
    bin_dir = destination / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _download_node_runtime(destination)
    shutil.copy2(PACKAGE_JSON, destination / "package.json")
    shutil.copy2(PACKAGE_LOCK, destination / "package-lock.json")
    subprocess.run(
        [
            npm, "ci", "--prefix", str(destination), "--no-audit", "--no-fund",
            "--omit=dev",
        ],
        check=True,
    )
    _verify_srt_integrity(destination)
    _apply_allow_all_domains_patch(destination)
    print(f"SRT runtime ready at {destination}")
    return 0


def _cached_node_archive(filename: str, url: str, *, force: bool = False) -> Path:
    """取回 Node 包：缓存里有就直接用，否则下载到缓存后再返回。"""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / filename
    if archive.is_file() and not force:
        print(f"复用已缓存的 Node 运行时包 {filename}（跳过下载）")
        return archive
    print(f"下载 {url} ...")
    staged = archive.with_suffix(archive.suffix + ".part")
    try:
        socket.setdefaulttimeout(60)
        urllib.request.urlretrieve(url, staged)
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise SystemExit(f"Failed to download Node runtime from {url}: {exc}") from exc
    staged.replace(archive)
    return archive


def _download_node_runtime(destination: Path) -> None:
    system = platform.system().lower()
    machine = _machine()
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Node architecture: {machine}")
    if system == "darwin":
        target = f"darwin-{arch}"
        suffix = "tar.gz"
    elif system == "linux":
        target = f"linux-{arch}"
        suffix = "tar.gz"
    elif system == "windows":
        target = f"win-{arch}"
        suffix = "zip"
    else:
        raise SystemExit(f"Unsupported Node platform: {system}")
    filename = f"node-{NODE_VERSION}-{target}.{suffix}"
    url = f"https://nodejs.org/dist/{NODE_VERSION}/{filename}"
    archive = _cached_node_archive(filename, url)
    expected_hash = (
        os.environ.get("STAFFDECK_NODE_SHA256", "").strip().lower()
        or NODE_SHA256.get(filename, "")
    )
    if not expected_hash:
        raise SystemExit(
            f"No trusted SHA256 is configured for {filename}; set STAFFDECK_NODE_SHA256."
        )
    actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        # 缓存包被截断/损坏：丢掉重下一次，仍不一致才报错（避免脏缓存卡死后续构建）。
        print(f"缓存 {filename} 摘要不一致，重新下载", file=sys.stderr)
        archive = _cached_node_archive(filename, url, force=True)
        actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit(
            f"Node runtime SHA256 mismatch: expected {expected_hash}, got {actual_hash}"
        )
    target_bin = destination / "bin"
    target_bin.mkdir(parents=True, exist_ok=True)
    node_name = "node.exe" if system == "windows" else "node"
    target_node = target_bin / node_name
    if suffix == "tar.gz":
        with tarfile.open(archive, "r:gz") as handle:
            members = [
                member
                for member in handle.getmembers()
                if member.name.endswith("/bin/node") and member.isfile()
            ]
            if len(members) != 1:
                raise SystemExit("Node archive does not contain exactly one bin/node.")
            source = handle.extractfile(members[0])
            if source is None:
                raise SystemExit("Node executable cannot be read from the archive.")
            with source, target_node.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        with zipfile.ZipFile(archive) as handle:
            members = [
                name
                for name in handle.namelist()
                if name.endswith("/node.exe") and not name.endswith("/")
            ]
            if len(members) != 1:
                raise SystemExit("Node archive does not contain exactly one node.exe.")
            with handle.open(members[0]) as source, target_node.open("wb") as output:
                shutil.copyfileobj(source, output)
    if system != "windows":
        target_node.chmod(target_node.stat().st_mode | 0o111)


def _machine() -> str:
    machine = platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if machine:
        return machine.lower()
    if platform.system().lower() == "windows" and sys.maxsize > 2**32:
        return "amd64"
    return machine.lower()


def _verify_srt_integrity(destination: Path) -> None:
    lock_path = destination / "package-lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        trusted = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        packages = _normalized_lock_packages(lock["packages"])
        trusted_packages = _normalized_lock_packages(trusted["packages"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit("SRT package lock is missing or malformed; refusing the bundle.") from exc
    if packages != trusted_packages:
        raise SystemExit("SRT dependency graph does not match the reviewed lockfile.")
    for relative, package in trusted_packages.items():
        if not relative:
            continue
        try:
            installed = json.loads(
                (destination / relative / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"SRT dependency is missing: {relative}") from exc
        if installed.get("version") != package.get("version"):
            raise SystemExit(f"SRT dependency version mismatch: {relative}")


def _normalized_lock_packages(packages: object) -> dict[str, object]:
    if not isinstance(packages, dict):
        raise TypeError("packages must be an object")
    normalized: dict[str, object] = {}
    for raw_key, value in packages.items():
        key = str(raw_key).replace("\\", "/")
        marker = "node_modules/"
        if marker in key:
            key = marker + key.rsplit(marker, 1)[1]
        elif key:
            continue
        normalized[key] = value
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
