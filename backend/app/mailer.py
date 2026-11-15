"""Sends you an email for every new submission, over SMTP with STARTTLS."""
import smtplib
import ssl
from email.message import EmailMessage

from .config import get_settings


def _send(msg: EmailMessage) -> None:
    s = get_settings()
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=20) as smtp:
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        if s.is_dev and s.smtp_host in ("localhost", "127.0.0.1"):
            smtp.send_message(msg)
            return
        smtp.starttls(context=ctx)
        smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


def send_code(email: str, code: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Bindswift verification code"
    msg["From"] = s.mail_from
    msg["To"] = email
    msg.set_content(f"Your Bindswift verification code is {code}\n\nIt expires in 10 minutes. "
                    "If you didn't request it, you can ignore this email.")
    _send(msg)


def send_submission_email(sub_id: int, legal_name: str, trade: str, county: str,
                          attachments: list[tuple[str, bytes]]) -> None:
    s = get_settings()
    review_url = f"{s.public_base_url.rstrip('/')}{s.admin_path}/submissions/{sub_id}"
    msg = EmailMessage()
    msg["Subject"] = f"New GL submission #{sub_id}: {legal_name}"
    msg["From"] = s.mail_from
    msg["To"] = s.broker_email
    lines = [
        f"New General Liability submission #{sub_id}",
        "",
        f"Business: {legal_name}",
        f"Trade: {trade}",
        f"County: {county}",
        "",
        f"Review and approve: {review_url}",
        "",
    ]
    if s.email_attach_pdfs and attachments:
        lines.append("Attached: answer summary, ACORD 125 and ACORD 126. The FEIN is masked in these copies;"
                     " the full version is in the admin portal.")
    else:
        lines.append("Documents are in the admin portal.")
    msg.set_content("\n".join(lines))
    if s.email_attach_pdfs:
        for filename, data in attachments:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)

    _send(msg)


def send_offer(email: str, name: str, link: str, carrier: str, premium: float | None, fee: int, expires: str,
               is_total: bool = False) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = "Your general liability quote is ready"
    msg["From"] = s.mail_from
    msg["To"] = email
    price = (f"${premium:,.2f} total due to the insurer" if is_total else f"${premium:,.0f}/year") if premium else "see the link"
    msg.set_content(
        f"Hi {name},\n\nYour quote from {carrier} is ready: {price}, plus the ${fee} Bindswift service fee "
        f"charged once right before binding.\n\nReview it and press Bind when you're ready:\n{link}\n\n"
        f"This link is just for you and works until {expires}.\n\nBindswift")
    _send(msg)


def notify_bind_request(sid: int, legal_name: str, carrier: str, premium: float) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"Bind requested #{sid}: {legal_name}"
    msg["From"] = s.mail_from
    msg["To"] = s.broker_email
    url = f"{s.public_base_url.rstrip('/')}{s.admin_path}/submissions/{sid}#carriers"
    msg.set_content(f"{legal_name} asked to bind the {carrier} quote (${premium:,.0f}).\n\n"
                    f"Next: collect the ${s.fee_amount} fee, record it, then bind:\n{url}")
    _send(msg)


def send_bound(email: str, name: str, carrier: str, policy_number: str | None) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = "You're covered: your general liability policy is bound"
    msg["From"] = s.mail_from
    msg["To"] = email
    msg.set_content(f"Hi {name},\n\nYour general liability policy with {carrier} is bound"
                    + (f" (policy {policy_number})" if policy_number else "") + ". "
                    "Your policy documents come from the insurer. You can now send certificates of insurance "
                    "through Bindswift.\n\nBindswift")
    _send(msg)



def send_cert_portal(email: str, name: str, link: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = "Send certificates of insurance anytime with Bindswift"
    msg["From"] = s.mail_from
    msg["To"] = email
    msg.set_content(f"Hi {name},\n\nYour policy is set up. Use this private link to send a certificate of insurance to "
                    f"a customer, GC or landlord, 24/7:\n\n{link}\n\nBookmark it and don't share it: anyone with the link can "
                    "issue certificates for your business.\n\nBindswift")
    _send(msg)


def send_certificate(to: str, insured: str, holder: str, pdf: bytes, number: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"Certificate of insurance: {insured} for {holder}"
    msg["From"] = s.mail_from
    msg["To"] = to
    msg.set_content(f"Attached is the certificate of insurance for {insured}, issued to {holder} (certificate {number}).\n\n"
                    f"Questions about this certificate: reply to this email.\n\nBindswift · {s.agency_name}")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"COI_{number}.pdf")
    _send(msg)


def notify_cert_request(sid: int, pid: int, insured: str, holder: str, needs: list[str]) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"Certificate needs approval #{sid}: {insured}"
    msg["From"] = s.mail_from
    msg["To"] = s.broker_email
    url = f"{s.public_base_url.rstrip('/')}{s.admin_path}/submissions/{sid}/policies/{pid}"
    msg.set_content(f"{insured} asked for a certificate for {holder} with: {', '.join(needs)}.\n"
                    f"The policy doesn't have that blanket endorsement on file. Get it endorsed, then approve:\n{url}")
    _send(msg)
