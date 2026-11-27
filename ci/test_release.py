#!/usr/bin/env python3
"""release.py 的纯函数回归：分组口径、标题规则、产物名解析。

跑法：python3 ci/test_release.py（stdlib unittest，无需安装依赖）
"""
import unittest
import unittest.mock

import release


def segments(*items):
    """把 (subject, body) 组成 git log 的分段格式。"""
    return [f"{subject}\n{body}".strip() for subject, body in items]


class GroupChangelogTest(unittest.TestCase):
    def setUp(self):
        release.PROJECT = "CalorieDiary"

    def test_six_groups_in_order(self):
        lines, unmatched = release.group_changelog(segments(
            ("feat: 支持批量导入", ""),
            ("fix: 修复断点续传", ""),
            ("perf: 首页提速", ""),
            ("revert: 回滚导入入口", ""),
        ))
        self.assertEqual(unmatched, [])
        self.assertEqual(lines, [
            "新增", "- 支持批量导入",
            "修复", "- 修复断点续传",
            "改进", "- 首页提速",
            "回滚", "- 回滚导入入口",
        ])

    def test_breaking_is_top_group_and_keeps_type(self):
        lines, _ = release.group_changelog(segments(("feat!: 移除旧版导入入口", "")))
        self.assertEqual(lines, ["破坏性变更", "- 移除旧版导入入口", "新增", "- 移除旧版导入入口"])

    def test_breaking_footer_counts(self):
        lines, _ = release.group_changelog(segments(
            ("fix: 调整同步协议", "BREAKING CHANGE: 服务端需同步升级"),
        ))
        self.assertEqual(lines[:2], ["破坏性变更", "- 调整同步协议"])

    def test_security_scope_is_top_group_and_fix(self):
        lines, _ = release.group_changelog(segments(
            ("fix(security): 修复 token 复用", "CVE-2026-0001"),
        ))
        self.assertEqual(lines, ["安全", "- 修复 token 复用", "修复", "- 修复 token 复用"])

    def test_hidden_types_and_fix_ci(self):
        lines, unmatched = release.group_changelog(segments(
            ("docs: 补充说明", ""),
            ("chore: 依赖升级", ""),
            ("ci: 调整流水线", ""),
            ("fix(ci): 修流水线", ""),
            ("refactor: 拆分模块", ""),
            ("test: 补测试", ""),
            ("build: 改构建", ""),
            ("style: 格式化", ""),
        ))
        self.assertEqual(lines, [])
        self.assertEqual(unmatched, [])

    def test_skip_trailer_and_subject(self):
        lines, unmatched = release.group_changelog(segments(
            ("feat: 内部开关", "Changelog: skip"),
            ("feat: 另一个内部改动 [skip changelog]", ""),
        ))
        self.assertEqual(lines, [])
        self.assertEqual(unmatched, [])

    def test_unmatched_reported_not_rendered(self):
        lines, unmatched = release.group_changelog(segments(
            ("Initial commit", ""),
            ("Update README", ""),
            ("actions: 改工作流", ""),
        ))
        self.assertEqual(lines, [])
        self.assertEqual(len(unmatched), 3)

    def test_auto_revert_title_is_revert_group(self):
        lines, unmatched = release.group_changelog(segments(
            ('Revert "fix(ci): changelog 区间基准"', "This reverts commit abc."),
        ))
        self.assertEqual(unmatched, [])
        self.assertEqual(lines, ["回滚", "- changelog 区间基准"])

    def test_emoji_stripped_and_deduped(self):
        lines, _ = release.group_changelog(segments(
            ("feat: 支持导入 \u2728 历史数据 \U0001F680", ""),
            ("feat: 支持导入 \u2728 历史数据 \U0001F680", ""),
        ))
        self.assertEqual(lines, ["新增", "- 支持导入 历史数据"])

    def test_scope_dropped(self):
        lines, _ = release.group_changelog(segments(("feat(ui): 骨架屏", "")))
        self.assertEqual(lines, ["新增", "- 骨架屏"])


class CheckSubjectTest(unittest.TestCase):
    def test_valid(self):
        for subject in ("feat: 支持导入", "fix(ui): 修复闪退", "feat!: 移除入口",
                        "ci: 同步模板", 'Revert "feat: x"', "Merge branch 'main'"):
            self.assertTrue(release.check_subject(subject), subject)

    def test_invalid(self):
        for subject in ("Fix: 大写类型", "feat：全角冒号", "feat:", "feat: 结尾有句号。",
                        "update readme", "feat:  " + "x" * 120):
            self.assertFalse(release.check_subject(subject), subject)

    def test_skip_marker_is_allowed(self):
        self.assertTrue(release.check_subject("随便写的标题 [skip changelog]"))


