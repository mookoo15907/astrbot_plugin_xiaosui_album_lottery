"""本机离线测试：不连接 QQ、不写小碎现有数据、不发送消息。"""
import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_xiaosui_album_lottery import core, qq_reader

HOST = "123456789"
A, B, C = "234567890", "345678901", "456789012"
START = 1791123000


def raw(cid, qq=A, timestamp=START + 1, **extra):
    return {"id": str(cid), "poster": {"id": qq}, "postTime": timestamp, **extra}


def pool(rows, status="confirmed"):
    comments = [core.normalize_comment(row).__dict__ for row in rows]
    return {"start": START, "organizer": HOST, "status": status, "decisions": {},
            "snapshot": {"comments": comments, "fingerprint": core.digest(comments)}}


class RuleTests(unittest.TestCase):
    def test_earliest_valid_in_window_not_old_duplicate(self):
        rows = [raw("old", A, START - 10), raw("valid", A, START + 2),
                raw("dup", A, START + 4), raw("b", B, START),
                raw("early_host", HOST, START - 1), raw("cut", HOST, START + 10),
                raw("late", C, START + 11), raw("later_cut", HOST, START + 20)]
        result = core.checked_summary(pool(rows))
        self.assertEqual([(row["qq"], row["id"]) for row in result["entrants"]], [(B, "b"), (A, "valid")])
        self.assertEqual(result["cutoff"], START + 10)
        self.assertEqual(result["duplicates"], 1)

    def test_reply_and_nested_reply_never_become_entrants(self):
        rows = [raw("a", A, replies=[raw("r", B, replies=[raw("rr", C)])]),
                raw("cut", HOST, START + 10)]
        self.assertEqual([row["qq"] for row in core.checked_summary(pool(rows))["entrants"]], [A])

    def test_reply_by_host_is_not_cutoff(self):
        with self.assertRaises(core.LotteryError):
            core.checked_summary(pool([raw("a", replies=[raw("r", HOST, START + 10)])]))

    def test_pre_start_host_alone_is_not_cutoff(self):
        with self.assertRaises(core.LotteryError):
            core.checked_summary(pool([raw("a"), raw("host", HOST, START - 1)]))

    def test_cutoff_same_second_requires_explicit_resolution(self):
        current = pool([raw("a"), raw("amb", B, START + 10), raw("cut", HOST, START + 10)])
        with self.assertRaises(core.LotteryError):
            core.commit_draw(current, 1)
        current["decisions"]["amb"] = True
        self.assertEqual(set(core.commit_draw(current, 2)), {A, B})

    def test_boundary_duplicate_cannot_add_another_ticket(self):
        current = pool([raw("a"), raw("dup", A, START + 10), raw("cut", HOST, START + 10)])
        self.assertEqual(core.checked_summary(current)["unresolved"], [])
        self.assertEqual(core.commit_draw(current, 1), [A])

    def test_multiple_same_second_comments_by_one_qq_dedup(self):
        current = pool([raw("a"), raw("b1", B, START + 10), raw("b2", B, START + 10), raw("cut", HOST, START + 10)])
        current["decisions"] = {"b1": True, "b2": True}
        result = core.checked_summary(current)
        self.assertEqual(len(result["entrants"]), 2)
        self.assertEqual(result["duplicates"], 1)

    def test_boundary_exclusion(self):
        current = pool([raw("a"), raw("b", B, START + 10), raw("cut", HOST, START + 10)])
        current["decisions"]["b"] = False
        self.assertEqual(core.commit_draw(current, 1), [A])

    def test_invalid_fields_fail_closed(self):
        for row in [raw("", A), raw("a", "0"), raw("a", A, True),
                    raw("a", A, "yesterday"), raw("../evil"), {"id": "a"}]:
            with self.subTest(row=row), self.assertRaises(core.LotteryError):
                core.normalize_comment(row)

    def test_album_ids_reject_topic_delimiters_paths_and_controls(self):
        for value in (None, True, "", "a|b", "../album", "a\\b", "a\nb", "a\rb", "a b", "a&b", "a" * 257):
            with self.subTest(value=value), self.assertRaises(core.LotteryError):
                core.album_identifier(value)

    def test_draw_bounds_and_empty(self):
        current = pool([raw("a"), raw("cut", HOST, START + 10)])
        for count in (0, -1, 2, True):
            with self.subTest(count=count), self.assertRaises(core.LotteryError):
                core.commit_draw(copy.deepcopy(current), count)
        with self.assertRaises(core.LotteryError):
            core.commit_draw(pool([raw("cut", HOST, START + 10)]), 1)

    def test_draw_once_and_never_reselect(self):
        current = pool([raw("a"), raw("b", B), raw("cut", HOST, START + 10)])
        result = core.commit_draw(current, 1)
        with patch.object(core.secrets, "SystemRandom", side_effect=AssertionError("must not redraw")):
            self.assertEqual(core.commit_draw(current, 1), result)
            with self.assertRaises(core.LotteryError):
                core.commit_draw(current, 2)

    def test_preview_cannot_draw(self):
        with self.assertRaises(core.LotteryError):
            core.commit_draw(pool([raw("a"), raw("cut", HOST, START + 10)], "preview"), 1)

    def test_modified_snapshot_rejected(self):
        current = pool([raw("a"), raw("cut", HOST, START + 10)])
        current["snapshot"]["comments"][0]["qq"] = B
        with self.assertRaises(core.LotteryError):
            core.checked_summary(current)

    def test_explicit_beijing_time(self):
        self.assertEqual(core.display_time(core.start_time("2026-10-04T22:10:00")), "2026-10-04 22:10:00")
        for text in ("22:10", "2026-02-30T22:10:00", "2026-10-04T22:10:00Z"):
            with self.assertRaises(core.LotteryError):
                core.start_time(text)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_27_direct_plus_3_replies_and_terminal_zero(self):
        rows = [raw(i + 1, replies=[raw("r" + str(i), B)] if i < 3 else []) for i in range(27)]
        calls = []
        async def fetch(offset):
            calls.append(offset)
            selected = rows[offset:offset + 20]
            return {"comments": selected, "total": 27 if selected else 0}
        result = await qq_reader.collect_pages(fetch)
        self.assertEqual(len(result), 27)
        self.assertEqual(calls, [0, 20, 40, 0, 20, 40])
        self.assertNotIn(B, [row.qq for row in result])

    async def test_122_direct_seven_nonempty_pages(self):
        rows = [raw(i + 1) for i in range(122)]
        fetch = AsyncMock(side_effect=lambda offset: {"comments": rows[offset:offset + 20], "total": 122 if offset < 122 else 0})
        self.assertEqual(len(await qq_reader.collect_pages(fetch)), 122)
        self.assertEqual(fetch.await_count, 16)

    async def test_empty_pool_is_readable(self):
        self.assertEqual(await qq_reader.collect_pages(AsyncMock(return_value={"total": 0})), [])

    async def test_duplicate_page_and_missing_page_rejected(self):
        rows = [raw(i + 1) for i in range(20)]
        for pages in ([{"comments": rows, "total": 40}, {"comments": rows, "total": 40}],
                      [{"comments": rows, "total": 40}, {"total": 0}],
                      [{"total": 4}]):
            with self.subTest(pages=len(pages)), self.assertRaises(core.LotteryError):
                await qq_reader.collect_pages(AsyncMock(side_effect=pages))

    async def test_total_change_rejected(self):
        pages = [{"comments": [raw(i) for i in range(20)], "total": 21},
                 {"comments": [raw(20)], "total": 22}]
        with self.assertRaises(core.LotteryError):
            await qq_reader.collect_pages(AsyncMock(side_effect=pages))

    async def test_middle_page_change_even_when_total_unchanged_rejected(self):
        rows = [raw(i + 1) for i in range(41)]
        requests = 0
        async def fetch(offset):
            nonlocal requests
            requests += 1
            selected = copy.deepcopy(rows[offset:offset + 20])
            if requests > 4 and offset == 20:
                selected[0]["poster"]["id"] = B
            return {"comments": selected, "total": 41 if selected else 0}
        with self.assertRaisesRegex(core.LotteryError, "两遍"):
            await qq_reader.collect_pages(fetch)

    async def test_read_limit_rejected_before_collecting(self):
        fetch = AsyncMock(return_value={"comments": [raw(1)], "total": 2001})
        with self.assertRaises(core.LotteryError):
            await qq_reader.collect_pages(fetch)
        self.assertEqual(fetch.await_count, 1)

    async def test_credentials_and_fixed_read_only_endpoint(self):
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value={"cookies": "p_skey=TEST_COOKIE; skey=OTHER"}))
        recorded = []
        def page(params, headers):
            recorded.append((copy.deepcopy(params), copy.deepcopy(headers)))
            return {"comments": [raw(1)] if params["start"] == 0 else [], "total": 1 if params["start"] == 0 else 0}
        with patch.object(qq_reader, "http_page", page):
            result = await qq_reader.read_comments(bot, "100000003", "100000001", "Album_1", "2147483648")
        bot.call_action.assert_awaited_once_with("get_credentials", domain="qzone.qq.com")
        self.assertEqual(recorded[0][0]["topicId"], "100000001|Album_1|2147483648")
        self.assertEqual(recorded[0][0]["cmtType"], 4)
        self.assertNotIn("TEST_COOKIE", json.dumps([row.__dict__ for row in result]))

    async def test_qq_encoded_album_id_preserved_in_comment_request(self):
        album = "V61DemoAlbum*abc!def~ghi="
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value={"cookies": "p_skey=TEST"}))
        recorded = []
        def page(params, headers):
            recorded.append(params.copy())
            return {"comments": [], "total": 0}
        with patch.object(qq_reader, "http_page", page):
            self.assertEqual(await qq_reader.read_comments(bot, "100000003", "100000001", album, "2147483648"), [])
        self.assertEqual(recorded[0]["topicId"], "100000001|" + album + "|2147483648")
        encoded = qq_reader.urllib.parse.urlencode(recorded[0])
        self.assertEqual(qq_reader.urllib.parse.parse_qs(encoded)["topicId"][0], recorded[0]["topicId"])

    def test_json_and_jsonp_not_executed(self):
        data = '{"code":0,"data":{"total":0}}'
        for text in (data, "shine0_Callback(" + data + ");"):
            self.assertEqual(qq_reader.decode_response(text), {"total": 0})
        for text in ("evil(); " + data, 'callback(' + data + ');evil()', '{"code":-4404}', '{"code":false,"data":{}}'):
            with self.assertRaises(core.LotteryError):
                qq_reader.decode_response(text)

    def test_no_redirect(self):
        self.assertIsNone(qq_reader.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_http_transport_jsonp_redirect_size_limit_and_redaction(self):
        reached = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                reached.append(self.path.split("?")[0])
                route = reached[-1]
                if route == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-receive-cookie")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 201 if route == "/large" else
                                     b'callback({"code":0,"data":{"total":0}});')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_address[1])
            with patch.object(qq_reader, "ENDPOINT", base + "/ok"):
                self.assertEqual(qq_reader.http_page({}, {"Cookie": "secret=FAKE"}), {"total": 0})
            with patch.object(qq_reader, "ENDPOINT", base + "/redirect"), self.assertRaises(core.LotteryError) as error:
                qq_reader.http_page({"g_tk": "FAKE_TOKEN"}, {"Cookie": "secret=FAKE"})
            self.assertNotIn("FAKE", str(error.exception))
            self.assertNotIn("/should-not-receive-cookie", reached)
            with patch.object(qq_reader, "ENDPOINT", base + "/large"), patch.object(qq_reader, "MAX_BODY", 100), self.assertRaises(core.LotteryError):
                qq_reader.http_page({}, {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bad_cookie_rejected(self):
        for cookie in (None, "", "foo=bar", "p_skey=abc\r\nBad: x"):
            with self.assertRaises(core.LotteryError):
                qq_reader.csrf_token(cookie)
        self.assertNotEqual(qq_reader.csrf_token("p_skey=a; skey=b"), qq_reader.csrf_token("skey=b"))

    async def test_napcat_error_message_is_redacted(self):
        secret = "cookie=PRIVATE_SECRET"
        bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError(secret)))
        with self.assertRaises(core.LotteryError) as error:
            await qq_reader.call_action(bot, "get_credentials")
        self.assertNotIn(secret, str(error.exception))

    async def test_unwrapped_and_enveloped_results(self):
        self.assertEqual(qq_reader.unwrap({"status": "ok", "data": [1]}), [1])
        self.assertEqual(qq_reader.unwrap([1]), [1])
        with self.assertRaises(core.LotteryError):
            qq_reader.unwrap({"status": "failed", "message": "SECRET"})

    async def test_list_pagination_guards(self):
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value={"album_list": [{"id": "a"}], "has_more": True, "attach_info": "cursor"}))
        rows, cursor = await qq_reader.list_page(bot, "100000001")
        self.assertEqual(cursor, "cursor")
        with self.assertRaises(core.LotteryError):
            await qq_reader.list_page(bot, "100000001", cursor=cursor)
        self.assertEqual(rows[0]["id"], "a")


