import asyncio
import logging
import random
from datetime import date
from typing import Optional, Any

from aiocqhttp import CQHttp
import aiocqhttp.exceptions

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.permission import PermissionType

logger = logging.getLogger(__name__)

# ---------- 默认回复模板 ----------
# 用户可通过 config 覆盖，格式同下方列表

DEFAULT_SUCCESS_RESPONSES = [
    "👍{total_likes}",
    "赞了赞了",
    "点赞成功！",
    "给{username}点了{total_likes}个赞",
    "赞送出去啦！一共{total_likes}个哦！",
    "为{username}点赞成功！总共{total_likes}个！",
    "点了{total_likes}个，快查收吧！",
    "赞已送达，请注意查收~ 一共{total_likes}个！",
    "给{username}点了{total_likes}个赞，记得回赞哟！",
    "赞了{total_likes}次，看看收到没？",
    "点了{total_likes}赞，没收到可能是我被风控了",
]

DEFAULT_LIMIT_RESPONSES = [
    "今天给{username}的赞已达上限",
    "赞了那么多还不够吗？",
    "{username}别太贪心哟~",
    "今天赞过啦！",
    "今天已经赞过啦~",
    "已经赞过啦~",
    "还想要赞？不给了！",
    "已经赞过啦，别再点啦！",
]

DEFAULT_PERMISSION_RESPONSES = [
    "你设了权限不许陌生人赞你",
    "对方权限设置无法点赞",
    "没赞成功，对方可能限制了陌生人点赞",
]

DEFAULT_STRANGER_RESPONSES = [
    "点赞失败啦，可能是被风控了",
    "呜呜呜点赞没成功，服务器不理我",
    "赞没送出去，可能是网络或风控限制",
]


def _render_template(template: str, **kwargs: str) -> str:
    """渲染模板字符串，替换 {key} 占位符。"""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