class ArtifactNameTest(unittest.TestCase):
    def setUp(self):
        release.PROJECT = "CalorieDiary"

    def test_apk_platform(self):
        name = f"{release.PROJECT}-1.0.2.2-android-arm64-v8a.apk"
        self.assertEqual(release.derive_platform(name, "1.0.2.2"), "android-arm64-v8a")

    def test_zip_platform_with_variant(self):
        release.PROJECT = "ColorApkExtractor"
        self.assertEqual(
            release.derive_platform("ColorApkExtractor-1.0.0.11-linux-x86_64-wsl.zip", "1.0.0.11"),
            "linux-x86_64-wsl",
        )

    def test_invalid_tail_rejected(self):
        with self.assertRaises(SystemExit):
            release.derive_platform("CalorieDiary-1.0.2.2-freebsd-apk.apk", "1.0.2.2")

    def test_artifact_name(self):
        self.assertEqual(
            release.artifact_name("1.0.2.2"),
            f"{release.PROJECT}-1.0.2.2-android-arm64-v8a.apk",
        )


class FetchVersionJsonTest(unittest.TestCase):
    """钉住三种服务器响应语义（旧 test-determine-version.sh 的假 curl 对应用例）。"""

    def setUp(self):
        release.PROJECT = "CalorieDiary"
        self._sleep = release.time.sleep
        release.time.sleep = lambda _s: None

    def tearDown(self):
        release.time.sleep = self._sleep

    def test_object_not_found_is_first_release(self):
        body = '{"code":500,"message":"failed to get obj: object not found","data":null}'
        with unittest.mock.patch.object(release, "http_get", return_value=body):
            self.assertIsNone(release.fetch_version_json("beta"))

    def test_ok_reads_raw_url_then_manifest(self):
        api = '{"code":200,"message":"success","data":{"raw_url":"https://example.invalid/version.json"}}'
        manifest = '{"versionName":"1.0.2.2","commit":"deadbeef"}'
        responses = iter([api, manifest])
        with unittest.mock.patch.object(release, "http_get", side_effect=lambda *a, **k: next(responses)):
            doc = release.fetch_version_json("beta")
        self.assertEqual(doc["versionName"], "1.0.2.2")
        self.assertEqual(doc["commit"], "deadbeef")

    def test_network_failure_fails_closed(self):
        with unittest.mock.patch.object(release, "http_get", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit):
                release.fetch_version_json("beta")

    def test_spa_html_fails_closed(self):
        with unittest.mock.patch.object(release, "http_get", return_value="<!doctype html><html>SPA</html>"):
            with self.assertRaises(SystemExit):
                release.fetch_version_json("beta")


class ManifestTest(unittest.TestCase):
    def setUp(self):
        release.PROJECT = "CalorieDiary"

    def test_manifest_shape(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            fname = f"{release.PROJECT}-1.0.2.2-android-arm64-v8a.apk"
            path = os.path.join(tmp, fname)
            open(path, "wb").write(b"apk-bytes")
            doc = release.build_manifest([path], "1.0.2.2", 1000202, "beta", "notes", "abc123")
        self.assertEqual(doc["schemaVersion"], 2)
        self.assertEqual(doc["versionName"], "1.0.2.2")
        self.assertEqual(doc["versionCode"], 1000202)
        self.assertEqual(doc["channel"], "beta")
        self.assertEqual(doc["commit"], "abc123")
        art = doc["artifacts"][0]
        self.assertEqual(art["platform"], "android-arm64-v8a")
        self.assertEqual(art["fileName"], fname)
        self.assertEqual(art["url"], f"https://storage.horatio.cn/{release.PROJECT}/beta/1.0.2.2/{fname}")
        self.assertEqual(art["size"], 9)
        self.assertEqual(len(art["sha256"]), 64)


class VersionCodeTest(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(release.version_code("1.0.2.0"), 1_000_200)
        self.assertEqual(release.version_code("1.2.34.99"), 1_020_000 + 3_400 + 99)


if __name__ == "__main__":
    unittest.main(verbosity=1)