class StorageTests(unittest.TestCase):
    def test_restart_keeps_draw_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory))
            state = store.load()
            current = pool([raw("a"), raw("cut", HOST, START + 10)])
            core.commit_draw(current, 1)
            state["pools"]["test"] = current
            store.save(state)
            reloaded = core.Store(Path(directory)).load()["pools"]["test"]
            self.assertEqual(core.commit_draw(reloaded, 1), [A])

    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory))
            state = store.load()
            store.save(state)
            original = store.path.read_bytes()
            state["active"]["new"] = "fake"
            with patch.object(core.os, "replace", side_effect=OSError("disk full")), self.assertRaises(core.LotteryError):
                store.save(state)
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_corrupt_file_is_never_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory))
            store.path.write_text("broken", encoding="utf-8")
            with self.assertRaises(core.LotteryError):
                store.load()
            self.assertEqual(store.path.read_text(), "broken")


# 最小框架替身只用于本机离线指令测试。真正的 AstrBot 加载须在部署时验证。
def install_astrbot_stubs():
    def decorator(*args, **kwargs):
        return lambda function: function
    class FakeStar:
        def __init__(self, context):
            self.context = context
    class FakeEvent:
        def __init__(self, bot, sender=HOST, group="100000001", admin=True, bot_qq="100000003"):
            self.bot, self.sender, self.group, self.admin = bot, sender, group, admin
            self.message_obj = types.SimpleNamespace(self_id=bot_qq)
        def get_group_id(self): return self.group
        def get_sender_id(self): return self.sender
        def is_admin(self): return self.admin
        def stop_event(self): pass
        def plain_result(self, text): return text
    package = "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star", package):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["astrbot.api"].logger = types.SimpleNamespace(warning=lambda text: None)
    sys.modules["astrbot.api.event"].AstrMessageEvent = FakeEvent
    sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(command=decorator, permission_type=decorator,
                                                                   PermissionType=types.SimpleNamespace(ADMIN="admin"))
    sys.modules["astrbot.api.star"].Context = object
    sys.modules["astrbot.api.star"].Star = FakeStar
    sys.modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda: Path("unused"))
    sys.modules["astrbot.api.star"].register = decorator
    sys.modules[package].AiocqhttpMessageEvent = FakeEvent
    return FakeEvent


