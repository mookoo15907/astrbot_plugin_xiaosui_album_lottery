import asyncio
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.star import StarTools
except ImportError:
    StarTools = None

from .core import (LotteryError, Store, checked_summary, commit_draw, digest,
                   display_time, identifier, integer, qq_id, start_time, summarize)
from .qq_reader import list_page, read_comments

PLUGIN = "astrbot_plugin_xiaosui_album_lottery"
HELP = """相册抽签（仅 AstrBot 管理员，本群内操作）
① 相册抽签 相册
② 相册抽签 图片 相册编号
③ 相册抽签 建池 图片编号 2026-10-04T22:10:00
④ 主办方发截止直接留言后：相册抽签 统计
⑤ 相册抽签 名单（可加页码）→ 相册抽签 确认
⑥ 相册抽签 抽取 1（可换人数）
其他：状态、边界、记录 池编号、结束 池编号。
建池可在最后加主办方QQ；默认是发指令的人。
相册/图片可加数字翻本页列表，或「下一页」取下一批。
开始时间按北京时间，包含开始秒；截止同秒需手动核对。
请单独上传一张报名图：读取范围是它所在的上传动态。"""


def clean_title(value) -> str:
    return str(value).replace("[CQ:", "［CQ:").replace("\n", " ").replace("\r", " ")[:60]


