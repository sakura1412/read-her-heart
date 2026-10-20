#!/usr/bin/env python3
"""Keep one project's requirement cards in a local, explicit JSON backlog.

The JSON files are the source of truth. Markdown is generated on demand. This
tool does not access the network or synchronize business data to a Skill repo.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback below
    fcntl = None


STORE_MARKER = ".read-her-heart-backlog.json"
STORE_KIND = "read-her-heart-backlog"
SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
STATUSES = {"评估中", "验证中", "实施中", "已暂停"}
MODES = {"normal", "mandatory"}
DEPENDENCY_TYPES = {
    "analysis_prerequisite",
    "delivery_prerequisite",
    "soft",
    "conflict",
    "shared_resource",
}
CONFIRMATIONS = {"confirmed", "pending"}
CARD_REQUIRED = {
    "schema_version", "id", "title", "project", "mode", "scope", "status",
    "priority", "dependencies", "decisions", "next_action", "source_refs",
}
CARD_OPTIONAL = {"analysis_batch", "links", "analysis", "revision", "updated_at", "history"}
SENSITIVE_KEY_PARTS = (
    "password", "passwd", "token", "apikey", "secret", "cookie",
    "credential", "authorization", "verificationcode", "otp", "验证码",
)


class BacklogError(Exception):
    """An expected, user-actionable data or store error."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_text(value, label, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise BacklogError(f"{label} 必须是{'可为空的' if allow_empty else '非空'}字符串")


def valid_id(value, label="id"):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise BacklogError(f"{label} 必须是以英文字母开头、仅含英数字/下划线/连字符的安全 ID（最多 64 字符）")


def valid_refs(value, label):
    if not isinstance(value, list):
        raise BacklogError(f"{label} 必须是字符串数组")
    for item in value:
        valid_text(item, label)


