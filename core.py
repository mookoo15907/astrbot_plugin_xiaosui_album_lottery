"""抽签规则和持久化；不依赖 AstrBot，也不接触小碎原有的数据。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


class LotteryError(Exception):
    """仅包含可以安全展示给管理员的信息。"""


def qq_id(value) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{4,19}", str(value)):
        raise LotteryError("QQ 号字段缺失或异常，已停止统计。")
    return str(value)


def integer(value, label: str, minimum=0, maximum=4102444800) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise LotteryError(f"{label}字段异常，已停止操作。")
    result = int(value)
    if not minimum <= result <= maximum:
        raise LotteryError(f"{label}超出允许范围，已停止操作。")
    return result


def identifier(value, label="留言编号") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise LotteryError(f"{label}缺失。")
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
        raise LotteryError(f"{label}异常。")
    return value


def album_identifier(value) -> str:
    """QQ 相册使用编码后的不透明 ID；保留 *、! 等字符，不将其当文件名。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise LotteryError("相册编号缺失。")
    value = str(value)
    # topicId 用 | 拼接，最终由 urlencode 编码；拒绝分隔符、路径及控制字符。
    if not re.fullmatch(r"[A-Za-z0-9_.!~*=-]{1,256}", value):
        raise LotteryError("相册编号异常。")
    return value


def start_time(text: str) -> int:
    try:
        return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=CST).timestamp())
    except (ValueError, OverflowError):
        raise LotteryError("时间请写为 2026-10-04T22:10:00（北京时间，含秒）。") from None


def display_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, CST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Comment:
    id: str
    qq: str
    timestamp: int


def normalize_comment(row: dict) -> Comment:
    if not isinstance(row, dict) or not isinstance(row.get("poster"), dict):
        raise LotteryError("留言结构发生变化，已停止统计。")
    return Comment(identifier(row.get("id")), qq_id(row["poster"].get("id")),
                   integer(row.get("postTime"), "留言时间", 1))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def summarize(comments: list[Comment], start: int, organizer: str,
              decisions: dict[str, bool] | None = None) -> dict:
    if len({c.id for c in comments}) != len(comments):
        raise LotteryError("发现重复留言编号，无法确定名单是否完整。")
    cutoff_rows = [c for c in comments if c.qq == organizer and c.timestamp >= start]
    if not cutoff_rows:
        raise LotteryError("还没有找到开始时间之后的主办方直接留言；请发截止留言后再统计。")
    cutoff = min(c.timestamp for c in cutoff_rows)
    valid = [c for c in comments if c.qq != organizer and start <= c.timestamp < cutoff]
    by_qq = {}
    for c in sorted(valid, key=lambda c: (c.timestamp, c.id)):
        by_qq.setdefault(c.qq, c)
    # 同秒内没有可靠的先后顺序；已在窗口内入池的 QQ 不会因此变化。
    boundary = [c for c in comments if c.qq != organizer and c.timestamp == cutoff
                and c.qq not in by_qq]
    boundary.sort(key=lambda c: c.id)
    decisions = decisions or {}
    if set(decisions) - {c.id for c in boundary} or any(type(v) is not bool for v in decisions.values()):
        raise LotteryError("同秒核对记录与这份名单不匹配。")
    unresolved = [asdict(c) for c in boundary if c.id not in decisions]
    included = [c for c in boundary if decisions.get(c.id) is True]
    for c in included:
        by_qq.setdefault(c.qq, c)
    entrants = sorted(by_qq.values(), key=lambda c: (c.timestamp, c.qq))
    return {"cutoff": cutoff, "cutoff_ids": sorted(c.id for c in cutoff_rows if c.timestamp == cutoff),
            "entrants": [asdict(c) for c in entrants], "boundary": [asdict(c) for c in boundary],
            "unresolved": unresolved, "duplicates": len(valid) + len(included) - len(entrants),
            "direct_total": len(comments),
            "outside": sum(c.qq != organizer and (c.timestamp < start or c.timestamp > cutoff)
                           for c in comments),
            "organizer_comments": sum(c.qq == organizer for c in comments)}


def checked_summary(pool: dict) -> dict:
    snap = pool.get("snapshot")
    if not snap or digest(snap["comments"]) != snap.get("fingerprint"):
        raise LotteryError("名单快照缺失或校验失败，请重新统计。")
    comments = [Comment(identifier(c["id"]), qq_id(c["qq"]),
                        integer(c["timestamp"], "留言时间", 1)) for c in snap["comments"]]
    return summarize(comments, pool["start"], pool["organizer"], pool.get("decisions"))


def commit_draw(pool: dict, count: int) -> list[str]:
    """调用者保存成功后才展示结果；重复调用永远返回已保存的结果。"""
    if pool["status"] == "drawn":
        if count != len(pool["winners"]):
            raise LotteryError("这一池已经抽过；重复指令只返回原结果，不能改变人数。")
        return list(pool["winners"])
    if pool["status"] != "confirmed":
        raise LotteryError("请先查看名单并发送「相册抽签 确认」。")
    summary = checked_summary(pool)
    if summary["unresolved"]:
        raise LotteryError("还有截止同秒的留言未核对，不能抽取。")
    entrants = [row["qq"] for row in summary["entrants"]]
    count = integer(count, "抽取人数", 1, max(1, len(entrants)))
    if not entrants:
        raise LotteryError("有效名单为空，不能抽取。")
    pool["winners"] = secrets.SystemRandom().sample(entrants, count)
    pool["status"] = "drawn"
    pool["drawn_at"] = int(datetime.now(CST).timestamp())
    return list(pool["winners"])


class Store:
    """单个独立文件。调用者持有锁，用磁盘副本操作，保存失败不发布抽签结果。"""
    def __init__(self, directory: Path):
        self.path = Path(directory) / "lottery_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "active": {}, "pools": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema") != 1 or not isinstance(data["active"], dict) or not isinstance(data["pools"], dict):
                raise ValueError()
            return data
        except (OSError, ValueError, KeyError, TypeError):
            raise LotteryError("抽签记录读取失败；为保护原记录，已停止操作。请备份文件后检查。") from None

    def save(self, data: dict):
        temp = None
        try:
            fd, temp = tempfile.mkstemp(prefix=".lottery-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
        except (OSError, ValueError, TypeError):
            raise LotteryError("抽签记录保存失败，未发布抽取结果；请检查磁盘空间和权限。") from None
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
