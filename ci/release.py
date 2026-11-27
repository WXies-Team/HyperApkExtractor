#!/usr/bin/env python3
"""发版流水线统一入口（纯标准库；签名用 cryptography，SSH/构建用系统命令）。

原来的 8 个 shell 脚本（determine-version / gen-changelog / prepare-release /
pack-all / upload-openlist / sign-manifest / rollback-openlist / build-server /
deploy-server）合并成一个带子命令的 CLI：同一套日志、同一套错误处理、同一份
OpenList 协议实现，纯函数部分可单测。

子命令：
  version        算 VERSION_NAME / VERSION_CODE（写 GITHUB_ENV）
  changelog      生成 changelog.txt（六组、Changelog: skip、未分类 notice）
  prepare        生成 changelog + release.md，然后构建产物
  pack           工具仓 8 平台打包
  upload         上传产物 + 生成并签名 version.json + 更新下载卡片 + 归档
  rollback       通道回滚（预检 → 备份 → 原子切换 → 外部验证 → 失败恢复 → 审计）
  server-build   Go 服务端测试 + 静态构建 + 打包 tar
  server-deploy  上传 tar → 原子切 symlink → supervisorctl → 健康检查 → 失败回滚
  artifact-name  打印客户端产物名（供 workflow 拼 GitHub Release 资产）
  check-msg      校验提交/PR 标题（本地 hook 用 --strict，CI 用默认警告）
"""
import argparse
import base64
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

PROJECT = "HyperApkExtractor"
PRODUCT_CN = "HyperOS"
STORAGE = "https://storage.horatio.cn"
APK_NAME_SUFFIX = "android-arm64-v8a"
# 平台词形是白名单：旧的 `-android-arm64-release` 之类不能静默混进清单。
ALLOWED_PLATFORMS = {
    "android-arm64-v8a", "android-x86_64",
    "linux-arm64", "linux-x86_64", "linux-x86_64-wsl",
    "darwin-arm64", "darwin-x86_64", "windows-x86_64",
}
TYPE_HEADER = "feat|fix|perf|refactor|docs|test|build|ci|chore|revert|style"

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
CONVENTIONAL = re.compile(r"^([a-z]+)(?:\(([^)]*)\))?(!)?:\s*(.+)$")
AUTO_REVERT = re.compile(r'^Revert "(.*)"$')
BREAKING_FOOTER = re.compile(r"^BREAKING(?: |-)CHANGE:")
SKIP_TRAILER = re.compile(r"^\s*changelog\s*:\s*skip\s*$", re.IGNORECASE)
SKIP_SUBJECT = re.compile(r"\[skip changelog\]", re.IGNORECASE)
HEADER_LIMIT = 100
# 版本目录名是四段 X.Y.Z.B（stable 恒为 .0），也允许三段与预发布后缀。
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?")

# 六组：破坏性变更/安全双归属（既进置顶组，也保留原语义），其余单归属。
GROUPS = [("feat", "新增"), ("fix", "修复"), ("perf", "改进"), ("revert", "回滚")]
HIDDEN = {"docs", "chore", "ci", "build", "refactor", "test", "style"}
CARD_HTML = """<div style="max-width: 780px; margin: 0 auto; padding: 4px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
<div style="text-align: center; margin: 16px 0 20px 0;">
<div style="font-size: 22px; font-weight: 700; margin-bottom: 6px;">HyperOS 下载中心</div>
<div style="font-size: 13px; opacity: 0.75;">两个版本通道相互独立 · 按需要选择，各通道按策略保留历史版本</div>
</div>
<div style="display: flex; flex-direction: column; gap: 14px;">
<a href="/HyperApkExtractor/stable/" style="display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-radius: 14px; border: 1px solid rgba(125,125,125,0.22); background: rgba(125,125,125,0.05); text-decoration: none; color: inherit; box-sizing: border-box;">
<div style="display: flex; flex-direction: column; gap: 6px; min-width: 0;">
<div style="display: flex; align-items: center; gap: 8px;">
<span style="font-size: 16px; font-weight: 600;">正式版 (Stable)</span>
<span style="font-size: 11px; padding: 2px 8px; border-radius: 20px; background: rgba(34,197,94,0.18); color: #16a34a; font-weight: 600;">推荐日常</span>
</div>
<div style="font-size: 13px; opacity: 0.75; line-height: 1.4;">经过充分验证的稳定版本，功能完善 · 保留最新 3 版</div>
</div>
<div style="font-size: 13px; font-weight: 600; color: #16a34a; white-space: nowrap; margin-left: 16px; flex-shrink: 0;">进入通道 →</div>
</a>
<a href="/HyperApkExtractor/beta/" style="display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-radius: 14px; border: 1px solid rgba(125,125,125,0.22); background: rgba(125,125,125,0.05); text-decoration: none; color: inherit; box-sizing: border-box;">
<div style="display: flex; flex-direction: column; gap: 6px; min-width: 0;">
<div style="display: flex; align-items: center; gap: 8px;">
<span style="font-size: 16px; font-weight: 600;">测试版 (Beta)</span>
<span style="font-size: 11px; padding: 2px 8px; border-radius: 20px; background: rgba(234,88,12,0.18); color: #ea580c; font-weight: 600;">先行尝鲜</span>
</div>
<div style="font-size: 13px; opacity: 0.75; line-height: 1.4;">每次 push 自动构建的新特性，欢迎体验与反馈 · 保留最新 2 版</div>
</div>
<div style="font-size: 13px; font-weight: 600; color: #ea580c; white-space: nowrap; margin-left: 16px; flex-shrink: 0;">进入通道 →</div>
</a>
</div>
<div style="text-align: center; margin: 20px 0 4px; font-size: 12px; opacity: 0.6;">版本号与更新说明由发布流水线自动生成 · 更新说明见各版本目录 README.md</div>
</div>
"""


