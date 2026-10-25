"""One-time local OAuth flow for user-owned My Drive uploads."""

import json
import os

from app.journal.config import JournalSettings
from app.journal.google import SCOPES


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = JournalSettings()
    if settings.google_credentials.exists():
        raise SystemExit("Credential file exists. Choose a new path or explicitly back it up first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secret), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    parent = settings.google_credentials.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation; never overwrite an existing user's refresh token.
    descriptor = os.open(settings.google_credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        data = json.loads(credentials.to_json())
        data["type"] = "authorized_user"
        stream.write(json.dumps(data))
    print("Google credentials saved outside the repository. No token is printed.")
    # Catch malformed serialized credentials here without displaying their content.
    json.loads(settings.google_credentials.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