def reject_sensitive_keys(value):
    """Inspect keys, not prose; values can legitimately discuss credentials."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BacklogError("JSON 对象键名必须是字符串")
            normalized = re.sub(r"[\s_\-]", "", key).lower()
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise BacklogError("卡片含凭证或敏感验证字段；请移除该字段，仅保留安全的来源引用")
            reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive_keys(child)


def valid_timestamp(value, label):
    valid_text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacklogError(f"{label} 必须是带时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BacklogError(f"{label} 必须包含时区")
    return parsed


def validate_card(card, project):
    if not isinstance(card, dict):
        raise BacklogError("卡片根节点必须是 JSON 对象")
    reject_sensitive_keys(card)
    missing = CARD_REQUIRED - card.keys()
    unknown = card.keys() - CARD_REQUIRED - CARD_OPTIONAL
    if missing:
        raise BacklogError("卡片缺少必填字段：" + ", ".join(sorted(missing)))
    if unknown:
        raise BacklogError("卡片含未知字段：" + ", ".join(sorted(unknown)))
    if type(card["schema_version"]) is not int or card["schema_version"] != SCHEMA_VERSION:
        raise BacklogError("不支持的卡片 schema_version；此脚本仅支持 1")
    valid_id(card["id"])
    valid_text(card["title"], "title")
    valid_text(card["project"], "project")
    if card["project"] != project:
        raise BacklogError("卡片 project 与此需求库的项目不一致")
    if not isinstance(card["mode"], str) or card["mode"] not in MODES:
        raise BacklogError("mode 只允许 normal 或 mandatory")
    valid_text(card["scope"], "scope")
    if not isinstance(card["status"], str) or card["status"] not in STATUSES:
        raise BacklogError("status 只允许评估中、验证中、实施中、已暂停")
    if card["priority"] is not None:
        valid_text(card["priority"], "priority")
    valid_text(card["next_action"], "next_action", allow_empty=True)
    valid_refs(card["source_refs"], "source_refs")
    if "analysis_batch" in card and card["analysis_batch"] is not None:
        valid_text(card["analysis_batch"], "analysis_batch")
    if "links" in card:
        if not isinstance(card["links"], dict):
            raise BacklogError("links 必须是对象")
        for key, value in card["links"].items():
            valid_text(key, "links 键名")
            valid_text(value, "links 值")
    if "analysis" in card and not isinstance(card["analysis"], dict):
        raise BacklogError("analysis 必须是对象")

    dependencies = card["dependencies"]
    if not isinstance(dependencies, list):
        raise BacklogError("dependencies 必须是数组")
    seen_dependencies = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"id", "type", "confirmation", "basis"}:
            raise BacklogError("每个依赖必须包含 id、type、confirmation、basis")
        valid_id(dependency["id"], "依赖 id")
        if dependency["id"] == card["id"]:
            raise BacklogError("卡片不能依赖自身")
        if not isinstance(dependency["type"], str) or dependency["type"] not in DEPENDENCY_TYPES:
            raise BacklogError("无效的依赖 type")
        if not isinstance(dependency["confirmation"], str) or dependency["confirmation"] not in CONFIRMATIONS:
            raise BacklogError("依赖 confirmation 只允许 confirmed 或 pending")
        valid_text(dependency["basis"], "依赖 basis", allow_empty=True)
        if dependency["confirmation"] == "confirmed" and not dependency["basis"].strip():
            raise BacklogError("已确认依赖必须填写 basis")
        marker = (dependency["id"], dependency["type"])
        if marker in seen_dependencies:
            raise BacklogError("相同 ID 与 type 的依赖不能重复")
        seen_dependencies.add(marker)

    decisions = card["decisions"]
    if not isinstance(decisions, list):
        raise BacklogError("decisions 必须是数组")
    previous_decision_at = None
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"at", "outcome", "reason", "source_refs"}:
            raise BacklogError("每条决策必须包含 at、outcome、reason、source_refs")
        decision_at = valid_timestamp(decision["at"], "决策 at")
        if previous_decision_at is not None and decision_at < previous_decision_at:
            raise BacklogError("decisions 必须按时间先后追加，以保证最近决策正确")
        previous_decision_at = decision_at
        valid_text(decision["outcome"], "决策 outcome")
        valid_text(decision["reason"], "决策 reason")
        valid_refs(decision["source_refs"], "决策 source_refs")

    if "revision" in card and (type(card["revision"]) is not int or card["revision"] < 1):
        raise BacklogError("revision 必须是正整数")
    if "updated_at" in card:
        valid_timestamp(card["updated_at"], "updated_at")
    if "history" in card:
        if not isinstance(card["history"], list):
            raise BacklogError("history 必须是数组")
        for expected_history_revision, event in enumerate(card["history"], 1):
            if not isinstance(event, dict) or set(event) != {"revision", "at", "note"}:
                raise BacklogError("history 条目格式不正确")
            if type(event["revision"]) is not int or event["revision"] != expected_history_revision:
                raise BacklogError("history revision 必须从 1 连续递增")
            valid_timestamp(event["at"], "history at")
            valid_text(event["note"], "history note")
        if "revision" in card and len(card["history"]) != card["revision"]:
            raise BacklogError("history 长度与 revision 不一致")
        if "updated_at" in card and card["history"] and card["history"][-1]["at"] != card["updated_at"]:
            raise BacklogError("history 最新时间与 updated_at 不一致")


def load_json(path):
    if path.is_symlink():
        raise BacklogError("拒绝读取符号链接文件")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BacklogError("文件不存在") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BacklogError("JSON 文件格式无效") from exc


def atomic_json_write(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".card-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass  # The replace is still atomic where supported.
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def immutable_json_write(path, payload):
    """Create an archived revision exactly once, never replacing another copy."""
    if path.is_symlink():
        raise BacklogError("历史快照不能是符号链接")
    if path.exists():
        if load_json(path) != payload:
            raise BacklogError("历史快照与当前旧版不一致；已停止更新以避免覆盖")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".revision-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(temporary), str(path))  # Fails rather than replacing an existing snapshot.
        except FileExistsError:
            if path.is_symlink() or load_json(path) != payload:
                raise BacklogError("历史快照与当前旧版不一致；已停止更新以避免覆盖")
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def store_root(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BacklogError("--root 必须是绝对路径")
    return path.resolve()


def ensure_not_skill_directory(root):
    skill_directory = Path(__file__).resolve().parent.parent
    if root == skill_directory or skill_directory in root.parents:
        raise BacklogError("不能把公开 Skill 仓库或安装目录用作需求数据目录")
    for ancestor in (root, *root.parents):
        skill_file = ancestor / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            if re.search(r"(?m)^name:\s*read-her-heart\s*$", skill_file.read_text(encoding="utf-8")[:1024]):
                raise BacklogError("不能在 read-her-heart Skill 目录内保存业务需求卡")
        except UnicodeError:
            continue


def inspect_store(root):
    if not root.is_dir():
        raise BacklogError("需求库目录不存在；先执行 init")
    marker = root / STORE_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise BacklogError("此目录不是已初始化的需求库；先检查 --root")
    metadata = load_json(marker)
    if not isinstance(metadata, dict) or metadata.get("kind") != STORE_KIND or metadata.get("schema_version") != SCHEMA_VERSION:
        raise BacklogError("需求库标识或版本无效")
    valid_text(metadata.get("project"), "需求库 project")
    cards = root / "cards"
    if cards.is_symlink() or not cards.is_dir():
        raise BacklogError("需求库 cards 目录无效")
    revisions = root / "revisions"
    if revisions.is_symlink() or not revisions.is_dir():
        raise BacklogError("需求库 revisions 目录无效")
    return metadata, cards


@contextlib.contextmanager
def store_lock(root):
    path = root / ".write.lock"
    if path.is_symlink():
        raise BacklogError("需求库锁文件不能是符号链接")
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows fallback
            import msvcrt
            os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows fallback
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def card_path(cards, card_id):
    valid_id(card_id)
    path = cards / (card_id + ".json")
    if path.is_symlink():
        raise BacklogError("卡片文件不能是符号链接")
    return path


def read_card(cards, card_id, project):
    path = card_path(cards, card_id)
    card = load_json(path)
    validate_card(card, project)
    if card["id"] != card_id or "revision" not in card or "updated_at" not in card or "history" not in card:
        raise BacklogError("卡片 ID、文件名或维护字段不一致")
    return card


def read_revision(root, cards, card_id, revision, project):
    latest = read_card(cards, card_id, project)
    if revision is None or revision == latest["revision"]:
        return latest
    if revision < 1 or revision > latest["revision"]:
        raise BacklogError("请求的卡片 revision 不存在")
    valid_id(card_id)
    revision_dir = root / "revisions" / card_id
    if revision_dir.is_symlink() or not revision_dir.is_dir():
        raise BacklogError("卡片历史目录无效")
    snapshot = revision_dir / f"rev-{revision}.json"
    card = load_json(snapshot)
    validate_card(card, project)
    if card["id"] != card_id or card.get("revision") != revision or "updated_at" not in card or "history" not in card:
        raise BacklogError("历史快照 ID 或版本不一致")
    return card


def preserve_previous_revision(root, existing):
    revisions = root / "revisions"
    if revisions.is_symlink() or not revisions.is_dir():
        raise BacklogError("需求库 revisions 目录无效")
    revision_dir = revisions / existing["id"]
    if revision_dir.is_symlink():
        raise BacklogError("卡片历史目录不能是符号链接")
    if not revision_dir.exists():
        revision_dir.mkdir(mode=0o700)
    elif not revision_dir.is_dir():
        raise BacklogError("卡片历史目录无效")
    snapshot = revision_dir / f"rev-{existing['revision']}.json"
    immutable_json_write(snapshot, existing)


def init_store(root, project):
    valid_text(project, "--project")
    ensure_not_skill_directory(root)
    if root.exists():
        if not root.is_dir():
            raise BacklogError("--root 已存在且不是目录")
        if (root / STORE_MARKER).is_file():
            metadata, _ = inspect_store(root)
            if metadata["project"] != project:
                raise BacklogError("此需求库已绑定其他项目")
            return {"root": str(root), "project": project, "created": False}
        if any(root.iterdir()):
            raise BacklogError("--root 不是空目录；拒绝在已有目录中初始化需求库")
    else:
        if not root.parent.is_dir():
            raise BacklogError("--root 的父目录不存在；请先选择已有项目路径")
        root.mkdir(mode=0o700)
    (root / "cards").mkdir(mode=0o700)
    (root / "revisions").mkdir(mode=0o700)
    atomic_json_write(root / STORE_MARKER, {
        "kind": STORE_KIND,
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "created_at": utc_now(),
    })
    return {"root": str(root), "project": project, "created": True}


def save_card(root, input_path, expected_revision, note):
    ensure_not_skill_directory(root)
    ensure_not_skill_directory(input_path.resolve())
    metadata, cards = inspect_store(root)
    incoming = load_json(input_path)
    validate_card(incoming, metadata["project"])
    path = card_path(cards, incoming["id"])
    with store_lock(root):
        if path.exists():
            existing = read_card(cards, incoming["id"], metadata["project"])
            if expected_revision is None:
                raise BacklogError("更新已有卡片必须提供 --expected-revision")
            if expected_revision != existing["revision"]:
                raise BacklogError("卡片版本已变化；请重新 show 后基于最新版本修改")
            valid_text(note, "--note")
            if "revision" in incoming and incoming["revision"] != existing["revision"]:
                raise BacklogError("输入卡片 revision 与当前版本不一致")
            if "updated_at" in incoming and incoming["updated_at"] != existing["updated_at"]:
                raise BacklogError("updated_at 由脚本维护，不可改写")
            if "history" in incoming and incoming["history"] != existing["history"]:
                raise BacklogError("history 由脚本维护，不可改写")
            if incoming["decisions"][:len(existing["decisions"])] != existing["decisions"]:
                raise BacklogError("既有决策不可删除或改写；只能追加新决策")
            major_changes = (
                incoming["mode"] != existing["mode"]
                or incoming["status"] != existing["status"]
                or incoming["scope"] != existing["scope"]
                or incoming["priority"] != existing["priority"]
            )
            if major_changes:
                new_decisions = incoming["decisions"][len(existing["decisions"]):]
                if not new_decisions or not new_decisions[-1]["source_refs"]:
                    raise BacklogError("模式、状态、范围或优先级变化必须追加含来源引用的决策记录")
            substantive_old = {k: v for k, v in existing.items() if k not in {"revision", "updated_at", "history"}}
            substantive_new = {k: v for k, v in incoming.items() if k not in {"revision", "updated_at", "history"}}
            if substantive_old == substantive_new:
                raise BacklogError("卡片内容未变化；不会生成空版本")
            preserve_previous_revision(root, existing)
            revision = existing["revision"] + 1
            history = list(existing["history"])
        else:
            if expected_revision not in (None, 0):
                raise BacklogError("新卡片的 --expected-revision 只能省略或为 0")
            if any(key in incoming for key in ("revision", "updated_at", "history")):
                raise BacklogError("新卡片不得自行填写 revision、updated_at 或 history")
            initial_major_state = (incoming["mode"] == "mandatory" or incoming["status"] != "评估中"
                                   or incoming["priority"] not in (None, "未确定"))
            if initial_major_state and (not incoming["decisions"] or not incoming["decisions"][-1]["source_refs"]):
                raise BacklogError("初始强制模式、非评估状态或正式优先级须附含来源引用的决策记录")
            revision = 1
            history = []
            note = note or "创建卡片"
        timestamp = utc_now()
        saved = {k: v for k, v in incoming.items() if k not in {"revision", "updated_at", "history"}}
        saved["revision"] = revision
        saved["updated_at"] = timestamp
        history.append({"revision": revision, "at": timestamp, "note": note})
        saved["history"] = history
        atomic_json_write(path, saved)
    return {"id": saved["id"], "revision": revision, "updated_at": timestamp, "path": str(path)}


def list_cards(cards, project):
    cards_found = []
    for path in sorted(cards.glob("*.json")):
        if path.is_symlink():
            raise BacklogError("卡片文件不能是符号链接")
        card_id = path.stem
        cards_found.append(read_card(cards, card_id, project))
    return cards_found


def backlog_item(card):
    return {
        "id": card["id"],
        "title": card["title"],
        "status": card["status"],
        "priority": card["priority"],
        "dependencies": card["dependencies"],
        "latest_decision": card["decisions"][-1] if card["decisions"] else None,
        "updated_at": card["updated_at"],
        "next_action": card["next_action"],
    }


def md_cell(value):
    rendered = html.escape(str(value), quote=False)
    return rendered.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def markdown_backlog(project, cards):
    rows = [f"# {md_cell(project)} · Backlog", "", "| ID | 标题 | 状态 | 优先级 | 依赖 | 最近决策 | 更新时间 | 下一步 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for card in cards:
        item = backlog_item(card)
        dependencies = "、".join(
            f"{dep['id']} ({dep['type']}/{dep['confirmation']})"
            for dep in item["dependencies"]
        ) or "—"
        decision = item["latest_decision"]["outcome"] if item["latest_decision"] else "—"
        fields = [item["id"], item["title"], item["status"], item["priority"] or "未确定",
                  dependencies, decision, item["updated_at"], item["next_action"] or "—"]
        rows.append("| " + " | ".join(md_cell(field) for field in fields) + " |")
    if not cards:
        rows.append("| — | 暂无需求 | — | — | — | — | — | — |")
    return "\n".join(rows) + "\n"


def markdown_card(card):
    rows = [f"# {md_cell(card['id'])} · {md_cell(card['title'])}", "",
            "| 字段 | 内容 |", "| --- | --- |"]
    fields = [
        ("项目", card["project"]), ("模式", card["mode"]), ("范围", card["scope"]),
        ("状态", card["status"]), ("优先级", card["priority"] or "未确定"),
        ("分析批次", card.get("analysis_batch") or "—"),
        ("版本", card["revision"]), ("更新时间", card["updated_at"]),
        ("下一步", card["next_action"] or "—"),
    ]
    for label, value in fields:
        rows.append(f"| {label} | {md_cell(value)} |")
    rows.extend(["", "## 依赖", ""])
    if card["dependencies"]:
        for dep in card["dependencies"]:
            rows.append(f"- {md_cell(dep['id'])} · {md_cell(dep['type'])} · {md_cell(dep['confirmation'])}：{md_cell(dep['basis'] or '依据待确认')}")
    else:
        rows.append("- 暂无记录")
    rows.extend(["", "## 决策记录", ""])
    if card["decisions"]:
        for decision in card["decisions"]:
            refs = "、".join(md_cell(ref) for ref in decision["source_refs"]) or "无来源引用"
            rows.append(f"- {md_cell(decision['at'])} · {md_cell(decision['outcome'])}：{md_cell(decision['reason'])}（{refs}）")
    else:
        rows.append("- 尚无正式决策")
    rows.extend(["", "## 来源", ""])
    rows.extend(f"- {md_cell(ref)}" for ref in card["source_refs"])
    if not card["source_refs"]:
        rows.append("- 暂无来源引用")
    if card.get("links"):
        rows.extend(["", "## 关联文件", ""])
        rows.extend(f"- {md_cell(key)}：{md_cell(value)}" for key, value in card["links"].items())
    if card.get("analysis"):
        rendered = json.dumps(card["analysis"], ensure_ascii=False, indent=2)
        longest = max((len(match.group(0)) for match in re.finditer(r"~+", rendered)), default=0)
        fence = "~" * max(4, longest + 1)
        rows.extend(["", "## 分析快照", "", fence + "json", rendered, fence])
    rows.extend(["", "_此 Markdown 由 JSON 卡片生成，不是独立事实源。_", ""])
    return "\n".join(rows)


def build_parser():
    parser = argparse.ArgumentParser(description="离线维护单项目需求卡；业务数据只写入显式 --root，不写入公开 Skill 目录。")
    parser.add_argument("--root", required=True, help="项目专属需求库的绝对路径；不能位于此 Skill 仓库/安装目录内")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="初始化新目录或空目录，并绑定一个项目")
    init.add_argument("--project", required=True, help="项目名；后续卡片 project 必须完全一致")
    save = subcommands.add_parser("save", help="创建或更新 JSON 卡片；更新必须校验版本并留变更说明")
    save.add_argument("--input", required=True, help="待保存卡片 JSON 文件")
    save.add_argument("--expected-revision", type=int, help="更新时从 show 读到的当前 revision；新建可省略")
    save.add_argument("--note", help="变更说明；更新时必填")
    show = subcommands.add_parser("show", help="按 ID 跨会话重新加载卡片")
    show.add_argument("--id", required=True, help="卡片 ID")
    show.add_argument("--revision", type=int, help="读取指定旧版；省略则读取最新卡片")
    show.add_argument("--format", choices=("json", "markdown"), default="json")
    listing = subcommands.add_parser("list", help="生成 Backlog 视图；不修改卡片")
    listing.add_argument("--format", choices=("json", "markdown"), default="markdown")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        root = store_root(args.root)
        if args.command == "init":
            result = init_store(root, args.project)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "save":
            result = save_card(root, Path(args.input), args.expected_revision, args.note)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "show":
            metadata, cards = inspect_store(root)
            card = read_revision(root, cards, args.id, args.revision, metadata["project"])
            if args.format == "markdown":
                print(markdown_card(card), end="")
            else:
                print(json.dumps(card, ensure_ascii=False, indent=2))
        elif args.command == "list":
            metadata, cards = inspect_store(root)
            found = list_cards(cards, metadata["project"])
            if args.format == "markdown":
                print(markdown_backlog(metadata["project"], found), end="")
            else:
                print(json.dumps({"project": metadata["project"], "items": [backlog_item(card) for card in found]},
                                 ensure_ascii=False, indent=2))
    except (BacklogError, OSError) as exc:
        print(f"backlog: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
