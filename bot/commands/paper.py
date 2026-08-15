# -*- coding: utf-8 -*-
"""Safe personal Paper Account commands for conversation channels.

The command surface deliberately stops at the virtual account boundary.  It
can inspect accounts and approve/reject a proposal; staging and matching are
available through the authenticated Web/API workbench so an accidental chat
message cannot silently create an order.
"""

import json
import logging
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)


class PaperCommand(BotCommand):
    @property
    def name(self) -> str:
        return "paper"

    @property
    def aliases(self) -> List[str]:
        return ["纸面", "影子账户"]

    @property
    def description(self) -> str:
        return "查看 Paper Account，审批或拒绝 Proposal"

    @property
    def usage(self) -> str:
        return "/paper accounts | inspect <account_id> | approve <account_id> <proposal_id> | reject <account_id> <proposal_id>"

    def validate_args(self, args: List[str]) -> str | None:
        if not args:
            return "请指定 accounts、inspect、approve 或 reject"
        action = args[0].lower()
        if action in {"inspect", "查看", "status", "状态"} and len(args) != 2:
            return "inspect/status 需要 account_id"
        if action in {"approve", "批准", "通过", "reject", "拒绝"} and len(args) != 3:
            return "approve/reject 需要 account_id 和 proposal_id"
        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        action = args[0].lower()
        try:
            from src.paper_account.service import PaperAccountService

            service = PaperAccountService()
            if action in {"accounts", "list", "列表", "账户"}:
                accounts = service.list_accounts(include_inactive=False)
                if not accounts:
                    return BotResponse.text_response("📭 暂无 Paper Account")
                lines = ["📒 **Paper Accounts**", ""]
                for item in accounts:
                    account = item["account"]
                    config = item["config"]
                    lines.append(
                        f"• `{account['id']}` {account['name']} "
                        f"[{config.get('state', 'active')}] / {config.get('approval_mode', '')}"
                    )
                return BotResponse.markdown_response("\n".join(lines))

            if action in {"inspect", "查看", "status", "状态"}:
                account_id = int(args[1])
                state = service.inspect(account_id)
                if state is None:
                    return BotResponse.text_response("❌ Paper Account 不存在")
                return BotResponse.markdown_response(
                    "```json\n" + json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n```"
                )

            if action in {"approve", "批准", "通过"}:
                result = service.approve_proposal(
                    int(args[1]), args[2], approved_by=f"{message.platform}:{message.user_id}"
                )
                return BotResponse.text_response(f"✅ Proposal `{result['proposal_id']}` 已批准（仅 Paper Account）")

            if action in {"reject", "拒绝"}:
                result = service.reject_proposal(
                    int(args[1]), args[2], rejected_by=f"{message.platform}:{message.user_id}"
                )
                return BotResponse.text_response(f"✅ Proposal `{result['proposal_id']}` 已拒绝")

            return BotResponse.text_response(f"用法：`{self.usage}`")
        except ValueError:
            return BotResponse.error_response("account_id 必须是整数")
        except Exception as exc:
            logger.warning("Paper command failed: %s", type(exc).__name__)
            return BotResponse.error_response(str(exc)[:160])


__all__ = ["PaperCommand"]
