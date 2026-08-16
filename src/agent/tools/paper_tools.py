# -*- coding: utf-8 -*-
"""Paper Account proposal tools.

The handlers deliberately expose only the Paper proposal boundary.  There is
no broker, cash, position, fill, shell, filesystem, or database-write tool in
this module; persistence is confined to the PaperAccountService facade.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy


def get_paper_account_service():
    from src.paper_account.service import PaperAccountService

    return PaperAccountService()


_PAPER_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read"],
    permissions=["paper:read"],
    cancellation_safe=True,
    scope_dimensions=["account"],
    allowed_profiles=["paper_proposal"],
)
_PAPER_PROPOSAL_POLICY = ToolPolicy.declared(
    read_only=False,
    side_effects=["paper_proposal"],
    permissions=["paper:proposal"],
    scope_dimensions=["account"],
    allowed_profiles=["paper_proposal"],
)
_PAPER_SUBMIT_POLICY = ToolPolicy.declared(
    read_only=False,
    side_effects=["paper_proposal"],
    permissions=["paper:proposal"],
    scope_dimensions=["account", "symbol"],
    allowed_profiles=["paper_proposal"],
)
_PAPER_PROPOSAL_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read"],
    permissions=["paper:proposal:read"],
    cancellation_safe=True,
    scope_dimensions=["account"],
    allowed_profiles=["paper_proposal", "paper_approval"],
)
_PAPER_APPROVAL_POLICY = ToolPolicy.declared(
    read_only=False,
    side_effects=["paper_approval"],
    permissions=["paper:approval"],
    scope_dimensions=["account"],
    allowed_profiles=["paper_approval"],
)


def _handle_read_paper_context(account_id: int, observation_id: Optional[str] = None) -> Dict[str, Any]:
    service = get_paper_account_service()
    state = service.inspect(int(account_id))
    if state is None:
        raise ValueError("paper account not found")
    payload: Dict[str, Any] = {
        "account": state["account"],
        "config": state["config"],
        "frozen": False,
    }
    if observation_id:
        observation = service.get_observation(observation_id)
        if observation is None or int(observation["account_id"]) != int(account_id):
            raise ValueError("frozen paper observation not found")
        payload["observation"] = observation["payload"]
        payload["observation_id"] = observation["observation_id"]
        payload["frozen"] = observation["status"] == "frozen"
    return payload


read_paper_context_tool = ToolDefinition(
    name="read_paper_context",
    description="Read one Paper Account and optional frozen observation; no live broker state is exposed.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("observation_id", "string", "Optional immutable observation id.", required=False),
    ],
    handler=_handle_read_paper_context,
    category="paper",
    policy=_PAPER_READ_POLICY,
)


def _handle_list_paper_proposals(account_id: int, limit: int = 100) -> Dict[str, Any]:
    service = get_paper_account_service()
    if service.inspect(int(account_id)) is None:
        raise ValueError("paper account not found")
    return {"items": list(service.list_proposals(int(account_id), limit=limit))}


list_paper_proposals_tool = ToolDefinition(
    name="list_paper_proposals",
    description="List structured Paper Proposals for an account.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("limit", "integer", "Maximum number of proposals.", required=False, default=100),
    ],
    handler=_handle_list_paper_proposals,
    category="paper",
    policy=_PAPER_PROPOSAL_READ_POLICY,
)


def _handle_get_paper_proposal(account_id: int, proposal_id: str) -> Dict[str, Any]:
    proposal = get_paper_account_service().get_proposal(int(account_id), proposal_id)
    if proposal is None:
        raise ValueError("paper proposal not found")
    return {"proposal": proposal}


get_paper_proposal_tool = ToolDefinition(
    name="get_paper_proposal",
    description="Read one structured Paper Proposal by id.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("proposal_id", "string", "Paper Proposal id.", required=True),
    ],
    handler=_handle_get_paper_proposal,
    category="paper",
    policy=_PAPER_PROPOSAL_READ_POLICY,
)


def _handle_submit_paper_proposal(
    account_id: int,
    run_id: str,
    observation_id: str,
    symbol: str,
    market: str,
    side: str,
    order_type: str,
    rationale: str,
    quantity: Optional[float] = None,
    target_weight: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    evidence_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    proposal: Dict[str, Any] = {
        "symbol": symbol,
        "market": market,
        "side": side,
        "order_type": order_type,
        "rationale": rationale,
        "evidence_refs": evidence_refs or [],
    }
    for name, value in (
        ("quantity", quantity),
        ("target_weight", target_weight),
        ("limit_price", limit_price),
        ("stop_price", stop_price),
    ):
        if value is not None:
            proposal[name] = value
    return get_paper_account_service().submit_proposal(
        int(account_id),
        run_id=run_id,
        observation_id=observation_id,
        proposal=proposal,
    )


submit_paper_proposal_tool = ToolDefinition(
    name="submit_paper_proposal",
    description="Submit exactly one structured Paper Proposal; it cannot create a broker order or fill.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("run_id", "string", "Decision run id.", required=True),
        ToolParameter("observation_id", "string", "Frozen observation id.", required=True),
        ToolParameter("symbol", "string", "Security symbol.", required=True),
        ToolParameter("market", "string", "Market code.", required=True, enum=["cn", "hk", "us", "jp", "kr", "tw"]),
        ToolParameter("side", "string", "Proposal side.", required=True, enum=["buy", "sell", "hold", "reduce"]),
        ToolParameter("order_type", "string", "Virtual order intent only.", required=True, enum=["market", "limit", "stop"]),
        ToolParameter("quantity", "number", "Absolute quantity; mutually exclusive with target_weight.", required=False),
        ToolParameter("target_weight", "number", "Target portfolio weight; mutually exclusive with quantity.", required=False),
        ToolParameter("limit_price", "number", "Limit price when order_type=limit.", required=False),
        ToolParameter("stop_price", "number", "Stop price when order_type=stop.", required=False),
        ToolParameter("rationale", "string", "Structured explanation grounded in evidence.", required=True),
        ToolParameter("evidence_refs", "array", "Evidence reference objects.", required=False, default=[]),
    ],
    handler=_handle_submit_paper_proposal,
    category="paper",
    policy=_PAPER_SUBMIT_POLICY,
)


def _handle_cancel_paper_proposal(
    account_id: int,
    proposal_id: str,
    cancelled_by: str = "local_user",
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    return get_paper_account_service().cancel_proposal(
        int(account_id), proposal_id, cancelled_by=cancelled_by, expected_version=expected_version
    )


cancel_paper_proposal_tool = ToolDefinition(
    name="cancel_paper_proposal",
    description="Cancel a recommendation or pending Paper Proposal; does not touch orders or cash.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("proposal_id", "string", "Paper Proposal id.", required=True),
        ToolParameter("cancelled_by", "string", "Local operator identity.", required=False, default="local_user"),
        ToolParameter("expected_version", "integer", "Optional Paper Account config version.", required=False),
    ],
    handler=_handle_cancel_paper_proposal,
    category="paper",
    policy=_PAPER_PROPOSAL_POLICY,
)


def _handle_approve_paper_proposal(
    account_id: int,
    proposal_id: str,
    approved_by: str,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    return get_paper_account_service().approve_proposal(
        int(account_id), proposal_id, approved_by=approved_by, expected_version=expected_version
    )


approve_paper_proposal_tool = ToolDefinition(
    name="approve_paper_proposal",
    description="Approve one pending virtual Paper Proposal after an explicit human identity check.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("proposal_id", "string", "Paper Proposal id.", required=True),
        ToolParameter("approved_by", "string", "Approver identity.", required=True),
        ToolParameter("expected_version", "integer", "Optional Paper Account config version.", required=False),
    ],
    handler=_handle_approve_paper_proposal,
    category="paper",
    policy=_PAPER_APPROVAL_POLICY,
)


def _handle_reject_paper_proposal(
    account_id: int,
    proposal_id: str,
    rejected_by: str,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    return get_paper_account_service().reject_proposal(
        int(account_id), proposal_id, rejected_by=rejected_by, expected_version=expected_version
    )


reject_paper_proposal_tool = ToolDefinition(
    name="reject_paper_proposal",
    description="Reject one recommendation or pending virtual Paper Proposal.",
    parameters=[
        ToolParameter("account_id", "integer", "Paper Account id.", required=True),
        ToolParameter("proposal_id", "string", "Paper Proposal id.", required=True),
        ToolParameter("rejected_by", "string", "Rejector identity.", required=True),
        ToolParameter("expected_version", "integer", "Optional Paper Account config version.", required=False),
    ],
    handler=_handle_reject_paper_proposal,
    category="paper",
    policy=_PAPER_APPROVAL_POLICY,
)


ALL_PAPER_TOOLS = [
    read_paper_context_tool,
    list_paper_proposals_tool,
    get_paper_proposal_tool,
    submit_paper_proposal_tool,
    cancel_paper_proposal_tool,
    approve_paper_proposal_tool,
    reject_paper_proposal_tool,
]


__all__ = [
    "ALL_PAPER_TOOLS",
    "approve_paper_proposal_tool",
    "cancel_paper_proposal_tool",
    "get_paper_account_service",
    "get_paper_proposal_tool",
    "list_paper_proposals_tool",
    "read_paper_context_tool",
    "reject_paper_proposal_tool",
    "submit_paper_proposal_tool",
]
