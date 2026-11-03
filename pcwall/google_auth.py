"""Local, optional Google OAuth for Calendar and Photos Picker.

Importing this module does not load credentials or contact Google. Only the CLI
(or an explicit ``interactive=True`` call) may start a browser sign-in.
"""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
)
logger = logging.getLogger(__name__)


def _configured_path(variable: str, default: str) -> Path:
    path = Path(os.environ.get(variable, default)).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def credentials_path() -> Path:
    """Resolve the OAuth client file relative to the repository, never the cwd."""
    return _configured_path("GOOGLE_CREDENTIALS_FILE", "credentials.json")


def token_path() -> Path:
    """Resolve this device's token file relative to the repository."""
    return _configured_path("GOOGLE_TOKEN_FILE", "data/google_token.json")


def _save_token(credentials, path: Path) -> None:
    """Replace tokens atomically so interrupted writes keep the previous token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def get_credentials(scopes: tuple[str, ...] = SCOPES, interactive: bool = False):
    """Load or refresh local credentials, returning None when unavailable.

    Missing SDK packages, malformed/revoked tokens, declined consent and file
    errors leave Google disconnected. Background sync never opens a browser.
    """
    try:
        token = token_path()
        credentials = None
        if token.is_file():
            try:
                from google.oauth2.credentials import Credentials

                credentials = Credentials.from_authorized_user_file(str(token), list(scopes))
                if not credentials.valid and credentials.expired and credentials.refresh_token:
                    from google.auth.transport.requests import Request

                    credentials.refresh(Request())
                    if credentials.valid:
                        _save_token(credentials, token)
            except Exception as exc:
                # SDK error text can contain tokens or responses; log only its type.
                logger.info("Google token could not be used (%s).", type(exc).__name__)
                credentials = None
        if credentials is not None and credentials.valid:
            return credentials
        if not interactive:
            return None

        client = credentials_path()
        if not client.is_file():
            logger.info("Google client file is missing: %s", client)
            return None
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials = InstalledAppFlow.from_client_secrets_file(str(client), list(scopes)).run_local_server(port=0)
        if credentials is None or not credentials.valid:
            logger.warning("Google sign-in did not return valid credentials.")
            return None
        _save_token(credentials, token)
        return credentials
    except Exception as exc:
        logger.warning("Google authorization unavailable (%s). See docs/google-setup.md.", type(exc).__name__)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect this wall to Google Calendar and Photos Picker.")
    parser.parse_args(argv)
    try:
        client = credentials_path()
        if not client.is_file():
            print(f"Google client file not found: {client}. Follow docs/google-setup.md first.", file=sys.stderr)
            return 1
        if get_credentials(interactive=True) is None:
            print(f"Google sign-in failed. Check {client}, the Google SDK installation, and docs/google-setup.md.",
                  file=sys.stderr)
            return 1
        print(f"Google connected. Token saved at {token_path()}")
        return 0
    except OSError as exc:
        print(f"Cannot access Google authorization files ({type(exc).__name__}). See docs/google-setup.md.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