# ---------------------------------------------------------------- 基础工具

def notice(msg):
    print("::notice::" + msg)


def warning(msg):
    print("::warning::" + msg)


def die(msg):
    print("::error::" + msg, file=sys.stderr)
    sys.exit(1)


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def out(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def sh(value):
    return shlex.quote(str(value))


def runner_temp():
    return os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()


def env(name, required=True):
    value = os.environ.get(name, "")
    if required and not value:
        die(f"{name} is required")
    return value


def write_env(pairs):
    path = os.environ.get("GITHUB_ENV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs.items():
            fh.write(f"{key}={value}\n")


# ---------------------------------------------------------------- OpenList 协议

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"{PROJECT}-release"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_version_json(channel):
    """读通道根 version.json；不存在返回 None；网络/格式错误重试 3 次后 fail closed。

    不能直连 /HyperApkExtractor/<channel>/version.json —— 那是 SPA 路由，返回前端 HTML。
    先取 /api/fs/get 的 data.raw_url，再读真正的 JSON。把「不存在」误判成「网络失败」
    会让首版永远发不出去；反过来会把 BUILD 归 1，发出低于线上的版本。
    """
    api = f"{STORAGE}/api/fs/get?path=" + urllib.parse.quote(f"/{PROJECT}/{channel}/version.json", safe="/")
    raw = ""
    for attempt in range(3):
        try:
            doc = json.loads(http_get(api))
            if "object not found" in str(doc.get("message") or ""):
                notice(f"{PROJECT}/{channel}/version.json 不存在，按首次发版处理")
                return None
            raw = (doc.get("data") or {}).get("raw_url") or ""
            if doc.get("code") == 200 and raw:
                break
            raise ValueError("unexpected AList response")
        except Exception:
            if attempt == 2:
                die(f"读取服务器 {PROJECT}/{channel}/version.json 失败（已重试 3 次）：无法确认上一版时拒绝发版")
            time.sleep((attempt + 1) * 2)
    for attempt in range(3):
        try:
            doc = json.loads(http_get(raw))
            if not doc.get("versionName"):
                raise ValueError("missing versionName")
            return doc
        except Exception:
            if attempt == 2:
                die(f"{PROJECT}/{channel} version.json 缺少 versionName（文件损坏或 raw_url 失效）")
            time.sleep((attempt + 1) * 2)
    return None


def fetch_version_name(channel):
    doc = fetch_version_json(channel)
    return doc["versionName"] if doc else ""


def fetch_prev_commit(channel):
    try:
        doc = fetch_version_json(channel)
    except SystemExit:
        raise
    return (doc or {}).get("commit", "")


# ---------------------------------------------------------------- SSH / SCP

class Remote:
    def __init__(self, known_hosts=None, key_name="openlist_key"):
        self.host = env("SERVER_HOST")
        self.user = env("SERVER_USER")
        self.port = os.environ.get("SSH_PORT", "22")
        self.key = os.path.join(runner_temp(), key_name)
        os.makedirs(os.path.dirname(self.key), exist_ok=True)
        with open(self.key, "w", encoding="utf-8") as fh:
            fh.write(env("SERVER_SSH_KEY") + "\n")
        os.chmod(self.key, 0o600)
        self.known_hosts = known_hosts
        self.opts = ["-i", self.key, "-p", self.port, "-o", "BatchMode=yes"]
        if known_hosts:
            self.opts += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
        else:
            self.opts += ["-o", "StrictHostKeyChecking=accept-new"]

    @property
    def target(self):
        return f"{self.user}@{self.host}"

    def run(self, command):
        run(["ssh", *self.opts, self.target, "bash -c " + sh(command)])

    def run_script(self, script, args):
        # 远端命令是单条 shell 字符串：逐个 quote，空参数才能保留成 ''（否则位置参数错位）。
        remote = "bash -s -- " + " ".join(shlex.quote(str(a)) for a in args)
        run(["ssh", *self.opts, self.target, remote], input=script, text=True)

    def scp(self, source, dest):
        run(["scp", "-i", self.key, "-P", self.port, *self.opts[4:], source, f"{self.target}:{dest}"])

    def close(self):
        try:
            os.remove(self.key)
        except FileNotFoundError:
            pass


def known_hosts_for(host, port):
    """优先用 SERVER_KNOWN_HOSTS；没有就 ssh-keyscan 一次（TOFU），并提示补 secret。"""
    path = os.path.join(runner_temp(), "known_hosts")
    pinned = os.environ.get("SERVER_KNOWN_HOSTS", "")
    if pinned:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(pinned + "\n")
        return path
    result = subprocess.run(["ssh-keyscan", "-p", str(port), "-T", "10", host], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        die(f"无法获取 {host} 主机密钥")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(result.stdout)
    warning("SERVER_KNOWN_HOSTS 未配置，本次用 ssh-keyscan 临时校验；建议补该 secret")
    return path


# ---------------------------------------------------------------- 版本

def version_code(name):
    try:
        major, minor, patch, build = (int(x) for x in name.split("."))
    except Exception:
        die(f"版本号无法解析: {name}")
    return major * 1_000_000 + minor * 10_000 + patch * 100 + build


def cmd_version(args):
    channel = args.channel
    if channel not in ("beta", "stable"):
        die(f"CHANNEL 必须是 beta 或 stable，收到: {channel}")

    if channel == "stable":
        if not args.tag:
            die("stable 发版必须提供 git tag")
        if not re.fullmatch(r"\d+\.\d+\.\d+\.0", args.tag):
            die(f"stable tag 必须是 X.Y.Z.0，收到: {args.tag}")
        name = args.tag
        current = ""
    else:
        if not os.path.isfile(args.version_file):
            die(f"缺少 {args.version_file}（内容应为 X.Y.Z，如 1.0.0）")
        xyz = "".join(open(args.version_file, encoding="utf-8").read().split())
        if not re.fullmatch(r"\d+\.\d+\.\d+", xyz):
            die(f"{args.version_file} 必须是 X.Y.Z，收到: {xyz}")
        lock_path = os.path.join(tempfile.gettempdir(), f"{PROJECT}-beta-build.lock")
        with open(lock_path, "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = fetch_version_name("beta")
            if current.startswith(xyz + "."):
                tail = current.rsplit(".", 1)[1]
                if not tail.isdigit():
                    die(f"服务器当前 beta 版本第四位非法: {current}")
                build = int(tail) + 1
            else:
                build = 1
        if not 1 <= build <= 99:
            die(f"BUILD 超出 1-99（当前 {build}）：请手动提升 {args.version_file} 的 PATCH 后重发")
        name = f"{xyz}.{build}"

    segments = name.split(".")
    if len(segments) != 4 or any(not s.isdigit() or int(s) > 99 for s in segments):
        die(f"版本号每段必须 0-99: {name}")
    code = version_code(name)

    if os.environ.get("SKIP_MONOTONIC_CHECK", "0") != "1":
        if channel == "beta" and current:
            old = version_code(current)
            if code <= old:
                die(f"versionCode 回退: {code} <= {old} ({current})")
        if channel == "stable":
            prev = fetch_version_name("stable")
            if prev and code <= version_code(prev):
                die(f"stable versionCode 回退: {code} <= {version_code(prev)} ({prev})")

    print(f"channel={channel} version_name={name} version_code={code}")
    write_env({"CHANNEL": channel, "VERSION": name, "VERSION_NAME": name, "VERSION_CODE": code})


# ---------------------------------------------------------------- changelog

def clean_text(text):
    text = EMOJI.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def parse_commit(segment):
    """把一条提交解析成 (类型, 描述, 是否破坏性, 是否安全)，供分组与单测复用。"""
    lines = segment.splitlines()
    subject = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:])
    if SKIP_SUBJECT.search(subject) or any(SKIP_TRAILER.match(line) for line in lines[1:]):
        return None
    auto = AUTO_REVERT.match(subject)
    if auto:
        inner = auto.group(1)
        match = CONVENTIONAL.match(inner)
        return ("revert", match.group(4).strip() if match else inner, False, False)
    match = CONVENTIONAL.match(subject)
    if not match:
        return ("__unmatched__", subject, False, False)
    typ, scope, bang, desc = match.group(1), match.group(2) or "", match.group(3), match.group(4).strip()
    if typ in HIDDEN or (typ == "fix" and scope == "ci"):
        return None
    breaking = bool(bang) or any(BREAKING_FOOTER.match(line) for line in lines[1:])
    security = typ == "fix" and (scope == "security" or re.search(r"CVE-\d+", body))
    return (typ, desc, breaking, security)


def group_changelog(segments):
    """返回 (输出行列表, 被跳过的未分类标题列表)。纯函数，单测直测。"""
    buckets = {key: [] for key, _ in GROUPS}
    breaking, security, unmatched = [], [], []
    for segment in segments:
        parsed = parse_commit(segment)
        if parsed is None:
            continue
        typ, text, is_breaking, is_security = parsed
        if typ == "__unmatched__":
            unmatched.append(text)
            continue
        text = clean_text(text)
        if not text:
            continue
        if typ not in buckets:
            unmatched.append(text)
            continue
        buckets[typ].append(text)
        if is_breaking:
            breaking.append(text)
        if is_security:
            security.append(text)

    def push(lines, label, items):
        seen, picked = set(), []
        for item in items:
            if item not in seen:
                seen.add(item)
                picked.append(item)
        if picked:
            lines.append(label)
            lines.extend("- " + item for item in picked)

    lines = []
    push(lines, "破坏性变更", breaking)
    push(lines, "安全", security)
    for key, label in GROUPS:
        push(lines, label, buckets[key])
    return lines, unmatched


def git_log(prev_commit):
    rng = "HEAD"
    if prev_commit and subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{prev_commit}^{{commit}}"], capture_output=True
    ).returncode == 0:
        rng = f"{prev_commit}..HEAD"
    raw = out(["git", "log", "--no-merges", "--pretty=format:%s%n%b%x00", rng])
    return [s.strip() for s in raw.split("\0") if s.strip()]


def write_changelog(prev_commit, path):
    lines, unmatched = group_changelog(git_log(prev_commit))
    if unmatched:
        head = "；".join(unmatched[:5])
        notice(f"跳过 {len(unmatched)} 条未分类提交：{head}")
    if not lines:
        lines = ["本次更新未附带更新说明"]
        notice("区间无用户可见变更，使用中性兜底文案")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"changelog 已生成: {path} ({len(lines)} 行)")