def page_rows(rows: list, page_text: str) -> tuple[list, int, int]:
    pages = max(1, (len(rows) + 19) // 20)
    page = integer(page_text or "1", "页码", 1, pages)
    return rows[(page - 1) * 20:page * 20], page, pages


def data_directory():
    getter = getattr(StarTools, "get_data_dir", None)
    if callable(getter):
        return Path(getter())
    # 较早的 AstrBot 没有此工具函数。仅识别正常的 data/plugins/插件名 结构，
    # 不猜测当前工作目录；将记录放在同级 plugin_data，插件更新也不会覆盖它。
    root = Path(__file__).resolve().parent
    if root.parent.name == "plugins" and root.parent.parent.name == "data":
        return root.parent.parent / "plugin_data" / PLUGIN
    raise RuntimeError("无法确定 AstrBot 插件数据目录，请使用标准插件安装目录。")


@register(PLUGIN, "mookoo", "小碎群相册去重报名与抽签", "0.1.0")
class XiaosuiAlbumLottery(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = Store(data_directory())
        self.lock = asyncio.Lock()
        # 选择列表分机器人、群、管理员隔离，10 分钟有效，不持久化私有图片 URL。
        self.selections = {}

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("相册抽签")
    async def album_lottery(self, event: AstrMessageEvent, operation: str = "帮助",
                            arg1: str = "", arg2: str = "", arg3: str = ""):
        event.stop_event()
        # 同时在入口验证，防止外部命令权限覆盖将本功能意外开放给群成员。
        if not event.is_admin():
            yield event.plain_result("相册抽签只允许 AstrBot 管理员使用。")
            return
        if self.lock.locked():
            yield event.plain_result("正在处理另一条相册抽签指令，请稍后再试。")
            return
        try:
            async with self.lock:
                answer = await self.dispatch(event, operation, arg1, arg2, arg3)
        except LotteryError as error:
            answer = str(error)
        except Exception as error:
            # 不记录错误原文、网络 URL、Cookie 或评论正文。
            logger.warning(f"相册抽签操作中止：{type(error).__name__}")
            answer = "相册抽签遇到异常，已停止本次操作；请检查插件版本和本地记录。"
        yield event.plain_result(answer)

    def event_context(self, event):
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        if not isinstance(event, AiocqhttpMessageEvent) or not event.get_group_id():
            raise LotteryError("请在使用 NapCat 的 QQ 群里发送这个指令。")
        group = qq_id(event.get_group_id())
        bot_qq = qq_id(event.message_obj.self_id)
        sender = qq_id(event.get_sender_id())
        key = bot_qq + ":" + group
        return event.bot, bot_qq, group, sender, key, key + ":" + sender

    def cached_selection(self, selection_key):
        cache = self.selections.get(selection_key)
        if not cache or time.monotonic() - cache["updated"] > 600:
            raise LotteryError("选择列表已过期，请重新发送「相册抽签 相册」。")
        return cache

    async def dispatch(self, event, operation, arg1, arg2, arg3):
        if operation == "帮助":
            return HELP
        bot, bot_qq, group, sender, key, selection_key = self.event_context(event)
        if operation == "相册":
            if arg1 == "下一页":
                old = self.cached_selection(selection_key)
                cursor = old["album_cursor"]
                if not cursor:
                    raise LotteryError("没有下一批相册。")
                rows, next_cursor = await list_page(bot, group, cursor=cursor)
            elif arg1 in ("", "1"):
                rows, next_cursor = await list_page(bot, group)
            else:
                old = self.cached_selection(selection_key)
                rows, next_cursor = old["albums"], old["album_cursor"]
            albums = [{"id": identifier(row.get("album_id", row.get("id")), "相册编号"),
                       "name": clean_title(row.get("name", row.get("title", row.get("album_name", "未命名"))))}
                      for row in rows]
            subset, page, pages = page_rows(albums, "1" if arg1 == "下一页" else arg1)
            self.selections[selection_key] = {"albums": albums, "album_cursor": next_cursor,
                                              "updated": time.monotonic()}
            lines = [f"本批相册 第{page}/{pages}页（编号只对应本批列表）"]
            lines += [f"{(page - 1) * 20 + i}. {row['name']}" for i, row in enumerate(subset, 1)]
            lines += ["相册抽签 图片 编号" if albums else "本群没有可读取的相册。"]
            if next_cursor:
                lines.append("还有下一批：相册抽签 相册 下一页")
            return "\n".join(lines)

        if operation == "图片":
            cache = self.cached_selection(selection_key)
            index = integer(arg1, "相册编号", 1, len(cache["albums"])) - 1
            album = cache["albums"][index]
            if arg2 == "下一页":
                if cache.get("selected_album") != album or not cache.get("media_cursor"):
                    raise LotteryError("请先查看这个相册的图片；目前没有可用的下一批。")
                rows, next_cursor = await list_page(bot, group, album["id"], cache["media_cursor"])
            elif arg2 in ("", "1"):
                rows, next_cursor = await list_page(bot, group, album["id"])
            else:
                if cache.get("selected_album") != album:
                    raise LotteryError("请先查看这个相册的图片。")
                rows, next_cursor = None, cache["media_cursor"]
            media = cache["media"] if rows is None else []
            if rows is not None:
                for row in rows:
                    # 只接受含 image 的照片；视频不作为报名图。
                    if not isinstance(row.get("image"), dict):
                        continue
                    media.append({"batch": identifier(row.get("batch_id"), "上传批次"),
                                  "uploaded": integer(row.get("upload_time"), "上传时间", 1)})
            subset, page, pages = page_rows(media, "1" if arg2 == "下一页" else arg2)
            cache.update(selected_album=album, media=media, media_cursor=next_cursor,
                         updated=time.monotonic())
            lines = [f"{album['name']} 图片 第{page}/{pages}页（北京时间；编号对应本批）"]
            lines += [f"{(page - 1) * 20 + i}. 上传 {display_time(row['uploaded'])}，批次 {row['batch']}"
                      for i, row in enumerate(subset, 1)]
            lines.append("建池 图片编号 开始时间：相册抽签 建池 1 2026-10-04T22:10:00")
            if next_cursor:
                lines.append(f"还有下一批：相册抽签 图片 {arg1} 下一页")
            return "\n".join(lines)

        state = self.store.load()
        pool_id = state["active"].get(key)
        pool = state["pools"].get(pool_id)
        if operation == "记录":
            archived = state["pools"].get(arg1)
            if not archived or archived["scope"] != key:
                raise LotteryError("本群没有这个池编号的记录。")
            return self.describe(archived) + self.result_text(archived)
        if operation == "建池":
            if pool:
                raise LotteryError(f"本群还有池 {pool_id}。要开展新一轮，请先发送「相册抽签 结束 {pool_id}」。旧记录会保留。")
            cache = self.cached_selection(selection_key)
            media = cache.get("media", [])
            index = integer(arg1, "图片编号", 1, len(media)) - 1
            target = media[index]
            start = start_time(arg2)
            organizer = qq_id(arg3 or sender)
            if start < target["uploaded"]:
                raise LotteryError("开始时间早于所选图片上传时间，请核对图片和日期。")
            pool_id = secrets.token_hex(4)
            while pool_id in state["pools"]:
                pool_id = secrets.token_hex(4)
            pool = {"id": pool_id, "scope": key, "group": group, "bot_qq": bot_qq,
                    "album": cache["selected_album"], "batch": target["batch"],
                    "uploaded": target["uploaded"], "start": start, "organizer": organizer,
                    "creator": sender, "created_at": int(time.time()), "status": "draft",
                    "snapshot": None, "decisions": {}, "audit": []}
            state["active"][key] = pool_id
            state["pools"][pool_id] = pool
            self.store.save(state)
            return self.describe(pool) + "\n已经建池。截止直接留言发布后，发送「相册抽签 统计」。"
        if not pool:
            raise LotteryError("本群还没有活动池，请先查看相册并建池。")
        if pool["scope"] != key:
            raise LotteryError("抽签池归属校验失败，已停止操作。")
        if operation == "状态":
            return self.describe(pool) + self.result_text(pool)
        if operation == "结束":
            if arg1 != pool_id:
                raise LotteryError(f"请填写当前池编号：相册抽签 结束 {pool_id}")
            state["active"].pop(key)
            pool["ended_at"] = int(time.time())
            pool["audit"].append({"op": "end", "actor": sender, "at": int(time.time())})
            self.store.save(state)
            return f"池 {pool_id} 已结束，名单和抽取记录保留。可用「相册抽签 记录 {pool_id}」查看。"
        if operation == "统计":
            if pool["status"] in ("confirmed", "drawn"):
                raise LotteryError("这份名单已确认锁定，不再从 QQ 刷新。发送「相册抽签 名单」查看。")
            # 重读失败不能保留可确认的旧预览。
            pool.update(status="draft", snapshot=None, decisions={})
            self.store.save(state)
            rows = await read_comments(bot, bot_qq, group, pool["album"]["id"], pool["batch"])
            summary = summarize(rows, pool["start"], pool["organizer"])
            comments = [asdict(row) for row in rows]
            pool.update(status="preview", snapshot={"comments": comments, "fingerprint": digest(comments),
                                                    "read_at": int(time.time())})
            pool["audit"].append({"op": "read", "actor": sender, "at": int(time.time()),
                                  "fingerprint": pool["snapshot"]["fingerprint"]})
            self.store.save(state)
            return self.summary_text(pool, summary)
        if operation == "名单":
            summary = checked_summary(pool)
            subset, page, pages = page_rows(summary["entrants"], arg1)
            return "\n".join([f"池 {pool_id} 去重名单 {len(summary['entrants'])}人 第{page}/{pages}页"] +
                             [f"{(page - 1) * 20 + i}. QQ {row['qq']}｜{display_time(row['timestamp'])}"
                              for i, row in enumerate(subset, 1)] +
                             ["名单无误后：相册抽签 确认"])
        if operation == "边界":
            summary = checked_summary(pool)
            if not arg1 or arg1.isdigit() and arg2 == "":
                subset, page, pages = page_rows(summary["boundary"], arg1)
                return "\n".join([f"截止同秒核对 第{page}/{pages}页"] +
                                 [f"编号 {row['id']}｜QQ {row['qq']}｜" +
                                  ("纳入" if pool["decisions"].get(row["id"]) is True else
                                   "排除" if row["id"] in pool["decisions"] else "待核对")
                                  for row in subset] +
                                 ["核对 QQ 中的先后顺序后：相册抽签 边界 留言编号 纳入（或排除）"])
            if pool["status"] != "preview":
                raise LotteryError("只有尚未确认的名单可以核对同秒留言。")
            if arg1 not in {row["id"] for row in summary["boundary"]} or arg2 not in ("纳入", "排除"):
                raise LotteryError("请填写这份预览中的留言编号，以及「纳入」或「排除」。")
            pool["decisions"][arg1] = arg2 == "纳入"
            pool["audit"].append({"op": "boundary", "id": arg1, "include": arg2 == "纳入",
                                  "actor": sender, "at": int(time.time())})
            self.store.save(state)
            return self.summary_text(pool, checked_summary(pool))
        if operation == "确认":
            if pool["status"] in ("confirmed", "drawn"):
                return "这份名单已经确认锁定。" + self.result_text(pool)
            summary = checked_summary(pool)
            if summary["unresolved"]:
                raise LotteryError(f"还有 {len(summary['unresolved'])}条截止同秒留言待核对，发送「相册抽签 边界」查看。")
            if not summary["entrants"]:
                raise LotteryError("名单为空，不能确认或抽取。")
            pool["status"] = "confirmed"
            pool["audit"].append({"op": "confirm", "actor": sender, "at": int(time.time())})
            self.store.save(state)
            return f"池 {pool_id} 已锁定 {len(summary['entrants'])}人的名单。发送「相册抽签 抽取 人数」。"
        if operation == "抽取":
            count = integer(arg1 or "1", "抽取人数", 1, 2000)
            already_drawn = pool["status"] == "drawn"
            commit_draw(pool, count)
            if not already_drawn:
                pool["audit"].append({"op": "draw", "actor": sender, "at": int(time.time())})
                self.store.save(state)
            return f"池 {pool_id}" + self.result_text(pool) + "\n重复发送抽取指令会返回这一份结果。"
        return "没有这个操作。\n" + HELP

    @staticmethod
    def describe(pool):
        status = {"draft": "待统计", "preview": "待确认", "confirmed": "已确认", "drawn": "已抽取"}
        return (f"池 {pool['id']}｜{status.get(pool['status'], '未知')}\n相册：{pool['album']['name']}"
                f"｜上传：{display_time(pool['uploaded'])}\n上传动态批次：{pool['batch']}"
                f"\n开始：{display_time(pool['start'])}（包含开始秒）\n主办方 QQ：{pool['organizer']}")

    @staticmethod
    def result_text(pool):
        if pool.get("status") != "drawn":
            return ""
        return "\n抽取结果：" + "、".join(pool["winners"])

    @staticmethod
    def summary_text(pool, summary):
        return (f"池 {pool['id']}：读取 {summary['direct_total']}条直接留言（回复不报名）。"
                f"\n截止：{display_time(summary['cutoff'])}，主办方不入池。"
                f"\n有效去重名单 {len(summary['entrants'])}人，剔除窗口内重复 {summary['duplicates']}条。"
                f"\n截止同秒待核对 {len(summary['unresolved'])}条。"
                "\n查看：相册抽签 名单；同秒核对：相册抽签 边界；无误后：相册抽签 确认。")
