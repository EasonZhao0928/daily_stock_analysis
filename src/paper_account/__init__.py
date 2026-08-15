# -*- coding: utf-8 -*-
"""Paper Account domain facade.

The package intentionally stops at structured paper proposals and risk
decisions. Virtual orders/fills are a later seam and project through the
existing Account Ledger outbox only.
"""

from .mandate import MandateDecision, PaperMandate, PaperMandateError, evaluate_proposal
from .observation import ObservationError, build_paper_observation
from .controllers import (
    ControllerError,
    HybridController,
    LLMController,
    ShadowController,
    StructuredOutputError,
    StructuredProposalAdapter,
)
from .order_service import ORDER_STATES, ORDER_TRANSITIONS, PaperOrderService
from .service import PaperAccountService, PaperStateError

__all__ = [
    "MandateDecision",
    "ControllerError",
    "HybridController",
    "LLMController",
    "ObservationError",
    "PaperAccountService",
    "PaperMandate",
    "PaperMandateError",
    "PaperStateError",
    "PaperOrderService",
    "ORDER_STATES",
    "ORDER_TRANSITIONS",
    "ShadowController",
    "StructuredOutputError",
    "StructuredProposalAdapter",
    "build_paper_observation",
    "evaluate_proposal",
]