def cmd_changelog(args):
    write_changelog(args.prev_commit or "", args.out)


# ---------------------------------------------------------------- 构建

def build_app(version, code):
    lock_path = os.path.join(tempfile.gettempdir(), f"{PROJECT}-kotlin-release.lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run([
            "./gradlew", ":shared:jvmTest", ":composeApp:assembleRelease",
            "-x", "lint", "-x", "lintVitalRelease", "-x", "lintVitalAnalyzeRelease",
            "--no-parallel", "--max-workers=1",
            f"-PVERSION_NAME={version}", f"-PVERSION_CODE={code}",
        ])
    src = "composeApp/build/outputs/apk/release/composeApp-release.apk"
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        die(f"构建产物不存在: {src}")
    dst = artifact_name(version)
    shutil.copyfile(src, dst)
    print(f"产物就绪: {dst} (versionCode={code})")


PLATFORMS = [
    ("windows-x86_64", "payload-dumper-go_*_windows_amd64.tar.gz", "payload-dumper-go.exe",
     "erofs-tools-*Cygwin_x86_64*.zip", "", ["cygwin1.dll", "extract.erofs.exe"]),
    ("linux-x86_64", "payload-dumper-go_*_linux_amd64.tar.gz", "payload-dumper-go",
     "erofs-tools-*Linux_x86_64*.zip", "", ["extract.erofs"]),
    ("linux-arm64", "payload-dumper-go_*_linux_arm64.tar.gz", "payload-dumper-go",
     "erofs-tools-*Linux_aarch64*.zip", "", ["extract.erofs"]),
    ("darwin-x86_64", "payload-dumper-go_*_darwin_amd64.tar.gz", "payload-dumper-go",
     "erofs-utils-*Darwin_x86_64*.zip", "v1.8.10-251217", ["extract.erofs"]),
    ("darwin-arm64", "payload-dumper-go_*_darwin_arm64.tar.gz", "payload-dumper-go",
     "erofs-utils-*Darwin_aarch64*.zip", "v1.8.10-251217", ["extract.erofs"]),
    ("android-arm64-v8a", "payload-dumper-go_*_linux_arm64.tar.gz", "payload-dumper-go",
     "erofs-tools-*Android_arm64*.zip", "", ["extract.erofs"]),
    ("android-x86_64", "payload-dumper-go_*_linux_amd64.tar.gz", "payload-dumper-go",
     "erofs-tools-*Android_x86_64*.zip", "", ["extract.erofs"]),
    ("linux-x86_64-wsl", "payload-dumper-go_*_linux_amd64.tar.gz", "payload-dumper-go",
     "erofs-tools-*WSL_x86_64*.zip", "", ["extract.erofs"]),
]


