#!/usr/bin/env python3
"""Build a Windows portable ZIP using the pinned, official embeddable CPython."""

import argparse
import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.14.10"
RUNTIME_NAME = "python-%s-embed-amd64.zip" % VERSION
RUNTIME_URL = "https://www.python.org/ftp/python/%s/%s" % (VERSION, RUNTIME_NAME)
RELEASE_URL = "https://www.python.org/downloads/release/python-31410/"
# SHA-256 published on the official Python release page above.
RUNTIME_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
APP_FILES = (
    "启动收码工具.bat", "启动局域网诊断.bat", "Windows使用说明.txt", "mailtool.py", "README.md", "LICENSE",
    "web/index.html", "web/style.css", "web/app.js",
)
REQUIRED_RUNTIME = {
    "python.exe", "python314.dll", "python3.dll", "python314.zip", "python314._pth",
    "_ssl.pyd", "_socket.pyd", "_sqlite3.pyd", "sqlite3.dll", "libssl-3.dll",
    "libcrypto-3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
}


def load_runtime(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".download")
        try:
            with urllib.request.urlopen(RUNTIME_URL, timeout=60) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != RUNTIME_SHA256:
                raise ValueError("Python 官方运行时 SHA-256 不匹配，已停止打包")
            partial.replace(path)
        finally:
            if partial.exists():
                partial.unlink()
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != RUNTIME_SHA256:
        raise ValueError("缓存运行时 SHA-256 不匹配，请重新下载官方文件")
    return data


def build(runtime_path, output):
    runtime = load_runtime(runtime_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(io.BytesIO(runtime)) as vendor:
            if vendor.testzip() is not None or not REQUIRED_RUNTIME.issubset(vendor.namelist()):
                raise ValueError("Python 嵌入式运行时不完整")
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
                # Explicit allowlist: never package account databases, local credentials or test fixtures.
                for name in APP_FILES:
                    data = (ROOT / name).read_bytes()
                    if name.endswith(".bat"):
                        data = data.decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
                    bundle.writestr("MailCode/" + name, data)
                for info in vendor.infolist():
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                        raise ValueError("运行时包含无效的文件路径")
                    bundle.writestr("MailCode/runtime/" + info.filename, vendor.read(info.filename))
                manifest = {"python_version": VERSION, "platform": "Windows x64",
                            "source": RUNTIME_URL, "sha256": RUNTIME_SHA256,
                            "release_page": RELEASE_URL, "license": "runtime/LICENSE.txt"}
                bundle.writestr("MailCode/runtime-info.json", json.dumps(manifest, indent=2) + "\n")
        with zipfile.ZipFile(temporary) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("最终 ZIP 完整性校验失败")
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def main():
    parser = argparse.ArgumentParser(description="打包 Windows 10/11 x64 免安装版（含官方 Python）")
    parser.add_argument("--runtime", type=Path, default=ROOT / ".build-cache" / RUNTIME_NAME)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "收码台-Windows-简洁版-x64.zip")
    args = parser.parse_args()
    path = build(args.runtime, args.output)
    print("已生成：%s（%.1f MB，含 Python %s）" % (path, path.stat().st_size / 1024 ** 2, VERSION))


if __name__ == "__main__":
    main()
