#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Newifi D2 OC1100 固件一键重建脚本

改完 config/ / scripts/ / files/ / workflow 里的任意工程文件后，运行：
    python3 scripts/rebuild_firmware.py

流程：
  1. 比对本地工程文件与 GitHub 仓库内容，只上传有变化的文件
  2. push 自动触发编译（无文件变化时手动 dispatch 强制编译）
  3. 轮询等待编译完成
  4. 下载 artifact、解压、校验 SHA256（与构建日志比对）
  5. 备份当前固件为 ..._prev.bin，保存新固件

常用参数：
  --check            只检查本地与远程差异，不推送、不编译
  --no-download      推送并编译，但不下载固件
  --force            即使无文件变化也强制编译
  --mhz 1100|1000    手动 dispatch 时的 CPU 频率（默认 1100）
  --poll 30          轮询间隔秒数（默认 30）
  --timeout 120      等待编译的最长分钟数（默认 120）
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

OWNER = "woqiwuyi"
REPO = "20260331"
BRANCH = "main"
WORKFLOW = "build-newifi-d2-oc1100.yml"
FIRMWARE_NAME = "openwrt-ramips-mt7621-d-team_newifi-d2-squashfs-sysupgrade.bin"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_DIR = os.path.join(ROOT, "firmware")

# 本地工程文件（相对 ROOT）→ 需推送到仓库的路径。脚本自身不推送。
SOURCE_FILES = [
    ".github/workflows/build-newifi-d2-oc1100.yml",
    "config/custom.config",
    "config/apply.sh",
    "scripts/patch_dts.py",
    "scripts/patch_clk.py",
    "files/etc/uci-defaults/99-custom",
]

TOKEN = ""
HEADERS = {}


def log(msg):
    print(msg, flush=True)


def get_token():
    """优先读环境变量 GITHUB_TOKEN，否则从 git 凭据存储读取。"""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=30,
        )
        for line in p.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    raise RuntimeError("无法获取 GitHub token：请设置 GITHUB_TOKEN 环境变量，或配置 git 凭据存储。")


def api(method, path, body=None, timeout=30, retries=3):
    """带简单重试的 GitHub API 调用。"""
    data = json.dumps(body).encode() if body is not None else None
    url = "https://api.github.com" + path
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            code = e.code
            detail = e.read().decode(errors="replace")
            if code < 500 and code != 429:
                return code, detail
            last = f"HTTP {code}: {detail[:200]}"
        except Exception as e:
            last = str(e)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"API 调用失败 {method} {path}: {last}")


def content_bytes(s, resp):
    if s != 200:
        return None, None
    return base64.b64decode(resp.get("content", "")), resp.get("sha")


def check_and_push(check_only=False):
    """比对并推送变化的文件。返回 (变化数量, 文件总数)。"""
    changed = 0
    log("=== 工程文件差异检查 ===")
    for rel in SOURCE_FILES:
        local = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(local):
            log(f"  [缺失] {rel}（本地不存在，跳过）")
            continue
        with open(local, "rb") as f:
            data = f.read()
        s, resp = api("GET", f"/repos/{OWNER}/{REPO}/contents/{rel}?ref={BRANCH}")
        remote, sha = content_bytes(s, resp)
        if remote == data:
            log(f"  [相同] {rel}")
            continue
        if check_only:
            tag = "新增" if remote is None else "变化"
            log(f"  [{tag}] {rel}")
            changed += 1
            continue
        body = {
            "message": f"rebuild: update {rel}",
            "content": base64.b64encode(data).decode(),
            "branch": BRANCH,
        }
        if sha:
            body["sha"] = sha
        s2, resp2 = api("PUT", f"/repos/{OWNER}/{REPO}/contents/{rel}", body)
        if s2 >= 300:
            raise RuntimeError(f"推送失败 {rel}: {str(resp2)[:300]}")
        log(f"  [已推送] {rel}  →  {resp2.get('commit', {}).get('sha', '')[:8]}")
        changed += 1
    log(f"变化文件：{changed}/{len(SOURCE_FILES)}")
    return changed


