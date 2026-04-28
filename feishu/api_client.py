import json
import logging
import os
import re
import time
from datetime import datetime
from typing import List, Optional

import httpx

from memory.schemas import EvidenceMessage

logger = logging.getLogger(__name__)

# 进程级昵称缓存：open_id → 显示名称，避免对同一用户重复调用接口
_name_cache: dict[str, str] = {}

def extract_open_id(mention) -> str:
    """
    从飞书消息的 mention 元素中提取 open_id，兼容所有来源格式：
    - 字符串：消息列表 API 直接返回 open_id 字符串
    - dict {"id": {"open_id": "..."}}: WebSocket 事件 content.mentions 格式
    - dict {"id": "..."}  / {"open_id": "..."}: 其他简化格式
    - SDK 对象：lark-oapi mention 对象，id 为嵌套属性
    """
    if isinstance(mention, str):
        return mention

    if isinstance(mention, dict):
        id_field = mention.get("id")
        if isinstance(id_field, dict):
            return id_field.get("open_id") or id_field.get("user_id") or mention.get("open_id") or ""
        if isinstance(id_field, str):
            return id_field or mention.get("open_id") or ""
        return mention.get("open_id") or mention.get("user_id") or ""

    # SDK 对象（lark-oapi）
    id_attr = getattr(mention, "id", None)
    if id_attr is not None:
        if hasattr(id_attr, "open_id"):
            return getattr(id_attr, "open_id", "") or ""
        if isinstance(id_attr, str):
            return id_attr
    return getattr(mention, "open_id", None) or getattr(mention, "user_id", None) or ""


# 明显疑问句模式（批处理侧过滤用）
_QUERY_RE = re.compile(
    r"为什么|怎么定的|之前.*怎么|谁说的|原话|依据是|来着[？?]?$|到底.*怎么定|"
    r"是不是.*讨论过|之前.*说|查一下|帮我查"
)


