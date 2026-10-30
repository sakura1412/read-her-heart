"""End-to-end checks for the local requirement-card store."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backlog.py"
SKILL_ROOT = SCRIPT.parents[1]
BACKLOG_SPEC = importlib.util.spec_from_file_location("backlog", SCRIPT)
backlog = importlib.util.module_from_spec(BACKLOG_SPEC)
BACKLOG_SPEC.loader.exec_module(backlog)


class BacklogCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project-backlog"
        self.run_cli("init", "--project", "演示项目")

    def run_cli(self, *args, expected=0, root=None, io_encoding="utf-8"):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING=io_encoding)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root or self.root), *args],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def input_card(self, *, card_id="REQ-001", title="批量导入客户", priority=None):
        return {
            "schema_version": 1,
            "id": card_id,
            "title": title,
            "project": "演示项目",
            "mode": "normal",
            "scope": "本期只支持 CSV 导入",
            "status": "评估中",
            "priority": priority,
            "analysis_batch": "第一批分析",
            "dependencies": [],
            "decisions": [],
            "next_action": "核验现有导入失败率",
            "source_refs": ["2026-10-20｜客服工单 1-15｜/private/report"],
            "links": {},
            "analysis": {"current_stage": "01", "unknowns": ["失败率基线未知"]},
        }

    def write_input(self, card, filename="incoming.json"):
        path = self.base / filename
        path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        return path

    def save(self, card, *, expected_revision=None, note=None, expected=0, filename="incoming.json"):
        path = self.write_input(card, filename)
        args = ["save", "--input", str(path)]
        if expected_revision is not None:
            args.extend(["--expected-revision", str(expected_revision)])
        if note is not None:
            args.extend(["--note", note])
        return self.run_cli(*args, expected=expected)

    def show(self, card_id="REQ-001"):
        return json.loads(self.run_cli("show", "--id", card_id, "--format", "json").stdout)

    def show_revision(self, revision, card_id="REQ-001"):
        return json.loads(self.run_cli("show", "--id", card_id, "--revision", str(revision),
                                       "--format", "json").stdout)

    def test_create_then_reload_in_fresh_process_and_render_backlog(self):
        saved = json.loads(self.save(self.input_card()).stdout)
        self.assertEqual(saved["revision"], 1)
        reloaded = self.show()  # A separate CLI process, not Python memory.
        self.assertEqual(reloaded["project"], "演示项目")
        self.assertEqual(reloaded["revision"], 1)
        self.assertEqual(reloaded["history"][0]["note"], "创建卡片")
        self.assertTrue((self.root / ".read-her-heart-backlog.json").is_file())
        self.assertTrue((self.root / "cards" / "REQ-001.json").is_file())
        self.assertTrue((self.root / "revisions").is_dir())

        listing = json.loads(self.run_cli("list", "--format", "json").stdout)
        self.assertEqual(listing["project"], "演示项目")
        self.assertEqual(listing["items"][0]["priority"], None)
        self.assertIsNone(listing["items"][0]["latest_decision"])
        markdown = self.run_cli("list", "--format", "markdown").stdout
        self.assertIn("| ID | 标题 | 状态 | 优先级 | 依赖 | 最近决策 | 更新时间 | 下一步 |", markdown)
        self.assertIn("未确定", markdown)
        self.assertIn("核验现有导入失败率", markdown)
        self.assertIn("| REQ-001 |", markdown)

    def test_optimistic_update_and_append_only_decisions(self):
        self.save(self.input_card())
        card = self.show()
        card["status"] = "验证中"
        card["decisions"].append({
            "at": "2026-10-20T10:00:00+08:00", "outcome": "先验证",
            "reason": "当前基线未知", "source_refs": ["需求会议 2026-10-20"],
        })
        self.save(card, expected_revision=1, note="确认先做基线核验")
        current = self.show()
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["history"][-1]["note"], "确认先做基线核验")
        self.assertEqual(len(current["decisions"]), 1)
        self.assertEqual(json.loads(self.run_cli("list", "--format", "json").stdout)["items"][0]["latest_decision"]["outcome"], "先验证")

        stale = dict(card)
        stale["next_action"] = "已变更"
        result = self.save(stale, expected_revision=1, note="过时写入", expected=2)
        self.assertIn("版本已变化", result.stderr)
        self.assertEqual(self.show()["revision"], 2)

        tampered = self.show()
        tampered["decisions"][0]["reason"] = "覆盖旧理由"
        result = self.save(tampered, expected_revision=2, note="尝试改旧决策", expected=2)
        self.assertIn("只能追加", result.stderr)
        unchanged = self.show()
        result = self.save(unchanged, expected_revision=2, note="空更新", expected=2)
        self.assertIn("内容未变化", result.stderr)

    def test_rejects_unsafe_id_and_sensitive_nested_keys(self):
        result = self.save(self.input_card(card_id="../outside"), expected=2)
        self.assertIn("安全 ID", result.stderr)
        self.assertEqual(list((self.root / "cards").iterdir()), [])
        card = self.input_card()
        card["analysis"]["apiKey"] = "should-not-be-saved"
        result = self.save(card, expected=2)
        self.assertIn("敏感验证字段", result.stderr)
        self.assertNotIn("should-not-be-saved", result.stderr)
        self.assertEqual(list((self.root / "cards").iterdir()), [])

    def test_rejects_windows_device_ids_without_creating_cards(self):
        for card_id in ("CON", "con", "PrN", "AUX", "NUL", "COM1", "com9", "LPT1", "lPt9"):
            with self.subTest(card_id=card_id):
                result = self.save(self.input_card(card_id=card_id), expected=2)
                self.assertIn("Windows 保留设备名", result.stderr)
        self.assertEqual(list((self.root / "cards").iterdir()), [])
        self.save(self.input_card(card_id="CON-001"))
        self.assertEqual(self.show("CON-001")["id"], "CON-001")

    def test_utf8_pipes_override_legacy_encoding_for_success_and_errors(self):
        root = self.base / "中文需求库"
        options = {"root": root, "io_encoding": "cp1252:strict"}
        initialized = self.run_cli("init", "--project", "演示项目", **options)
        self.assertEqual(json.loads(initialized.stdout)["project"], "演示项目")
        card = self.input_card(title="导入客户 🚀")
        input_path = self.write_input(card)
        saved = self.run_cli("save", "--input", str(input_path), **options)
        self.assertEqual(json.loads(saved.stdout)["revision"], 1)
        shown = self.run_cli("show", "--id", card["id"], **options)
        self.assertEqual(json.loads(shown.stdout)["title"], card["title"])
        listing = self.run_cli("list", "--format", "markdown", **options)
        self.assertIn(card["title"], listing.stdout)
        missing = self.run_cli("show", "--id", "REQ-MISSING", expected=2, **options)
        self.assertIn("文件不存在", missing.stderr)

    def test_rejects_unmarked_or_skill_directory_and_other_project(self):
        blank = self.base / "unmarked"
        blank.mkdir()
        result = self.run_cli("list", root=blank, expected=2)
        self.assertIn("不是已初始化的需求库", result.stderr)
        result = self.run_cli("init", "--project", "演示项目", root=SKILL_ROOT, expected=2)
        self.assertIn("不能把公开 Skill", result.stderr)
        result = self.run_cli("save", "--input", str(SKILL_ROOT / "templates" / "requirement-card.json"), expected=2)
        self.assertIn("不能把公开 Skill", result.stderr)
        card = self.input_card()
        card["project"] = "其他项目"
        result = self.save(card, expected=2)
        self.assertIn("项目不一致", result.stderr)

    def test_markdown_escapes_cells_and_keeps_dependencies(self):
        card = self.input_card(title="客户 A | 客户 B <测试>")
        card["dependencies"] = [{
            "id": "REQ-002", "type": "delivery_prerequisite",
            "confirmation": "pending", "basis": "待研发确认",
        }]
        card["next_action"] = "访谈\n复核"
        self.save(card)
        markdown = self.run_cli("list", "--format", "markdown").stdout
        self.assertIn("客户 A \\| 客户 B &lt;测试&gt;", markdown)
        self.assertIn("REQ-002", markdown)
        self.assertIn("delivery_prerequisite/pending", markdown)
        self.assertIn("访谈<br>复核", markdown)
        card_markdown = self.run_cli("show", "--id", "REQ-001", "--format", "markdown").stdout
        self.assertIn("此 Markdown 由 JSON 卡片生成", card_markdown)
        self.assertIn("## 决策记录", card_markdown)

    def test_invalid_schema_and_status_do_not_create_cards(self):
        card = self.input_card()
        card["schema_version"] = 99
        self.assertIn("schema_version", self.save(card, expected=2).stderr)
        card = self.input_card()
        card["status"] = "已上线"
        self.assertIn("status", self.save(card, expected=2).stderr)
        self.assertEqual(list((self.root / "cards").iterdir()), [])

    def test_priority_requires_traceable_decision_and_latest_is_chronological(self):
        card = self.input_card(priority="高")
        self.assertIn("优先级", self.save(card, expected=2).stderr)
        card["decisions"] = [{
            "at": "2026-10-20T09:00:00+08:00", "outcome": "优先级高",
            "reason": "业务评审已批准", "source_refs": ["2026-10-20｜评审纪要｜/private/minutes"],
        }]
        self.save(card)
        current = self.show()
        current["priority"] = "中"
        self.assertIn("含来源引用", self.save(current, expected_revision=1, note="改优先级", expected=2).stderr)
        current["decisions"].append({
            "at": "2026-10-19T09:00:00+08:00", "outcome": "优先级中",
            "reason": "复核", "source_refs": ["复核纪要"],
        })
        self.assertIn("按时间先后", self.save(current, expected_revision=1, note="改优先级", expected=2).stderr)
        current["decisions"][-1]["at"] = "2026-10-20T12:00:00+08:00"
        self.save(current, expected_revision=1, note="评审调整优先级")
        listing = json.loads(self.run_cli("list", "--format", "json").stdout)
        self.assertEqual(listing["items"][0]["priority"], "中")
        self.assertEqual(listing["items"][0]["latest_decision"]["outcome"], "优先级中")

    def test_prior_revision_snapshot_recovers_full_old_content(self):
        card = self.input_card()
        card["dependencies"] = [{
            "id": "REQ-002", "type": "analysis_prerequisite",
            "confirmation": "pending", "basis": "待业务确认",
        }]
        self.save(card)
        original = self.show()
        changed = self.show()
        changed["scope"] = "本期同时支持 CSV 与 XLSX"
        changed["source_refs"] = ["2026-10-21｜现场观察｜/private/field-notes"]
        changed["dependencies"] = []
        changed["analysis"]["current_stage"] = "05"
        changed["decisions"].append({
            "at": "2026-10-21T09:00:00+08:00", "outcome": "扩大交付范围",
            "reason": "业务核准范围调整", "source_refs": ["2026-10-21｜业务评审纪要｜/private/review"],
        })
        self.save(changed, expected_revision=1, note="业务确认扩大范围")
        snapshot_path = self.root / "revisions" / "REQ-001" / "rev-1.json"
        self.assertTrue(snapshot_path.is_file())
        self.assertEqual(json.loads(snapshot_path.read_text(encoding="utf-8")), original)
        old = self.show_revision(1)
        self.assertEqual(old["scope"], "本期只支持 CSV 导入")
        self.assertEqual(old["source_refs"], card["source_refs"])
        self.assertEqual(old["dependencies"], card["dependencies"])
        self.assertEqual(old["analysis"]["current_stage"], "01")
        self.assertEqual(self.show()["revision"], 2)

        # A crash/retry may find an existing snapshot; a divergent one must never be overwritten.
        divergent = self.show()
        divergent["next_action"] = "改变下一步"
        archive = self.root / "revisions" / "REQ-001" / "rev-2.json"
        archive.write_text(json.dumps({"unexpected": "different"}), encoding="utf-8")
        result = self.save(divergent, expected_revision=2, note="更新下一步", expected=2)
        self.assertIn("历史快照与当前旧版不一致", result.stderr)
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), {"unexpected": "different"})
        self.assertEqual(self.show()["revision"], 2)

    def test_major_lifecycle_changes_require_new_sourced_decision(self):
        self.save(self.input_card())
        for field, value in (("mode", "mandatory"), ("status", "验证中"),
                             ("scope", "扩大到 XLSX"), ("priority", "高")):
            candidate = self.show()
            candidate[field] = value
            result = self.save(candidate, expected_revision=1, note="尝试未留痕变更", expected=2)
            self.assertIn("含来源引用", result.stderr)
            self.assertEqual(self.show()[field], self.input_card()[field])
        candidate = self.show()
        candidate["mode"] = "mandatory"
        candidate["status"] = "验证中"
        candidate["scope"] = "扩大到 XLSX"
        candidate["priority"] = "高"
        candidate["decisions"].append({
            "at": "2026-10-20T11:00:00+08:00", "outcome": "范围与状态调整",
            "reason": "用户明确改为强制执行", "source_refs": ["2026-10-20｜用户指令｜/private/approval"],
        })
        self.save(candidate, expected_revision=1, note="记录用户确认")
        self.assertEqual(self.show()["revision"], 2)

    def test_initial_nondefault_lifecycle_requires_sourced_decision(self):
        card = self.input_card()
        card["mode"] = "mandatory"
        self.assertIn("初始强制模式", self.save(card, expected=2).stderr)
        card = self.input_card()
        card["status"] = "实施中"
        self.assertIn("非评估状态", self.save(card, expected=2).stderr)


class BacklogWindowsLockTests(unittest.TestCase):
    """Exercise the Windows branch with a simulated msvcrt API on every host."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.msvcrt = mock.Mock(LK_LOCK=1, LK_UNLCK=2)
        patches = (
            mock.patch.object(backlog, "fcntl", None),
            mock.patch.dict(sys.modules, {"msvcrt": self.msvcrt}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_fd_closed(self, fd):
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_locks_empty_file_without_writing_before_acquisition(self):
        with mock.patch.object(backlog.os, "write", side_effect=AssertionError("unexpected lock-file write")):
            with backlog.store_lock(self.root):
                fd = self.msvcrt.locking.call_args[0][0]
                self.msvcrt.locking.assert_called_once_with(fd, self.msvcrt.LK_LOCK, 1)
                self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
                self.assertEqual(os.fstat(fd).st_size, 0)
        self.assertEqual(self.msvcrt.locking.call_args_list, [
            mock.call(fd, self.msvcrt.LK_LOCK, 1),
            mock.call(fd, self.msvcrt.LK_UNLCK, 1),
        ])
        self.assert_fd_closed(fd)

    def test_failed_acquisition_preserves_error_and_closes_without_unlocking(self):
        acquisition_error = OSError("acquire failed")
        self.msvcrt.locking.side_effect = [acquisition_error, OSError("unlock failed")]
        with self.assertRaises(OSError) as raised:
            with backlog.store_lock(self.root):
                self.fail("must not enter the protected operation without a lock")
        self.assertIs(raised.exception, acquisition_error)
        fd = self.msvcrt.locking.call_args[0][0]
        self.msvcrt.locking.assert_called_once_with(fd, self.msvcrt.LK_LOCK, 1)
        self.assert_fd_closed(fd)

    def test_failed_unlock_still_closes_descriptor(self):
        release_error = OSError("unlock failed")
        self.msvcrt.locking.side_effect = [None, release_error]
        with self.assertRaises(OSError) as raised:
            with backlog.store_lock(self.root):
                fd = self.msvcrt.locking.call_args[0][0]
        self.assertIs(raised.exception, release_error)
        self.assert_fd_closed(fd)


if __name__ == "__main__":
    unittest.main()