def latest_run_id():
    s, data = api("GET", f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=5")
    if s != 200:
        raise RuntimeError(f"查询 workflow runs 失败: {str(data)[:200]}")
    runs = data.get("workflow_runs", [])
    return runs[0]["id"] if runs else 0


def wait_new_run(before_id, wait_seconds=120):
    """等待新 run 出现。"""
    deadline = time.time() + wait_seconds
    log(f"等待新编译任务出现（基线 Run #{before_id}）...")
    while time.time() < deadline:
        s, data = api("GET", f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=5")
        runs = data.get("workflow_runs", [])
        for r in runs:
            if r["id"] > before_id:
                log(f"检测到新 Run #{r['run_number']}（id={r['id']}, event={r['event']}）")
                return r["id"]
        time.sleep(5)
    raise RuntimeError("等待新编译任务超时（120 秒）。请检查 GitHub Actions 是否被禁用或 push 未触发。")


def step_summary(run_id):
    """打印各步骤状态，返回 (status, conclusion, failed_step_name)。"""
    s, data = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs")
    if s != 200:
        return "unknown", None, None
    failed = None
    for job in data.get("jobs", []):
        for st in job.get("steps", []):
            c = st.get("conclusion")
            if c == "failure" and failed is None:
                failed = st["name"]
            if st["number"] >= 10:
                log(f"  [{(c or '-'):9}] {st['number']:>2}. {st['name']}")
    if data.get("jobs"):
        j = data["jobs"][0]
        return j.get("status"), j.get("conclusion"), failed
    return "unknown", None, failed


def wait_run(run_id, poll, timeout_min):
    """轮询等待 run 完成。"""
    deadline = time.time() + timeout_min * 60
    log(f"开始跟踪编译（轮询 {poll}s，最长 {timeout_min} 分钟）...")
    last_status = None
    while time.time() < deadline:
        s, run = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}")
        if s != 200:
            time.sleep(poll)
            continue
        status, conclusion = run.get("status"), run.get("conclusion")
        if status == "completed":
            log(f"编译结束：status={status}, conclusion={conclusion}")
            return conclusion
        if status != last_status:
            log(f"任务状态：{status}")
            last_status = status
        time.sleep(poll)
    raise RuntimeError(f"等待编译超时（{timeout_min} 分钟），Run id={run_id} 仍在进行。")


def fetch_log(run_id):
    """下载 job 日志文本，失败返回 None。"""
    try:
        s, data = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs")
        if s != 200 or not data.get("jobs"):
            return None
        jid = data["jobs"][0]["id"]
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(
            f"https://api.github.com/repos/{OWNER}/{REPO}/actions/jobs/{jid}/logs",
            headers=HEADERS,
        )
        try:
            opener.open(req, timeout=30)
            return None
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if not loc:
                return None
            with urllib.request.urlopen(loc, timeout=120) as r:
                return r.read().decode(errors="replace")
    except Exception:
        return None


def expected_sha_from_log(run_id):
    text = fetch_log(run_id)
    if not text:
        return None
    matches = re.findall(r"([0-9a-f]{64})\s+\S*sysupgrade\.bin", text)
    return matches[-1] if matches else None


def artifact_info(run_id):
    s, data = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/artifacts")
    if s != 200 or not data.get("artifacts"):
        raise RuntimeError("未找到构建产物 artifact")
    a = data["artifacts"][0]
    return a["id"], a["name"], a["size_in_bytes"]


def download_artifact(art_id, dest_zip):
    """获取重定向地址后用 curl 下载（对象存储下载用 curl 更稳定）。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/actions/artifacts/{art_id}/zip",
        headers=HEADERS,
    )
    try:
        opener.open(req, timeout=60)
        raise RuntimeError("artifact 下载未返回重定向")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if not loc:
            raise RuntimeError("artifact 下载缺少 Location")
    log("下载固件中（可能较慢，请耐心）...")
    subprocess.run(
        ["curl", "-sL", "--retry", "3", "--connect-timeout", "30",
         "--max-time", "600", "--speed-time", "40", "--speed-limit", "1024",
         "-o", dest_zip, loc],
        check=True,
    )
    if not os.path.exists(dest_zip) or os.path.getsize(dest_zip) < 1000:
        raise RuntimeError("artifact 下载失败或文件过小")


def install_firmware(zip_path, expected_sha):
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(".bin")]
        if not names:
            raise RuntimeError("artifact 中没有 .bin 文件")
        data = z.read(names[0])
    actual = hashlib.sha256(data).hexdigest()
    log(f"固件 SHA256：{actual}")
    if expected_sha and actual != expected_sha:
        raise RuntimeError(f"SHA256 不匹配！构建日志={expected_sha}，下载文件={actual}")
    if expected_sha:
        log("SHA256 与构建日志一致 ✓")

    target = os.path.join(FIRMWARE_DIR, FIRMWARE_NAME)
    if os.path.exists(target):
        backup = os.path.join(FIRMWARE_DIR, FIRMWARE_NAME.replace(".bin", "_prev.bin"))
        shutil.copy2(target, backup)
        log(f"旧固件已备份：{os.path.basename(backup)}")
    with open(target, "wb") as f:
        f.write(data)
    log(f"新固件已保存：{target}（{len(data)} 字节）")


def main():
    global TOKEN, HEADERS
    ap = argparse.ArgumentParser(description="Newifi D2 固件一键重建")
    ap.add_argument("--check", action="store_true", help="只检查差异，不推送不编译")
    ap.add_argument("--no-download", action="store_true", help="不下载固件")
    ap.add_argument("--force", action="store_true", help="即使无变化也强制编译")
    ap.add_argument("--mhz", choices=["1100", "1000"], default="1100", help="dispatch 时的频率")
    ap.add_argument("--poll", type=int, default=30, help="轮询间隔秒数")
    ap.add_argument("--timeout", type=int, default=120, help="等待编译最长分钟数")
    args = ap.parse_args()

    TOKEN = get_token()
    HEADERS = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "newifi-d2-rebuild",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    changed = check_and_push(check_only=args.check)
    if args.check:
        return

    before_id = latest_run_id()

    if changed > 0:
        log("检测到文件变化，push 会自动触发编译。")
    else:
        log("无文件变化" + ("，按 --force 强制编译。" if args.force else "，手动触发编译。"))

    if changed == 0 or args.force:
        s, resp = api(
            "POST",
            f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
            {"ref": BRANCH, "inputs": {"cpu_mhz": args.mhz}},
        )
        if s >= 300:
            raise RuntimeError(f"触发编译失败: {str(resp)[:300]}")
        log(f"已触发 workflow_dispatch（{args.mhz} MHz）")

    run_id = wait_new_run(before_id)

    s, run = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}")
    if s == 200:
        log(f"Run #{run.get('run_number')}：{run.get('html_url', '')}")

    conclusion = wait_run(run_id, args.poll, args.timeout)
    log("=== 步骤状态 ===")
    _, _, failed = step_summary(run_id)

    if conclusion != "success":
        if failed:
            log(f"失败步骤：{failed}")
        log("编译未成功，跳过下载。请查看 Actions 日志。")
        sys.exit(1)

    if args.no_download:
        log("按 --no-download 要求，跳过固件下载。")
        return

    art_id, art_name, art_size = artifact_info(run_id)
    log(f"产物：{art_name}（{art_size} 字节，id={art_id}）")
    expected = expected_sha_from_log(run_id)
    if expected:
        log(f"构建日志 SHA256：{expected}")

    zip_path = os.path.join(ROOT, ".firmware_download.zip")
    try:
        download_artifact(art_id, zip_path)
        install_firmware(zip_path, expected)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    log("=== 完成 ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已取消。")
        sys.exit(130)
    except Exception as e:
        log(f"\n错误：{e}")
        sys.exit(1)