class FeishuAPIClient:
    _token: str = ""
    _token_expires_at: float = 0.0

    def __init__(self):
        self.app_id = os.getenv("FEISHU_APP_ID", "")
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "")
        self.base = "https://open.feishu.cn/open-apis"

    async def _get_token(self) -> str:
        if self._token and time.time() < FeishuAPIClient._token_expires_at - 60:
            return FeishuAPIClient._token

        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.post(
                f"{self.base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            FeishuAPIClient._token = data["tenant_access_token"]
            FeishuAPIClient._token_expires_at = time.time() + data["expire"]
            return FeishuAPIClient._token

    async def get_user_name(self, open_id: str) -> str:
        """
        查询用户昵称，结果缓存到 _name_cache。
        需要 contact:user.base:readonly 权限。
        查询失败时回退到 open_id。
        """
        if open_id in _name_cache:
            return _name_cache[open_id]

        token = await self._get_token()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                resp = await client.get(
                    f"{self.base}/contact/v3/users/{open_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"user_id_type": "open_id"},
                )
            data = resp.json()
            if data.get("code") == 0:
                name = data.get("data", {}).get("user", {}).get("name", open_id)
            else:
                logger.warning("get_user_name 失败 | open_id=%s msg=%s", open_id, data.get("msg"))
                name = open_id
        except Exception:
            logger.exception("get_user_name 请求异常 | open_id=%s", open_id)
            name = open_id

        _name_cache[open_id] = name
        return name

    async def _resolve_sender_names(self, messages: List[EvidenceMessage]) -> None:
        """批量解析消息列表中所有发送者的昵称（跳过已有名称的条目）。"""
        unique_ids = {m.sender_id for m in messages if not m.sender_name and m.sender_id}
        for open_id in unique_ids:
            name = await self.get_user_name(open_id)
            for m in messages:
                if m.sender_id == open_id:
                    m.sender_name = name

    async def send_text(self, chat_id: str, text: str):
        token = await self._get_token()
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.post(
                f"{self.base}/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.error("Feishu send_text failed: %s", result)

    async def fetch_messages(
        self,
        chat_id: str,
        start_time: Optional[datetime] = None,
        page_size: int = 50,
    ) -> tuple[List[EvidenceMessage], Optional[datetime]]:
        """
        增量拉取群聊历史消息。

        返回 (有效消息列表, 最后一条原始消息的时间戳)。
        第二个返回值包含被过滤掉的消息（机器人回复、查询语句），
        用于游标更新，确保下次轮询不重复拉取已处理过的消息。

        过滤顺序（时间戳提取在所有过滤之前，确保游标能越过被过滤的消息）：
          1. 非文本消息
          2. 机器人自己发的消息（sender_type == "app"）
          3. @机器人 的查询消息（mentions 含 bot open_id）
          4. 明显疑问句（正则匹配）—— 解析 body 后过滤
        """
        token = await self._get_token()
        params: dict = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeAsc",
            "page_size": page_size,
        }
        if start_time:
            params["start_time"] = str(int(start_time.timestamp()))

        messages: List[EvidenceMessage] = []
        last_raw_ts: Optional[datetime] = None   # 最后一条原始消息的时间戳（含被过滤的）
        page_token: Optional[str] = None
        bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()

        for _ in range(2):  # 最多拉取两页
            if page_token:
                params["page_token"] = page_token

            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                resp = await client.get(
                    f"{self.base}/im/v1/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("fetch_messages failed: %s", data)
                break

            for item in data.get("data", {}).get("items", []):
                # 时间戳最先提取，确保无论此消息是否被过滤，游标都能推进
                raw_ts_val = int(item.get("create_time", "0")) / 1000
                if raw_ts_val > 0:
                    last_raw_ts = datetime.fromtimestamp(raw_ts_val)

                if item.get("msg_type") != "text":
                    continue

                sender = item.get("sender", {})

                # 过滤 1：机器人自己发的消息（body 可能格式特殊，直接跳过）
                if sender.get("sender_type") == "app":
                    logger.debug("跳过机器人消息 | msg_id=%s", item.get("message_id"))
                    continue

                # 过滤 2：@机器人 的查询消息（在解析 body 之前过滤）
                if bot_open_id:
                    if any(extract_open_id(m) == bot_open_id for m in item.get("mentions", [])):
                        logger.debug("跳过 @机器人 查询消息 | msg_id=%s", item.get("message_id"))
                        continue

                # 解析消息体（仅对通过前两道过滤的消息）
                try:
                    raw_content = (item.get("body") or {}).get("content") or ""
                    body = json.loads(raw_content.strip() or "{}")
                    text = body.get("text", "").strip()
                    if not text:
                        continue

                    # 过滤 3：明显疑问句
                    if _QUERY_RE.search(text):
                        logger.debug("跳过疑问句消息 | text=%s", text[:40])
                        continue

                    messages.append(EvidenceMessage(
                        message_id=item["message_id"],
                        sender_id=sender.get("id", ""),
                        sender_name="",
                        timestamp=last_raw_ts,
                        text=text,
                    ))
                except Exception:
                    logger.debug("消息体解析失败，跳过 | msg_id=%s", item.get("message_id"))

            if not data.get("data", {}).get("has_more"):
                break
            page_token = data.get("data", {}).get("page_token")

        await self._resolve_sender_names(messages)
        logger.info("Fetched %d messages for chat %s", len(messages), chat_id)
        return messages, last_raw_ts

    async def get_bot_open_id(self) -> str:
        """
        调用 GET /bot/v3/info 获取机器人自身的 open_id。
        无需额外权限，用 tenant_access_token 即可。
        """
        token = await self._get_token()
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            resp = await client.get(
                f"{self.base}/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("get_bot_open_id 失败: %s", data.get("msg"))
            return ""
        return data.get("bot", {}).get("open_id", "")

    async def get_chat_info(self, chat_id: str) -> dict:
        """获取群聊基本信息（名称等），用于初始化 ChatMemorySpace。"""
        token = await self._get_token()
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            resp = await client.get(
                f"{self.base}/im/v1/chats/{chat_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("get_chat_info failed: %s", data)
            return {}
        return data.get("data", {})
