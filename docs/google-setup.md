# Connect Google Calendar and Photos

Google is optional: the wall works without signing in. Connecting grants read-only
Calendar access and permission to pick photos. Photos Picker shares only the items
you select; it does not grant the wall access to your entire photo library.
See Google's [Picker authorization scopes](https://developers.google.com/photos/overview/authorization).

Install the Google packages in the Python environment you use for the wall:

```sh
python -m pip install google-api-python-client google-auth-oauthlib
```

## Path A: 3-minute Google Cloud setup (self-hosted)

Allow roughly three minutes for the console steps, plus your first sign-in.

1. Open the [Google Cloud Console](https://console.cloud.google.com/), sign in,
   and create or select a project for your wall.
2. In **APIs & Services → Library**, enable **Google Calendar API** and
   **Photos Picker API**. Google documents the Photos project setup
   [here](https://developers.google.com/photos/overview/configure-your-app).
3. Open **Google Auth Platform** (or **APIs & Services → OAuth consent screen**).
   Set up the app name and contact email. Choose an **External** audience and
   leave the publishing status at **Testing**. Under **Audience → Test users**,
   add the personal Gmail address that will own the wall's calendars and photos.
4. In **Data Access**, add these two scopes:
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`
5. In **Clients → Create client**, choose **Desktop app**. Download the OAuth
   client JSON and name it `credentials.json` in the repository root. This must
   be an OAuth client download, not a service-account key. Google's
   [desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)
   describes this client type and the browser consent flow.
6. From the repository root, run:

   ```sh
   python -m pcwall.google_auth
   ```

   A browser opens. Sign in as your Test User and grant both permissions. Run
   this on the computer hosting the wall, with a browser that can reach its
   localhost callback. The command prints success and the local token location.
7. Start or restart your wall. In the wall's calendar settings, choose the
   calendars to display. Use the photo picker to select photos for the wall;
   downloaded copies remain available for offline playback.

## Path B: friends' zero-setup path

Friends can skip creating a Google Cloud project:

1. The repository owner adds the friend's Google email under their project's
   **Audience → Test users**, while the OAuth app is External/Testing.
2. The owner sends only the Desktop OAuth client's `credentials.json` directly
   to the friend through a private, out-of-band channel. Keep it out of the
   public repository. Never send the owner's `google_token.json`.
3. The friend puts `credentials.json` in their own repository root, installs the
   Google packages above, and runs `python -m pcwall.google_auth` on their own
   wall computer. They sign in with **their own** account and grant access.
4. The friend chooses their calendars and photos on their own wall.

The client JSON identifies the shared Google Cloud app; it does not grant access
to the owner's Google account. Each person's access and refresh tokens stay in
their local token file. This wall does not upload those tokens to the owner.
The owner controls the shared OAuth app and its quota, so share with people you
trust. Calendar data and chosen photo copies are stored locally on each wall.

## File locations and optional school notices

Paths are resolved relative to the repository root, even if you launch Python
from another working directory; absolute paths also work:

| Setting | Default |
| --- | --- |
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` |
| `GOOGLE_TOKEN_FILE` | `data/google_token.json` |

Set environment variables in the shell before signing in or starting the wall.
For example, in PowerShell:

```powershell
$env:GOOGLE_CREDENTIALS_FILE = 'C:\wall-private\credentials.json'
$env:GOOGLE_TOKEN_FILE = 'C:\wall-private\google_token.json'
python -m pcwall.google_auth
```

The background connector never opens a sign-in browser. It reuses a valid token
or refreshes it and saves the replacement locally. If no usable token exists,
calendar refresh keeps the last cached events and logs that Google is disconnected.
Keep token files private and out of source control; anyone holding a refresh
token can use its granted permissions.

The optional dress-day mail scanner uses the generic Gmail query
`subject:dress OR subject:spirit`. Set `PCWALL_DRESS_QUERY` to a Gmail search such
as `from:school.example subject:theme` to match your household's notices. The
scanner also needs the Gmail API enabled and a token with Gmail read-only
authorization; the default sign-in above requests only Calendar and Photos.
Changing a search query does not grant Gmail access.

## Troubleshooting

- **Refresh stops after seven days / `invalid_grant`:** External apps in Testing
  receive refresh tokens that expire after seven days for these scopes. Run
  `python -m pcwall.google_auth` again to sign in. For ongoing use, the project
  owner can move the External app to **In production** under **Audience** and
  sign in again to obtain a new token. Production removes the Testing-specific
  seven-day limit, but tokens can still be revoked or expire for other reasons;
  publishing alone does not complete verification or remove unverified-app
  warnings and user limits. See Google's
  [refresh-token rules](https://developers.google.com/identity/protocols/oauth2/)
  and [app publishing states](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview).
- **Access blocked / user not authorized:** Check the signed-in email is listed
  as a Test User on the project that issued this `credentials.json`. Managed
  school or work accounts may also require their administrator's permission.
- **Missing client file or wrong client type:** Check the printed path and
  download a **Desktop app** OAuth client again. Relative environment overrides
  are rooted at the repository, not the current shell directory.
- **SDK missing, consent declined, or file not writable:** Install the packages
  above, confirm the token directory is writable, and run the sign-in command
  again. It exits with status 1 when authentication cannot be completed and
  status 0 when connected; it never prints the token contents.
- **Malformed or revoked token:** Move aside your local token file and sign in
  again. Background refresh failures leave cached calendar events in place.
