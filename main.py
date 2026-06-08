import asyncio
import logging
import random
from datetime import datetime, date
from typing import Optional

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

# ---------- 回复模板 ----------

# 点赞成功回复
success_responses = [
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

# 点赞数到达上限回复
limit_responses = [
    "今天给{username}的赞已达上限",
    "赞了那么多还不够吗？",
    "{username}别太贪心哟~",
    "今天赞过啦！",
    "今天已经赞过啦~",
    "已经赞过啦~",
    "还想要赞？不给了！",
    "已经赞过啦，别再点啦！",
]

# 权限限制回复 (对方设置了不允许陌生人点赞)
permission_responses = [
    "你设了权限不许陌生人赞你",
    "对方权限设置无法点赞",
    "没赞成功，对方可能限制了陌生人点赞",
]

# 陌生人/风控/其他失败回复
stranger_responses = [
    "点赞失败啦，可能是被风控了",
    "呜呜呜点赞没成功，服务器不理我",
    "赞没送出去，可能是网络或风控限制",
    "加我好友了吗就想要我赞你？",
    "滚！",
]


@register(
    "astrbot_plugin_zanwo",
    "Futureppo",
    "发送 赞我 自动点赞（重写版，降低风控 & 更稳定）",
    "1.1.1",
    "https://github.com/Futureppo/astrbot_plugin_zanwo",
)
class zanwo(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.success_responses: list[str] = success_responses
        self._auto_like_tasks = set()

        # 群聊白名单
        self.white_list_groups: list[str] = config.get("white_list_groups", [])
        # 订阅点赞的用户ID列表
        self.subscribed_users: list[str] = config.get("subscribed_users", [])

        # 配置项
        self.like_times: int = int(config.get("like_times", 5))          # 单次点赞次数，默认5
        self.max_retry: int = int(config.get("max_retry", 2))           # 点赞失败最大重试次数
        self.like_delay: float = float(config.get("like_delay", 2.0))   # 每次点赞间隔（秒），0表示随机1~3s

        # 自动点赞相关：记录今天每个用户已成功点赞的次数
        # { user_id_str: { "date": "YYYY-MM-DD", "count": int } }
        self.like_records: dict[str, dict[str, any]] = config.get("like_records", {})

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

    def _get_today_like_count(self, user_id: str) -> int:
        """获取今天已成功给该用户点赞的次数"""
        rec = self.like_records.get(user_id)
        if not rec:
            return 0
        today_str = date.today().strftime("%Y-%m-%d")
        if rec.get("date") != today_str:
            # 不是今天的记录，重置
            self.like_records[user_id] = {"date": today_str, "count": 0}
            self.config["like_records"] = self.like_records
            self.config.save_config()
            return 0
        return int(rec.get("count", 0))

    def _add_today_like_count(self, user_id: str, count: int = 1) -> None:
        """记录今天成功点赞的次数"""
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

    async def _like_single_user(
        self, client: CQHttp, user_id: str, username: str
    ) -> tuple[bool, str]:
        """
        对单个用户执行点赞逻辑。
        返回 (是否成功, 回复文本)
        """
        # 先检查今日已点次数，避免超限
        already = self._get_today_like_count(user_id)
        if already >= 50:  # 常见上限，保守一点
            reply = random.choice(limit_responses)
            if "{username}" in reply:
                reply = reply.replace("{username}", username)
            return False, reply

        # 执行点赞（带重试 & 延迟）
        success_count = 0
        last_error_reply = ""

        for attempt in range(1, self.max_retry + 1):
            try:
                # 随机延迟，降低风控概率
                delay = self.like_delay if self.like_delay > 0 else random.uniform(1.0, 3.0)
                await asyncio.sleep(delay)

                await client.send_like(user_id=int(user_id), times=self.like_times)
                success_count += self.like_times
                self._add_today_like_count(user_id, self.like_times)
                logger.info(
                    "成功给用户 %s 点赞 %d 次（第 %d 次尝试），今日累计 %d 次",
                    user_id,
                    self.like_times,
                    attempt,
                    self._get_today_like_count(user_id),
                )
                # 成功就不再重试
                break

            except aiocqhttp.exceptions.ActionFailed as e:
                error_message = str(e)
                # 根据错误信息分类
                if "已达" in error_message:
                    last_error_reply = random.choice(limit_responses)
                    if "{username}" in last_error_reply:
                        last_error_reply = last_error_reply.replace("{username}", username)
                    logger.info("用户 %s 今日已达点赞上限: %s", user_id, error_message)
                    break  # 不再重试
                elif "权限" in error_message or "好友" in error_message:
                    last_error_reply = random.choice(permission_responses)
                    if "{username}" in last_error_reply:
                        last_error_reply = last_error_reply.replace("{username}", username)
                    logger.info("用户 %s 权限/好友限制: %s", user_id, error_message)
                    break  # 权限问题，重试也没用
                else:
                    # 其他错误（风控、超时等），重试
                    logger.warning(
                        "给用户 %s 点赞失败（第 %d 次）: %s",
                        user_id,
                        attempt,
                        error_message,
                    )
                    last_error_reply = random.choice(stranger_responses)
                    if "{username}" in last_error_reply:
                        last_error_reply = last_error_reply.replace("{username}", username)
                    # 继续重试
                    continue

            except Exception as e:
                logger.error("给用户 %s 点赞时出现未知异常: %s", user_id, e, exc_info=True)
                last_error_reply = random.choice(stranger_responses)
                if "{username}" in last_error_reply:
                    last_error_reply = last_error_reply.replace("{username}", username)
                break  # 非ActionFailed异常，不再重试

        # 判断本次点赞是否有效
        if success_count > 0:
            reply = random.choice(self.success_responses)
            if "{username}" in reply:
                reply = reply.replace("{username}", username)
            if "{total_likes}" in reply:
                reply = reply.replace("{total_likes}", str(success_count))
            return True, reply
        else:
            # 全部失败，使用最后一次的错误回复
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

    async def _trigger_auto_like(self, client: CQHttp):
        today_str = date.today().strftime("%Y-%m-%d")
        if self.zanwo_date == today_str:
            # 今天已经跑过自动点赞，不再重复
            return

        subscribed_users = list(self.subscribed_users)
        if not subscribed_users:
            return

        # 对每个订阅用户，根据今天已点次数决定是否再点
        auto_like_ids = []
        for uid in subscribed_users:
            already = self._get_today_like_count(uid)
            # 假设我们希望每天给每个订阅用户最多点 30 次，留出安全余量
            max_daily_per_user = 30
            if already < max_daily_per_user:
                auto_like_ids.append(uid)

        if not auto_like_ids:
            logger.info("所有订阅用户今日点赞次数已达安全上限，不执行自动点赞")
            self._save_zanwo_date(today_str)
            return

        # 顺序执行，避免并发请求过多
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

    @staticmethod
    def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
        """获取被at者们的id列表"""
        messages = event.get_messages()
        self_id = event.get_self_id()
        return [
            str(seg.qq)
            for seg in messages
            if (isinstance(seg, Comp.At) and str(seg.qq) != self_id)
        ]

    @filter.regex(r"^赞.*")
    async def like_me(self, event: AiocqhttpMessageEvent):
        """给用户点赞"""
        target_ids = []
        if event.message_str == "赞我":
            target_ids.append(event.get_sender_id())
        if not target_ids:
            target_ids = self.get_ats(event)
        result = await self._run_like(event, target_ids)
        if not result:
            return
        yield event.plain_result(result)
        self._schedule_auto_like(event.bot)

    @filter.llm_tool(name="like_qq_profile")
    async def like_qq_profile(self, event: AiocqhttpMessageEvent, target: str = "self"):
        """给 QQ 名片点赞。

        Args:
            target(string): 点赞目标，可填 self、me、我，或明确的 QQ 号。未明确提供时默认给当前发言者点赞。
        """
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

    @filter.command("订阅点赞")
    async def subscribe_like(self, event: AiocqhttpMessageEvent):
        """订阅点赞"""
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
        """取消订阅点赞"""
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
        """查看订阅点赞的用户ID列表"""

        if not self.subscribed_users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return
        users_str = "\n".join(self.subscribed_users).strip()
        yield event.plain_result(f"当前订阅点赞的用户ID列表：\n{users_str}")

    @filter.command("给所有订阅用户点赞")
    async def like_all_subscribed(self, event: AiocqhttpMessageEvent):
        """给所有订阅列表的用户点赞"""
        if not self.subscribed_users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return
        
        # 提前发送提示，避免用户以为卡死了
        yield event.plain_result(f"开始给 {len(self.subscribed_users)} 位订阅用户点赞，请稍候...")
        
        client = event.bot
        replys = []
        for uid in self.subscribed_users:
            username = await self._get_username(client, uid)
            ok, reply = await self._like_single_user(client, uid, username)
            replys.append(f"【{username}】: {reply}")
        
        yield event.plain_result("点赞结果：\n" + "\n".join(replys))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("谁赞了bot", alias={"谁赞了你"})
    async def get_profile_like(self, event: AiocqhttpMessageEvent):
        """获取bot自身点赞列表"""
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
        url = await self.text_to_image(reply)
        yield event.image_result(url)
