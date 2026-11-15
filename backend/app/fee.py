"""Broker service fee agreement (N.Y. Insurance Law §2119 and 11 NYCRR 20.6).

The fee may only be charged under a written memorandum, signed by the insured, that states the
amount. A copy must be kept at least 3 years after the services are performed. The server
keeps its own copy of the text (never trusting the browser's) and stores a signed PDF of it.
"""
from datetime import date

FEE_AMOUNT = 50
FEE_VERSION = "FA-2026-11"


def agreement_text(legal_name: str, signed_on: date) -> str:
    return (
        f"Between {legal_name} (\"you\") and Howard Analytics, a New York licensed property & casualty "
        "insurance broker (License #1931665), doing business as Bindswift.\n"
        "Services: preparing and submitting your insurance applications, shopping insurance carriers, "
        "placing and binding coverage you approve, and issuing certificates of insurance through Bindswift "
        "during the policy term.\n"
        f"Fee: ${FEE_AMOUNT}.00, flat and one time, for each policy bound.\n"
        "When it's charged: right before your policy is bound, after you approve a quote. If no policy is "
        "bound, you are not charged. If the insurer does not issue the policy, the fee is refunded.\n"
        "This fee is separate from, and in addition to, your insurance premium and any insurer policy fees, "
        "taxes or surcharges. Howard Analytics also receives a commission from the insurer.\n"
        f"Date: {signed_on.strftime('%m/%d/%Y')} · Agreement {FEE_VERSION}"
    )