FakeEvent = install_astrbot_stubs()
from astrbot_plugin_xiaosui_album_lottery import main


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.object(main.StarTools, "get_data_dir", return_value=Path(self.directory.name)):
            self.plugin = main.XiaosuiAlbumLottery(object())
        self.bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=lambda action, **kw:
            {"album_list": [{"id": "album", "name": "群相册"}]} if action == "get_qun_album_list" else
            {"media_list": [{"image": {}, "upload_time": START - 100, "batch_id": "2147483648"}]}))
        self.event = FakeEvent(self.bot)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def command(self, operation, arg1="", arg2="", arg3="", event=None):
        return [text async for text in self.plugin.album_lottery(event or self.event, operation, arg1, arg2, arg3)][0]

    async def create_pool(self):
        await self.command("相册")
        await self.command("图片", "1")
        return await self.command("建池", "1", core.display_time(START).replace(" ", "T"))

    async def test_qq_encoded_album_id_listing_selection_and_pool(self):
        album = "V61DemoAlbum*abc!def~ghi="
        self.bot.call_action = AsyncMock(side_effect=lambda action, **kw:
            {"album_list": [{"album_id": album, "name": "群相册"}]} if action == "get_qun_album_list" else
            {"media_list": [{"image": {}, "upload_time": START - 100, "batch_id": "2147483648"}]})
        self.assertIn("1. 群相册", await self.command("相册"))
        self.assertIn("上传", await self.command("图片", "1"))
        self.bot.call_action.assert_awaited_with("get_group_album_media_list", group_id="100000001",
                                               attach_info="", album_id=album)
        self.assertIn("已经建池", await self.command("建池", "1", core.display_time(START).replace(" ", "T")))
        self.assertEqual(next(iter(self.plugin.store.load()["pools"].values()))["album"]["id"], album)

    async def test_complete_flow_restart_and_idempotent_result(self):
        await self.create_pool()
        comments = [core.normalize_comment(raw("a")), core.normalize_comment(raw("cut", HOST, START + 10))]
        with patch.object(main, "read_comments", AsyncMock(return_value=comments)):
            self.assertIn("1人", await self.command("统计"))
        self.assertIn(A, await self.command("名单"))
        self.assertIn("已锁定", await self.command("确认"))
        result = await self.command("抽取", "1")
        with patch.object(main.StarTools, "get_data_dir", return_value=Path(self.directory.name)):
            self.plugin = main.XiaosuiAlbumLottery(object())
        self.assertEqual(await self.command("抽取", "1"), result)
        self.assertIn("不能改变人数", await self.command("抽取", "2"))
        state = self.plugin.store.load()
        saved_pool = next(iter(state["pools"].values()))
        self.assertNotIn("cookies", json.dumps(saved_pool))
        self.assertIn("已结束", await self.command("结束", saved_pool["id"]))
        self.assertIn(A, await self.command("记录", saved_pool["id"]))

    async def test_non_admin_does_not_read_or_modify(self):
        denied = await self.command("相册", event=FakeEvent(self.bot, admin=False))
        self.assertIn("管理员", denied)
        self.bot.call_action.assert_not_awaited()
        self.assertFalse(self.plugin.store.path.exists())

    async def test_other_group_bot_and_user_cannot_use_selection(self):
        await self.command("相册")
        for event in (FakeEvent(self.bot, group="100000002"), FakeEvent(self.bot, sender=B),
                      FakeEvent(self.bot, bot_qq="1123456789")):
            self.assertIn("列表已过期", await self.command("图片", "1", event=event))

    async def test_other_group_cannot_read_pool_record(self):
        await self.create_pool()
        saved_pool = next(iter(self.plugin.store.load()["pools"].values()))
        self.assertIn("没有这个池", await self.command("记录", saved_pool["id"], event=FakeEvent(self.bot, group="100000002")))

    async def test_private_chat_rejected(self):
        self.assertIn("QQ 群", await self.command("相册", event=FakeEvent(self.bot, group="")))

    async def test_refresh_failure_invalidates_old_preview(self):
        await self.create_pool()
        comments = [core.normalize_comment(raw("a")), core.normalize_comment(raw("cut", HOST, START + 10))]
        with patch.object(main, "read_comments", AsyncMock(return_value=comments)):
            await self.command("统计")
        with patch.object(main, "read_comments", AsyncMock(side_effect=core.LotteryError("读取失败"))):
            self.assertEqual(await self.command("统计"), "读取失败")
        self.assertIn("快照缺失", await self.command("确认"))
        self.assertIn("请先", await self.command("抽取"))

    async def test_draw_save_failure_does_not_publish_result(self):
        await self.create_pool()
        comments = [core.normalize_comment(raw("a")), core.normalize_comment(raw("cut", HOST, START + 10))]
        with patch.object(main, "read_comments", AsyncMock(return_value=comments)):
            await self.command("统计")
        await self.command("确认")
        with patch.object(core.os, "replace", side_effect=OSError("failure")):
            response = await self.command("抽取")
        self.assertNotIn(A, response)
        self.assertIn("保存失败", response)
        self.assertEqual(next(iter(self.plugin.store.load()["pools"].values()))["status"], "confirmed")

    async def test_busy_does_not_queue_duplicate_commands(self):
        async with self.plugin.lock:
            self.assertIn("稍后再试", await self.command("相册"))
        self.bot.call_action.assert_not_awaited()

    async def test_selection_expires(self):
        await self.command("相册")
        next(iter(self.plugin.selections.values()))["updated"] -= 601
        self.assertIn("列表已过期", await self.command("图片", "1"))

    async def test_older_astrbot_directory_fallback_keeps_data_outside_plugin(self):
        path = Path(self.directory.name) / "data" / "plugins" / main.PLUGIN / "main.py"
        with patch.object(main, "StarTools", None), patch.object(main, "__file__", str(path)):
            self.assertEqual(main.data_directory(), Path(self.directory.name) / "data" / "plugin_data" / main.PLUGIN)
        with patch.object(main, "StarTools", None), patch.object(main, "__file__", str(Path(self.directory.name) / "wrong" / "main.py")), self.assertRaises(RuntimeError):
            main.data_directory()

    async def test_same_second_boundary_confirmation_flow(self):
        await self.create_pool()
        comments = [core.normalize_comment(row) for row in [raw("a"), raw("amb", B, START + 10), raw("cut", HOST, START + 10)]]
        with patch.object(main, "read_comments", AsyncMock(return_value=comments)):
            await self.command("统计")
        self.assertIn("待核对", await self.command("确认"))
        self.assertIn("amb", await self.command("边界"))
        self.assertIn("2人", await self.command("边界", "amb", "纳入"))
        self.assertIn("已锁定", await self.command("确认"))
        self.assertIn("只有尚未确认", await self.command("边界", "amb", "排除"))
        result = await self.command("抽取", "2")
        self.assertIn(A, result)
        self.assertIn(B, result)

    async def test_photo_local_second_page_preserves_target(self):
        self.bot.call_action = AsyncMock(side_effect=lambda action, **kw:
            {"album_list": [{"id": "album", "name": "群相册"}]} if action == "get_qun_album_list" else
            {"media_list": [{"image": {}, "upload_time": START - 100, "batch_id": str(i + 1000)} for i in range(25)]})
        await self.command("相册")
        await self.command("图片", "1")
        self.assertIn("21.", await self.command("图片", "1", "2"))
        await self.command("建池", "21", core.display_time(START).replace(" ", "T"))
        self.assertEqual(next(iter(self.plugin.store.load()["pools"].values()))["batch"], "1020")


if __name__ == "__main__":
    unittest.main()
