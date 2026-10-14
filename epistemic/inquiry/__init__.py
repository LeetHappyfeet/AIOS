"""Read-only, character-scoped inquiry. No source or knowledge admission authority."""
from .contracts import InquiryDemand, InquiryEvidence, InquiryHit
from .lookup import InquiryLookup
from .trigger import demand_from_v11_rejection, should_escalate

__all__ = ["InquiryDemand", "InquiryEvidence", "InquiryHit",
           "InquiryLookup", "demand_from_v11_rejection", "should_escalate"]
