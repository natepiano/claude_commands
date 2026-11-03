"""Give send.py time for discovery, review, delivery, and result publication."""
from __future__ import annotations

from enum import Enum

from scripts.lib import conf


class DeliveryLimit(Enum):
    CONFIGURED = "configured"


def remote_seconds(delivery: float) -> float:
    """Allow the remote sender to finish each configured stage before SSH stops it."""
    settings = conf.read("send")
    postmaster = conf.read("postmaster")
    return (delivery + settings.number("kill_grace") + settings.number("remote_transport_seconds")
            + postmaster.number("wait_seconds") + postmaster.number("claim_grace_seconds")
            + settings.number("waits_review_seconds") + conf.read("codex_daemon").number("timeout_seconds"))


def seconds(delivery: float | DeliveryLimit = DeliveryLimit.CONFIGURED) -> float:
    """Cover local preparation, remote delivery, and both relay cleanup stages."""
    settings = conf.read("send")
    limit = settings.number("delivery_timeout") if isinstance(delivery, DeliveryLimit) else delivery
    # Both hosts can review an automatic refusal and discover a daemon recipient.
    return (remote_seconds(limit) + settings.number("kill_grace") + settings.number("caller_margin_seconds")
            + settings.number("waits_review_seconds") + conf.read("codex_daemon").number("timeout_seconds"))


def user_seconds(review_seconds: float) -> float:
    """Include the larger delivery limit that send.py gives user messages."""
    return seconds(max(conf.read("send").number("delivery_timeout"), review_seconds))
