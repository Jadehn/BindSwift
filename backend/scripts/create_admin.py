"""Create (or reset) an admin account with a password and authenticator-app 2FA.

    python -m scripts.create_admin you@howardanalytics.net

Scan the QR code with Google Authenticator, 1Password, Authy, or Microsoft Authenticator.
Run it again for the same email to reset the password and 2FA."""
import getpass
import sys

import pyotp

from app import db
from app.security import encrypt, hash_password


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.create_admin EMAIL")
    email = sys.argv[1].strip().lower()
    pw = getpass.getpass("Password (14+ characters): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match.")
    pw_hash = hash_password(pw)
    secret = pyotp.random_base32()
    db.run(
        """INSERT INTO admins (email, password_hash, totp_secret_enc) VALUES (%s,%s,%s)
           ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                 totp_secret_enc = EXCLUDED.totp_secret_enc, totp_last_step = 0, is_active = TRUE""",
        (email, pw_hash, encrypt(secret.encode())),
    )
    db.run("DELETE FROM admin_sessions WHERE admin_id = (SELECT id FROM admins WHERE email = %s)", (email,))
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Bindswift Admin")
    print("\nAdmin saved. Add this to your authenticator app:\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print(f"Or enter this key manually: {secret}\n")
    print("This key is shown once. It is stored encrypted and can't be displayed again.")
    db.close()


if __name__ == "__main__":
    main()