def pack(version):
    os.makedirs("output", exist_ok=True)
    for platform, dumper_glob, dumper_file, erofs_glob, erofs_tag, erofs_files in PLATFORMS:
        target = f"{PROJECT}-{platform}"
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(os.path.join(target, "tools"), exist_ok=True)
        for entry in glob.glob("repo/*"):
            dest = os.path.join(target, os.path.basename(entry))
            if os.path.isdir(entry):
                shutil.copytree(entry, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, dest)
        with tempfile.TemporaryDirectory() as tmp:
            run(["gh", "release", "download", "--repo", "ssut/payload-dumper-go",
                 "--pattern", dumper_glob, "--clobber"], cwd=tmp)
            cmd = ["gh", "release", "download"]
            if erofs_tag:
                cmd.append(erofs_tag)
            cmd += ["--repo", "sekaiacg/erofs-tools", "--pattern", erofs_glob, "--clobber"]
            run(cmd, cwd=tmp)
            tools = os.path.abspath(os.path.join(target, "tools"))
            for name in os.listdir(tmp):
                path = os.path.join(tmp, name)
                if name.endswith(".zip"):
                    run(["unzip", "-o", "-j", path, *erofs_files, "-d", tools])
                elif name.endswith(".tar.gz"):
                    run(["tar", "-xzf", path, "-C", tools, dumper_file])
        run(["7z", "a", "-y", os.path.abspath(f"{PROJECT}-{platform}.zip"), "./*"],
            cwd=target, stdout=subprocess.DEVNULL)
        shutil.move(f"{PROJECT}-{platform}.zip", f"output/{PROJECT}-{version}-{platform}.zip")
        print(f"packed: output/{PROJECT}-{version}-{platform}.zip")
    if not glob.glob(f"output/{PROJECT}-{version}-*.zip"):
        die("no platform archives produced")


def cmd_pack(args):
    pack(args.version)


