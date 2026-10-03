"""只读 QQ 上传动态的顶层评论。凭据仅驻留内存，不写入快照和日志。"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

from .core import Comment, LotteryError, digest, identifier, integer, normalize_comment, qq_id

ENDPOINT = "https://h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/cgi-bin/upp/qun_list_photocmt_v2"
PAGE_SIZE = 20
MAX_BODY = 2_000_000


def unwrap(result):
    if isinstance(result, dict) and "status" in result:
        if result["status"] != "ok" or result.get("retcode", 0) != 0:
            raise LotteryError("NapCat 接口返回失败，请检查机器人登录状态和接口支持。")
        return result.get("data")
    return result


async def call_action(bot, action: str, **params):
    try:
        result = await asyncio.wait_for(bot.call_action(action, **params), timeout=20)
    except Exception:
        raise LotteryError("NapCat 接口暂时不可用，请检查连接后重试。") from None
    return unwrap(result)


def csrf_token(cookie: str) -> int:
    if not isinstance(cookie, str) or not cookie or "\r" in cookie or "\n" in cookie:
        raise LotteryError("机器人网页登录凭据缺失或异常。")
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:
        raise LotteryError("机器人网页登录凭据格式异常。") from None
    key = parsed.get("p_skey") or parsed.get("skey") or parsed.get("rv2")
    if not key or not key.value:
        raise LotteryError("机器人缺少 QQ 相册网页登录态，请重新登录 NapCat 后重试。")
    token = 5381
    for char in key.value:
        token = (token + (token << 5) + ord(char)) & 0xffffffff
    return token & 0x7fffffff


def decode_response(text: str) -> dict:
    # 只接受纯 JSON 或标准 callback({...});，不执行 JavaScript。
    stripped = text.strip()
    if not stripped.startswith("{"):
        match = re.fullmatch(r"[A-Za-z_$][\w.$]*\s*\(\s*(\{.*\})\s*\)\s*;?", stripped, re.S)
        if not match:
            raise LotteryError("QQ 返回格式发生变化，已停止统计。")
        stripped = match.group(1)
    try:
        result = json.loads(stripped)
    except (ValueError, RecursionError):
        raise LotteryError("QQ 返回格式无法解析，已停止统计。") from None
    if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0:
        raise LotteryError("QQ 相册接口未成功返回；可能需要重新登录或接口已变化。")
    if not isinstance(result.get("data"), dict):
        raise LotteryError("QQ 返回缺少评论数据，已停止统计。")
    return result["data"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_page(params: dict, headers: dict) -> dict:
    """固定 HTTPS 地址，不跟随重定向，不使用本机 Cookie 存储。"""
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode(params), headers=headers)
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise LotteryError("QQ 响应过大，已停止统计。")
        return decode_response(raw.decode("utf-8"))
    except LotteryError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise LotteryError("QQ 相册 HTTP 请求未成功，已停止统计；请稍后重试。") from None
    except Exception:
        # 不向上透传 urllib 异常：其中可能有完整 URL 或登录信息。
        raise LotteryError("读取 QQ 相册失败，请稍后重试；没有保存不完整名单。") from None


def parse_page(data: dict) -> tuple[list[Comment], int]:
    if not isinstance(data, dict):
        raise LotteryError("评论分页结构异常。")
    rows = data.get("comments", [])
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise LotteryError("评论分页结构异常。")
    total = integer(data.get("total"), "评论总数", 0, 1_000_000)
    # 只读取 comments 的直接节点，完全不递归 replies。
    return [normalize_comment(row) for row in rows], total


async def collect_pages(fetch, max_comments: int = 2000) -> list[Comment]:
    """完整读取两遍比对元数据。最后空页 total=0 是 QQ 的正常行为。"""
    max_comments = integer(max_comments, "读取上限", 20, 10000)

    async def scan():
        all_rows: list[Comment] = []
        seen = set()
        rows, expected = parse_page(await fetch(0))
        if expected > max_comments:
            raise LotteryError(f"直接留言超过读取上限 {max_comments}，已停止统计。")
        total, offset = expected, 0
        for _ in range(max_comments // PAGE_SIZE + 2):
            if not rows:
                if total not in (0, expected) or len(all_rows) != expected:
                    raise LotteryError("分页提前结束或总数不一致，已停止统计。")
                return all_rows
            if total != expected:
                raise LotteryError("读取期间留言总数变化，请等留言稳定后重新统计。")
            for row in rows:
                if row.id in seen:
                    raise LotteryError("分页出现重复留言，已停止统计，避免漏人。")
                seen.add(row.id)
                all_rows.append(row)
            if len(all_rows) > expected or len(all_rows) > max_comments:
                raise LotteryError("实际留言数超出接口总数，已停止统计。")
            offset += PAGE_SIZE
            rows, total = parse_page(await fetch(offset))
        raise LotteryError("分页未正常结束，已停止统计。")

    first = await scan()
    repeat = await scan()
    signature = lambda rows: digest([c.__dict__ for c in rows])
    if signature(repeat) != signature(first):
        raise LotteryError("两遍读取的留言不同，请等留言稳定后重新统计。")
    return first


async def read_comments(bot, bot_qq: str, group: str, album: str, batch: str,
                        max_comments: int = 2000) -> list[Comment]:
    bot_qq, group = qq_id(bot_qq), qq_id(group)
    album, batch = identifier(album, "相册编号"), identifier(batch, "上传批次")
    credential = await call_action(bot, "get_credentials", domain="qzone.qq.com")
    if not isinstance(credential, dict):
        raise LotteryError("NapCat 未返回网页登录凭据。")
    cookie = credential.get("cookies")
    token = csrf_token(cookie)
    headers = {"Cookie": cookie,
               "Referer": "https://h5.qzone.qq.com/groupphoto/index?groupId=" + group,
               "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/100.0 Mobile Safari/537.36 QQ/8.9.68.10240"}

    async def fetch(start):
        params = {"uin": bot_qq, "hostUin": bot_qq, "start": start, "num": PAGE_SIZE,
                  "order": 0, "topicId": "|".join((group, album, batch)), "format": "jsonp",
                  "inCharset": "utf-8", "outCharset": "utf-8", "ref": "", "cmtType": 4, "g_tk": token}
        await asyncio.sleep(0.25)
        return await asyncio.to_thread(http_page, params, headers)

    try:
        return await asyncio.wait_for(collect_pages(fetch, max_comments), timeout=180)
    except asyncio.TimeoutError:
        raise LotteryError("相册读取超过三分钟，已停止统计；请稍后重试。") from None


async def list_page(bot, group: str, album: str | None = None, cursor: str = "") -> tuple[list, str]:
    action = "get_group_album_media_list" if album else "get_qun_album_list"
    params = {"group_id": group, "attach_info": cursor}
    if album:
        params["album_id"] = album
    data = await call_action(bot, action, **params)
    if not isinstance(data, dict):
        # 部分旧 NapCat 相册列表只有数组（没有下一页）。
        if album is None and isinstance(data, list):
            return data, ""
        raise LotteryError("相册列表结构变化，无法安全选择图片。")
    if album is None and isinstance(data.get("album"), dict):
        data = data["album"]
    rows = data.get("media_list" if album else "album_list")
    if rows is None and album is None:
        rows = data.get("album")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise LotteryError("相册列表字段异常。")
    more = data.get("next_has_more", data.get("has_more", False))
    next_cursor = data.get("next_attach_info", data.get("attach_info", ""))
    if more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or not rows):
        raise LotteryError("相册分页游标异常，已停止翻页。")
    return rows, next_cursor if more else ""
