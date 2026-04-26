import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


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