def cmd_prepare(args):
    prev = fetch_prev_commit(args.channel)
    write_changelog(prev, "changelog.txt")
    shutil.copyfile("changelog.txt", "release.md")
    if args.kind == "app":
        build_app(args.version, args.version_code)
    else:
        pack(args.version)
        print(f"8 平台产物就绪 (channel={args.channel} versionCode={args.version_code})")


def artifact_name(version):
    return f"{PROJECT}-{version}-{APK_NAME_SUFFIX}.apk"


def cmd_artifact_name(args):
    print(artifact_name(args.version))


# ---------------------------------------------------------------- 上传

def sign_manifest(path):
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        die("缺少 cryptography（签名需要）：pip install cryptography")
    pem = base64.b64decode(env("UPDATER_PRIVATE_KEY"))
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        die("UPDATER_PRIVATE_KEY 必须是 ECDSA P-256 (prime256v1) 私钥")
    data = open(path, "rb").read()
    der = key.sign(data, ec.ECDSA(hashes.SHA256()))
    with open(path + ".sig", "w", encoding="utf-8") as fh:
        fh.write(base64.b64encode(der).decode() + "\n")
    print(f"signed {path} -> {path}.sig (ECDSA P-256 / DER)")


def derive_platform(filename, version):
    prefix = f"{PROJECT}-{version}-"
    if not filename.startswith(prefix) or not filename.endswith(".apk") and not filename.endswith(".zip"):
        die(f"产物名不符合 HyperApkExtractor-<version>-<platform>.<ext>: {filename}")
    tail = filename[len(prefix):].rsplit(".", 1)[0]
    if tail not in ALLOWED_PLATFORMS:
        die(f"产物名尾部不是合法 platform: {tail}（允许: {', '.join(sorted(ALLOWED_PLATFORMS))}）")
    return tail


ARCHIVE_SCRIPT = r"""
set -Eeuo pipefail
TARGET_DIR="$1"; CURRENT_VERSION="$2"; KEEP_VERSIONS="$3"
ARCHIVE_DIR="$TARGET_DIR/以前的版本"
rm -f "$TARGET_DIR"/*.apk "$TARGET_DIR"/*.zip "$TARGET_DIR"/*.txt 2>/dev/null || true
for dir in "$TARGET_DIR"/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  if [ "$name" != "$CURRENT_VERSION" ] && [ "$name" != "以前的版本" ]; then
    mkdir -p "$ARCHIVE_DIR"; rm -rf "$ARCHIVE_DIR/$name"; mv "$dir" "$ARCHIVE_DIR/"
  fi
done
MAX_ARCHIVED=$((KEEP_VERSIONS - 1))
if [ "$MAX_ARCHIVED" -le 0 ]; then
  rm -rf "$ARCHIVE_DIR"
elif [ -d "$ARCHIVE_DIR" ]; then
  mapfile -t OLD_DIRS < <(ls -1td "$ARCHIVE_DIR"/*/ 2>/dev/null || true)
  COUNT="${#OLD_DIRS[@]}"
  if [ "$COUNT" -gt "$MAX_ARCHIVED" ]; then
    for ((i=MAX_ARCHIVED; i<COUNT; i++)); do rm -rf "${OLD_DIRS[i]}"; done
  fi
fi
"""