@register(
    "astrbot_plugin_zanwo",
    "Futureppo",
    "发送 赞我 自动点赞（重写版，降低风控 & 更稳定）",
    "1.1.1",
    "https://github.com/Futureppo/astrbot_plugin_zanwo",
)
class zanwo(Star):
    """QQ 点赞插件：支持手动「赞我」和自动订阅点赞。

    功能列表：
    - 赞我 / 赞@某人：给指定用户点赞
    - 订阅点赞：每天自动为订阅用户点赞
    - 给所有订阅用户点赞：手动触发批量点赞
    - 谁赞了bot：查看 bot 的点赞列表（管理员）

    配置项：
    - white_list_groups: 群聊白名单
    - subscribed_users: 订阅点赞用户列表
    - like_times: 单次点赞次数（默认 5）
    - max_retry: 失败最大重试次数（默认 2）
    - like_delay: 每次点赞间隔秒数，0=随机 1~3s（默认 2.0）
    - daily_like_limit: 每日每人点赞上限（默认 30）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._auto_like_tasks: set[asyncio.Task] = set()
        self._like_lock = asyncio.Lock()

        # 群聊白名单
        self.white_list_groups: list[str] = config.get("white_list_groups", [])
        # 订阅点赞的用户ID列表
        self.subscribed_users: list[str] = config.get("subscribed_users", [])

        # 配置项
        self.like_times: int = int(config.get("like_times", 5))
        self.max_retry: int = int(config.get("max_retry", 2))
        self.like_delay: float = float(config.get("like_delay", 2.0))
        self.daily_like_limit: int = int(config.get("daily_like_limit", 30))

        # 回复模板（可通过 config 覆盖）
        self.success_responses: list[str] = config.get(
            "success_responses", DEFAULT_SUCCESS_RESPONSES
        )
        self.limit_responses: list[str] = config.get(
            "limit_responses", DEFAULT_LIMIT_RESPONSES
        )
        self.permission_responses: list[str] = config.get(
            "permission_responses", DEFAULT_PERMISSION_RESPONSES
        )
        self.stranger_responses: list[str] = config.get(
            "stranger_responses", DEFAULT_STRANGER_RESPONSES
        )

        # 自动点赞记录：{ user_id_str: { "date": "YYYY-MM-DD", "count": int } }
        self.like_records: dict[str, dict[str, Any]] = config.get("like_records", {})

        # 上次自动点赞日期
        self.zanwo_date: Optional[str] = config.get("zanwo_date", None)
def _is_group_allowed(self, event: AiocqhttpMessageEvent) -> bool:
        group_id = event.get_group_id()
        if group_id and self.white_list_groups:
            return str(group_id) in self.white_list_groups
        return True

    async def _get_username(self, client: CQHttp, user_id: str) -> str:
        try:
            info = await client.get_stranger_info(user_id=int(user_id))
            return info.get("nickname", "未知用户")
        except Exception as e:
            logger.warning("获取用户 %s 昵称失败: %s", user_id, e)
            return "未知用户"

    def _render(self, template: str, username: str = "", total_likes: str = "") -> str:
        return _render_template(
            template, username=username, total_likes=total_likes
        )

    def _pick_response(self, templates: list[str], **kwargs: str) -> str:
        return self._render(random.choice(templates), **kwargs)

    def _get_today_like_count(self, user_id: str) -> int:
        rec = self.like_records.get(user_id)
        if not rec:
            return 0
        today_str = date.today().strftime("%Y-%m-%d")
        if rec.get("date") != today_str:
            self.like_records[user_id] = {"date": today_str, "count": 0}
            self.config["like_records"] = self.like_records
            self.config.save_config()
            return 0
        return int(rec.get("count", 0))

    async def _add_today_like_count(self, user_id: str, count: int = 1) -> None:
        async with self._like_lock:
            today_str = date.today().strftime("%Y-%m-%d")
            rec = self.like_records.get(user_id)
            if not rec or rec.get("date") != today_str:
                self.like_records[user_id] = {"date": today_str, "count": count}
            else:
                rec["count"] = int(rec.get("count", 0)) + count
            self.config["like_records"] = self.like_records
            self.config.save_config()

    def _save_zanwo_date(self, date_value: str) -> None:
        self.zanwo_date = date_value
        self.config["zanwo_date"] = date_value
        self.config.save_config()

    # ------------------------------------------------------------------ #
    #  点赞核心逻辑
    # ------------------------------------------------------------------ #

    async def _like_single_user(
        self, client: CQHttp, user_id: str, username: str
    ) -> tuple[bool, str]:
        already = self._get_today_like_count(user_id)
        if already >= self.daily_like_limit:
            return False, self._pick_response(
                self.limit_responses, username=username
            )

        success_count = 0
        last_error_reply = ""

        for attempt in range(1, self.max_retry + 1):
            try:
                delay = self.like_delay if self.like_delay > 0 else random.uniform(1.0, 3.0)
                await asyncio.sleep(delay)

                await client.send_like(user_id=int(user_id), times=self.like_times)
                success_count += self.like_times
                await self._add_today_like_count(user_id, self.like_times)
                logger.info(
                    "成功给用户 %s 点赞 %d 次（第 %d 次尝试），今日累计 %d 次",
                    user_id, self.like_times, attempt,
                    self._get_today_like_count(user_id),
                )
                break

            except aiocqhttp.exceptions.ActionFailed as e:
                error_message = str(e)
                if "已达" in error_message:
                    last_error_reply = self._pick_response(
                        self.limit_responses, username=username
                    )
                    logger.info("用户 %s 今日已达点赞上限: %s", user_id, error_message)
                    break
                elif "权限" in error_message or "好友" in error_message:
                    last_error_reply = self._pick_response(
                        self.permission_responses, username=username
                    )logger.info("用户 %s 权限/好友限制: %s", user_id, error_message)
                    break
                else:
                    logger.warning(
                        "给用户 %s 点赞失败（第 %d 次）: %s",
                        user_id, attempt, error_message,
                    )
                    last_error_reply = self._pick_response(
                        self.stranger_responses, username=username
                    )
                    continue

            except Exception as e:
                logger.error("给用户 %s 点赞时出现未知异常: %s", user_id, e, exc_info=True)
                last_error_reply = self._pick_response(
                    self.stranger_responses, username=username
                )
                break

        if success_count > 0:
            return True, self._pick_response(
                self.success_responses,
                username=username,
                total_likes=str(success_count),
            )
        else:
            return False, last_error_reply

    async def _run_like(
        self, event: AiocqhttpMessageEvent, target_ids: list[str]
    ) -> Optional[str]:
        if not self._is_group_allowed(event):
            return None
        if not target_ids:
            return None

        client = event.bot
        replys = []
        for uid in target_ids:
            username = await self._get_username(client, uid)
            ok, reply = await self._like_single_user(client, uid, username)
            replys.append(reply)

        return "\n".join(replys).strip() if replys else None

    # ------------------------------------------------------------------ #
    #  自动点赞
    # ------------------------------------------------------------------ #

    async def _trigger_auto_like(self, client: CQHttp):
        today_str = date.today().strftime("%Y-%m-%d")
        if self.zanwo_date == today_str:
            return

        subscribed_users = list(self.subscribed_users)
        if not subscribed_users:
            return

        auto_like_ids = []
        for uid in subscribed_users:
            already = self._get_today_like_count(uid)
            if already < self.daily_like_limit:
                auto_like_ids.append(uid)

        if not auto_like_ids:
            logger.info("所有订阅用户今日点赞次数已达安全上限，不执行自动点赞")
            self._save_zanwo_date(today_str)
            return

        for uid in auto_like_ids:
            username = await self._get_username(client, uid)
            ok, reply = await self._like_single_user(client, uid, username)
            logger.info("自动点赞用户 %s: %s", uid, reply)

        self._save_zanwo_date(today_str)

    def _handle_auto_like_task(self, task: asyncio.Task) -> None:
        self._auto_like_tasks.discard(task)
        try:
            task.result()
        except Exception:
            logger.exception("Auto-like task failed")

    def _schedule_auto_like(self, client: CQHttp) -> None:
        if not self.subscribed_users:
            return
        task = asyncio.create_task(self._trigger_auto_like(client))
        self._auto_like_tasks.add(task)
        task.add_done_callback(self._handle_auto_like_task)

    # ------------------------------------------------------------------ #
    #  消息处理入口
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
        messages = event.get_messages()
        self_id = event.get_self_id()
        return [
            str(seg.qq)
            for seg in messages
            if (isinstance(seg, Comp.At) and str(seg.qq) != self_id)
        ]

    @filter.regex(r"^赞[我@]")
    async def like_me(self, event: AiocqhttpMessageEvent):
        target_ids = []
        if event.message_str == "赞我":
            target_ids.append(event.get_sender_id())
        if not target_ids:
            target_ids = self.get_ats(event)

        if not target_ids:
            yield event.plain_result("请发送「赞我」或「赞@某人」来点赞哦~")
return

        result = await self._run_like(event, target_ids)
        if not result:
            if not self._is_group_allowed(event):
                yield event.plain_result("当前群不在白名单中，无法使用点赞功能")
            return
        yield event.plain_result(result)
        self._schedule_auto_like(event.bot)

    @filter.llm_tool(name="like_qq_profile")
    async def like_qq_profile(self, event: AiocqhttpMessageEvent, target: str = "self"):
        normalized_target = target.strip().lower() if target else "self"
        if normalized_target in {"", "self", "me", "我", "自己", "我自己"}:
            target_ids = [event.get_sender_id()]
        elif target.strip().isdigit():
            target_ids = [target.strip()]
        else:
            return "只能给当前发言者点赞，或给明确提供的 QQ 号点赞。"

        result = await self._run_like(event, target_ids)
        if not result:
            return "当前会话不允许使用点赞功能。"
        self._schedule_auto_like(event.bot)
        return result

    # ------------------------------------------------------------------ #
    #  订阅管理命令
    # ------------------------------------------------------------------ #

    @filter.command("订阅点赞")
    async def subscribe_like(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        if sender_id in self.subscribed_users:
            yield event.plain_result("你已经订阅点赞了哦~")
            return
        self.subscribed_users.append(sender_id)
        self.config["subscribed_users"] = self.subscribed_users
        self.config.save_config()
        yield event.plain_result("订阅成功！我将每天自动为你点赞")

    @filter.command("取消订阅点赞")
    async def unsubscribe_like(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        if sender_id not in self.subscribed_users:
            yield event.plain_result("你还没有订阅点赞哦~")
            return
        self.subscribed_users.remove(sender_id)
        self.config["subscribed_users"] = self.subscribed_users
        self.config.save_config()
        yield event.plain_result("已取消订阅！我将不再自动给你点赞")

    @filter.command("订阅点赞列表")
    async def like_list(self, event: AiocqhttpMessageEvent):
        if not self.subscribed_users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return
        users_str = "\n".join(self.subscribed_users).strip()
        yield event.plain_result(f"当前订阅点赞的用户ID列表：\n{users_str}")

    @filter.command("给所有订阅用户点赞")
    async def like_all_subscribed(self, event: AiocqhttpMessageEvent):
        if not self.subscribed_users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return

        yield event.plain_result(f"开始给 {len(self.subscribed_users)} 位订阅用户点赞，请稍候...")

        client = event.bot
        replys = []
        for uid in self.subscribed_users:
            username = await self._get_username(client, uid)
            ok, reply = await self._like_single_user(client, uid, username)
            replys.append(f"【{username}】: {reply}")

        yield event.plain_result("点赞结果：\n" + "\n".join(replys))

    # ------------------------------------------------------------------ #
    #  管理员命令
    # ------------------------------------------------------------------ #

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("谁赞了bot", alias={"谁赞了你"})
    async def get_profile_like(self, event: AiocqhttpMessageEvent):
        client = event.bot
        data = await client.get_profile_like()
        reply = ""
        user_infos = data.get("favoriteInfo", {}).get("userInfos", [])
        for user in user_infos:
            if (
                "nick" in user
                and user["nick"]
                and "count" in user
                and user["count"] > 0
            ):
                reply += f"\n【{user['nick']}】赞了我{user['count']}次"
        if not reply:
            reply = "暂无有效的点赞信息"
        try:
            url = await self.text_to_image(reply)
            yield event.image_result(url)
except (AttributeError, NotImplementedError):
            yield event.plain_result(reply)