def build_manifest(artifacts, version, code, channel, notes, commit):
    """生成 version.json 的文档（schemaVersion 2，platform 与文件名尾部同形）。纯函数，单测直测。"""
    entries = []
    for path in artifacts:
        data = open(path, "rb").read()
        name = os.path.basename(path)
        entries.append({
            "platform": derive_platform(name, version),
            "fileName": name,
            "url": f"{STORAGE}/{PROJECT}/{channel}/{version}/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return {
        "schemaVersion": 2,
        "versionName": version,
        "versionCode": int(code),
        "channel": channel,
        "publishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": commit,
        "notes": notes,
        "artifacts": entries,
    }


def cmd_upload(args):
    code = args.version_code or version_code(args.version)
    base = os.path.join(env("OPENLIST_STORAGE_DIR"), PROJECT)
    version_dir = f"{base}/{args.channel}/{args.version}"
    keep = 2 if args.channel == "beta" else 3
    remote = Remote()
    try:
        print(f"== [OpenList] 准备版本目录: {version_dir} ==")
        remote.run(f"mkdir -p {sh(version_dir)}")

        artifacts = sorted(glob.glob(os.path.join(args.artifact_dir, f"{args.artifact_prefix}-{args.version}-*")))
        artifacts = [p for p in artifacts if p.endswith(".apk") or p.endswith(".zip")]
        if not artifacts:
            die(f"未找到待上传产物: {args.artifact_dir}/{args.artifact_prefix}-{args.version}-*")
        for path in artifacts:
            remote.scp(path, version_dir + "/")
        print(f"上传产物 {len(artifacts)} 个")

        if os.path.isfile("release.md"):
            remote.scp("release.md", version_dir + "/README.md")
            remote.scp("release.md", f"{base}/{args.channel}/README.md")

        print("== [OpenList] 生成并签名 version.json ==")
        notes = open("changelog.txt", encoding="utf-8").read() if os.path.isfile("changelog.txt") else ""
        manifest = build_manifest(artifacts, args.version, code, args.channel,
                                  notes, env("GITHUB_SHA", required=False))
        tmp = os.path.join(runner_temp(), "version.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        sign_manifest(tmp)
        print(f"version.json: {args.version} code={code} artifacts={len(manifest['artifacts'])}")
        remote.scp(tmp, f"{base}/{args.channel}/version.json")
        remote.scp(tmp + ".sig", f"{base}/{args.channel}/version.json.sig")
        remote.scp(tmp, version_dir + "/version.json")
        remote.scp(tmp + ".sig", version_dir + "/version.json.sig")
        for leftover in (tmp, tmp + ".sig"):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass

        card = os.path.join(runner_temp(), "storage-card.md")
        with open(card, "w", encoding="utf-8") as fh:
            fh.write(CARD_HTML)
        remote.run(f"mkdir -p {sh(base + '/beta')} {sh(base + '/stable')}")
        remote.scp(card, f"{base}/README.md")

        print(f"== [OpenList] 归档旧版本 (通道: {args.channel}, 保留: {keep}) ==")
        remote.run_script(ARCHIVE_SCRIPT, [f"{base}/{args.channel}", args.version, keep])
        print("== [OpenList] 完成 ==")
    finally:
        remote.close()


# ---------------------------------------------------------------- 回滚

ROLLBACK_SCRIPT = r"""
set -Eeuo pipefail
TARGET_DIR="$1"; ARCHIVE_DIR="$2"; TARGET_VERSION="$3"; DROP_VERSION="$4"
DRY_RUN="$5"; PURGE="$6"; AUDIT_LOG="$7"; PRODUCT="$8"; CHANNEL="$9"
SNAPDIR="$TARGET_DIR/$TARGET_VERSION"
ARCHIVED="$ARCHIVE_DIR/$TARGET_VERSION"

RESTORE_SRC=""
if [ -d "$ARCHIVED" ]; then
  RESTORE_SRC="$ARCHIVED"
elif [ -f "$SNAPDIR/version.json" ] && [ -n "$(ls -A "$SNAPDIR"/*.apk "$SNAPDIR"/*.zip 2>/dev/null)" ]; then
  RESTORE_SRC="$SNAPDIR"
else
  echo "::error::目标版本 $TARGET_VERSION 在归档与通道根均不存在或产物缺失，拒绝回滚"; exit 1
fi
test -f "$RESTORE_SRC/version.json" || { echo "::error::目标缺 version.json"; exit 1; }
test -f "$RESTORE_SRC/README.md"   || { echo "::error::目标缺 README.md"; exit 1; }

ARTIFACT=$(python3 -c 'import json,sys,re; d=json.load(open(sys.argv[1])); arts=d.get("artifacts",[]); n=arts[0].get("fileName","") if arts else ""; print(n if re.fullmatch(r"[0-9A-Za-z._-]+",n) else "")' "$RESTORE_SRC/version.json")
[ -n "$ARTIFACT" ] || { echo "::error::version.json 的 artifacts[0].fileName 非法"; exit 1; }

echo "[rollback] 目标来源: $RESTORE_SRC / 产物: $ARTIFACT"
[ "$DRY_RUN" = "true" ] && { echo "[rollback] DRY RUN 预检通过，未修改"; exit 0; }

BACKUP="$ARCHIVE_DIR/.rollback-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP"
OLD_VER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("versionName","?"))' "$TARGET_DIR/version.json" 2>/dev/null || echo '?')
for f in version.json version.json.sig README.md; do [ -f "$TARGET_DIR/$f" ] && cp -p "$TARGET_DIR/$f" "$BACKUP/$f" || true; done

restore_pointer() {
  for f in version.json version.json.sig README.md; do [ -f "$BACKUP/$f" ] && mv -f "$BACKUP/$f" "$TARGET_DIR/$f" || true; done
  echo "[rollback] 已恢复回滚前指针"
}
trap 'restore_pointer' ERR

if [ "$RESTORE_SRC" = "$ARCHIVED" ]; then
  rm -rf "$SNAPDIR" && mv "$ARCHIVED" "$SNAPDIR"
fi
for f in version.json version.json.sig README.md; do
  [ -f "$SNAPDIR/$f" ] || continue
  cp "$SNAPDIR/$f" "$TARGET_DIR/$f.tmp.$$" && mv -f "$TARGET_DIR/$f.tmp.$$" "$TARGET_DIR/$f"
done
trap - ERR

URL="https://storage.horatio.cn/$PRODUCT/$CHANNEL/$TARGET_VERSION/$ARTIFACT"
if ! curl -fsS --max-time 20 -o /dev/null "$URL"; then
  echo "::error::切换后验证失败：下载链接不可用 $URL，已自动恢复指针"; restore_pointer; exit 1
fi
CURRENT_VER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("versionName",""))' "$TARGET_DIR/version.json")
[ "$CURRENT_VER" = "$TARGET_VERSION" ] || { echo "::error::指针版本不一致，已恢复"; restore_pointer; exit 1; }

if [ -n "$DROP_VERSION" ] && [ "$DROP_VERSION" != "$TARGET_VERSION" ]; then
  for loc in "$TARGET_DIR/$DROP_VERSION" "$ARCHIVE_DIR/$DROP_VERSION"; do
    [ -e "$loc" ] || continue
    if [ "$PURGE" = "true" ]; then
      rm -rf "$loc"; echo "[rollback] 已彻底删除 $loc"
    else
      mkdir -p "$ARCHIVE_DIR/已撤回"; mv "$loc" "$ARCHIVE_DIR/已撤回/$(basename "$loc")-$(date -u +%Y%m%dT%H%M%SZ)"
      echo "[rollback] 已归档撤回版本 $loc（未彻底删除）"
    fi
  done
fi

printf '%s\t%s\t%s\t%s->%s\tdrop=%s\tpurge=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${GITHUB_ACTOR:-local}" "$CHANNEL" "$OLD_VER" "$TARGET_VERSION" "$DROP_VERSION" "$PURGE" >> "$AUDIT_LOG"
echo "[rollback] 完成: $PRODUCT/$CHANNEL -> $TARGET_VERSION；审计已写入 $AUDIT_LOG"
"""


def cmd_rollback(args):
    channel = args.channel or os.environ.get("CHANNEL", "")
    target = args.target_version or os.environ.get("TARGET_VERSION", "")
    drop = args.drop_version or os.environ.get("DROP_VERSION", "")
    dry = "true" if args.dry_run else os.environ.get("DRY_RUN", "false")
    purge = "true" if args.purge else os.environ.get("PURGE", "false")
    confirm = args.confirm or os.environ.get("CONFIRM", "")
    if channel not in ("beta", "stable"):
        die(f"CHANNEL 必须是 beta 或 stable，收到: {channel}")
    if not target:
        die("TARGET_VERSION is required")
    if not VERSION_RE.fullmatch(target):
        die(f"TARGET_VERSION 格式非法: {target}")
    if drop:
        if not VERSION_RE.fullmatch(drop):
            die(f"DROP_VERSION 格式非法: {drop}")
        if drop == target:
            die("DROP_VERSION 不能等于 TARGET_VERSION")
        if confirm != "ROLLBACK":
            die("删除版本需要 CONFIRM=ROLLBACK（防误删）")

    known = known_hosts_for(env("SERVER_HOST"), os.environ.get("SSH_PORT", "22"))
    remote = Remote(known_hosts=known)
    try:
        base = os.path.join(env("OPENLIST_STORAGE_DIR"), PROJECT)
        target_dir = f"{base}/{channel}"
        print(f"== [Rollback] product={PROJECT} channel={channel} target={target} dry_run={dry} ==")
        remote.run_script(ROLLBACK_SCRIPT, [
            target_dir, f"{target_dir}/以前的版本", target, drop, dry, purge,
            f"{base}/rollback.log", PROJECT, channel,
        ])
        print("== [Rollback] 全部完成 ==")
    finally:
        remote.close()


# ---------------------------------------------------------------- 服务端

def cmd_server_build(_args):
    server = os.path.join(os.getcwd(), "server")
    stage = os.path.join("dist", "go-stage")
    out_tar = os.path.join("dist", "server.tar")
    print("== go test ==")
    run(["go", "test", "./..."], cwd=server)
    print("== go build（CGO_ENABLED=0 出静态二进制）==")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(os.path.join(stage, "server", "bin"))
    envp = dict(os.environ, CGO_ENABLED="0")
    run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o",
         os.path.join("..", stage, "server", "bin", "server"), "./cmd/server"], cwd=server, env=envp)
    os.makedirs(os.path.dirname(out_tar), exist_ok=True)
    run(["tar", "-cf", out_tar, "-C", stage, "server"])
    shutil.rmtree(stage, ignore_errors=True)
    listing = out(["tar", "-tf", out_tar]).splitlines()
    roots = {line.split("/")[0] for line in listing if line}
    if roots != {"server"}:
        die(f"tar 首层应只有 server/，实际是：{sorted(roots)}")
    if "server/bin/server" not in listing:
        die("tar 里缺少 server/bin/server——部署脚本 chmod +x bin/server 会失败")
    print(f"== 产物 ==\n{out_tar} ({os.path.getsize(out_tar)} bytes)")


def cmd_server_deploy(args):
    tar_file = args.tar
    if not os.path.isfile(tar_file):
        die(f"tar 不存在: {tar_file}")
    base = env("BASE_DIR")
    app_port = os.environ.get("APP_PORT", "8866")
    ctl = os.environ.get("SUPERVISORCTL", "supervisorctl")
    program = os.environ.get("PROGRAM", PROJECT)
    keep = int(os.environ.get("KEEP_RELEASES", "5"))
    release = out(["git", "rev-parse", "--short=8", "HEAD"]).strip()
    remote = Remote(key_name="deploy_key")
    current = ""
    try:
        print("== upload ==")
        remote.run(f"mkdir -p {sh(base + '/releases/' + release)}")
        remote.scp(tar_file, f"{base}/releases/{release}/server.tar")
        print("== extract ==")
        remote.run("set -e; cd " + sh(f"{base}/releases/{release}") +
                   " && tar -xf server.tar --strip-components=1 && rm server.tar && chmod +x bin/server")
        current = out(["ssh", *remote.opts, remote.target, f"readlink {sh(base + '/current')} || true"]).strip()
        print(f"current -> {current or '<none>'}  |  new: releases/{release}")

        def rollback():
            warning(f"deploy failed, rolling back to {current or 'none'}")
            if current:
                remote.run("ln -sfn " + sh(current) + " " + sh(base + "/current") +
                           " && " + ctl + " restart " + sh(program + ":*"))
                time.sleep(3)
                if subprocess.run(["ssh", *remote.opts, remote.target,
                                   f"curl -fsS http://127.0.0.1:{app_port}/health"], capture_output=True).returncode == 0:
                    print("rollback healthy")
                else:
                    warning("rollback health check failed — inspect server manually")
            else:
                remote.run(ctl + " stop " + sh(program + ":*"))
            sys.exit(1)

        try:
            print("== switch & restart ==")
            remote.run("ln -sfn " + sh(f"{base}/releases/{release}") + " " + sh(base + "/current") +
                       " && " + ctl + " restart " + sh(program + ":*"))
            remote.run(ctl + " status " + sh(program + ":*") + " || true")
            remote.run("tail -n 80 " + sh(base + "/current/logs/server.log") + " 2>/dev/null || tail -n 80 " +
                       sh(base + "/current/server.log") + " 2>/dev/null || journalctl -u " + PROJECT +
                       " -n 80 --no-pager 2>/dev/null || true")
            print("== health check ==")
            healthy = False
            for _ in range(15):
                if subprocess.run(["ssh", *remote.opts, remote.target,
                                   f"curl -fsS http://127.0.0.1:{app_port}/health"], capture_output=True).returncode == 0:
                    healthy = True
                    break
                time.sleep(2)
            if not healthy:
                print("::error::health check failed after 30s", file=sys.stderr)
                rollback()
        except subprocess.CalledProcessError:
            rollback()
        print(f"== prune (keep {keep}) ==")
        remote.run(f"cd {sh(base + '/releases')} && ls -1t | tail -n +{keep + 1} | xargs -r rm -rf")
        print(f"deployed: {release} -> {base}/current (health OK)")
    finally:
        remote.close()


# ---------------------------------------------------------------- 提交标题

def check_subject(subject):
    if AUTO_REVERT.match(subject) or subject.startswith("Merge "):
        return True
    if SKIP_SUBJECT.search(subject):
        return True
    if len(subject) > HEADER_LIMIT:
        return False
    match = re.fullmatch(rf"({TYPE_HEADER})(\([a-z0-9_.-]+\))?(!)?: \S.*", subject)
    return bool(match) and not subject.endswith((".", "。"))


def cmd_check_msg(args):
    strict = args.strict
    subjects = []
    if args.message:
        subjects = [args.message.splitlines()[0]]
    elif args.file:
        text = open(args.file, encoding="utf-8").read()
        lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
        subjects = [lines[0]] if lines else []
    elif args.range:
        raw = out(["git", "log", "--no-merges", "--pretty=format:%s%x00", args.range])
        subjects = [s for s in raw.split("\0") if s.strip()]
    else:
        die("check-msg 需要 --message / --file / --range 之一")

    bad = [s for s in subjects if not check_subject(s)]
    if not bad:
        print(f"commit message OK ({len(subjects)} 条)")
        return
    for subject in bad:
        message = f"标题不合规: {subject!r}（需要 type(scope)!: 描述，type 小写，ASCII 冒号，描述非空，≤{HEADER_LIMIT} 字符）"
        print(("::error::" if strict else "::warning::") + message, file=sys.stderr)
    if strict:
        sys.exit(1)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(prog="release.py", description="发版流水线统一入口")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("version", help="计算版本号并写 GITHUB_ENV")
    p.add_argument("--channel", required=True, choices=["beta", "stable"])
    p.add_argument("--version-file", default="VERSION")
    p.add_argument("--tag", default="")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("changelog", help="生成 changelog.txt")
    p.add_argument("--prev-commit", default="")
    p.add_argument("--out", default="changelog.txt")
    p.set_defaults(func=cmd_changelog)

    p = sub.add_parser("prepare", help="生成 changelog/release.md 并构建")
    p.add_argument("--kind", choices=["app", "tool"], required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--version-code", required=True, type=int)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("pack", help="工具仓 8 平台打包")
    p.add_argument("--version", required=True)
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("upload", help="上传产物并生成签名清单")
    p.add_argument("--channel", required=True, choices=["beta", "stable"])
    p.add_argument("--version", required=True)
    p.add_argument("--version-code", type=int, default=0, help="缺省时按版本号公式推导")
    p.add_argument("--artifact-dir", default=".")
    p.add_argument("--artifact-prefix", default=PROJECT)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("rollback", help="通道回滚")
    p.add_argument("--channel", default="")
    p.add_argument("--target-version", default="")
    p.add_argument("--drop-version", default="")
    p.add_argument("--confirm", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--purge", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("server-build", help="Go 服务端构建打包")
    p.set_defaults(func=cmd_server_build)

    p = sub.add_parser("server-deploy", help="部署 server.tar")
    p.add_argument("tar")
    p.set_defaults(func=cmd_server_deploy)

    p = sub.add_parser("artifact-name", help="打印客户端产物名")
    p.add_argument("--version", required=True)
    p.set_defaults(func=cmd_artifact_name)

    p = sub.add_parser("check-msg", help="校验提交/PR 标题")
    p.add_argument("--message", default="")
    p.add_argument("--file", default="")
    p.add_argument("--range", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_check_msg)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
