# Fully Kiosk Browser setup for pcwall

This guide turns an Android tablet into a dedicated pcwall display using
[Fully Kiosk Browser](https://www.fully-kiosk.com/). It was written against a 15.6 inch
1080p Android 14 tablet of the kind sold as a wall or counter display (RK3576-class
boards are common), but every step applies to any Android 7 or newer device with a
browser-grade WebView. Scale decisions are explained in
[`docs/display-scaling.md`](../docs/display-scaling.md); this page covers the kiosk side.

Settings names below follow the Fully Kiosk Browser menu. Menu wording shifts slightly
between versions, so if a name is not an exact match look for the nearest option in the
same section.

## Before you start

- pcwall is running and reachable on the local network. Note the server's LAN IP or
  host name and port (the default port is `8080`).
- The tablet is on the same network as the server, or can reach the host domain you use
  for pcwall.
- The tablet is plugged in. A wall display should never run from battery; the settings
  below assume mains power.
- You have a physical keyboard or a second device for typing long URLs if the tablet's
  on-screen keyboard is awkward. Fully's remote admin, enabled in step 7, lets you type
  from a laptop.

## 1. Prepare the tablet

1. Finish Android's own setup wizard, join Wi-Fi, and install any pending system updates.
   Kiosk mode fights with update prompts, so get them out of the way first.
2. Set the display to never sleep while charging: **Settings > Developer options > Stay
   awake**. If Developer options is hidden, tap **Settings > About tablet > Build number**
   seven times. Fully also keeps the screen on, but this Android setting is the fallback
   if the app is ever killed.
3. Turn off automatic brightness unless the tablet sits in a room with strong daylight
   changes. A fixed brightness avoids flicker when people walk past the light sensor.
4. Disable the lock screen: **Settings > Security > Screen lock > None**. If the device
   is managed and a lock is required, Fully can unlock a swipe lock but not a PIN.
5. Make sure the tablet's date, time and time zone are correct. pcwall times routines by
   the server's household time zone, but the browser clock drives auto-reload and the
   screen schedule configured below.

## 2. Install Fully Kiosk Browser

1. Install **Fully Kiosk Browser** from the Play Store, or sideload the APK from the
   vendor's site if the tablet has no Play Store. The free version is enough to test; the
   PLUS licence unlocks kiosk lock mode, the remote admin and the screensaver scheduler,
   all of which this guide uses.
2. Open the app once and grant the permissions it asks for. Grant **Display over other
   apps** and **Device admin** when prompted; both are needed for lock mode.
3. Open the Fully menu by swiping in from the left edge (or tapping the screen five times
   if the swipe gesture is disabled) and enter **Settings**.

## 3. Start URL

**Settings > Web Content Settings > Start URL**

Point the tablet at the server and seed the display preset in the same URL:

```text
http://<lan-ip>:8080/?preset=tablet_15_6
```

Replace `<lan-ip>` with the server's address. If pcwall is served under a domain name or
behind a reverse proxy, use that instead:

```text
https://<your-host>/?preset=tablet_15_6
```

Notes on the query string:

- `?preset=tablet_15_6` is right for 15.6 inch 1080p tablets, whose Android device pixel
  ratio of 1.5 gives the browser a 1280 x 720 viewport. Other presets are listed in
  `docs/display-scaling.md`; `?preset=auto` is a safe choice for an unknown panel.
- `?scale=0.85` is the raw alternative when no preset matches. The value is clamped to
  the range 0.60 to 1.40.
- The query is read only when the device has no stored scale yet. Once pcwall has saved
  the preset in the browser's local storage, the stored value wins and the URL can be
  plain. Changing the URL later does not override a stored value; change the scale from
  pcwall's Settings panel instead, or clear web storage (step 9).
- Do not add a trailing path. pcwall serves the wall at the root.

Tap the back arrow to leave Settings; Fully loads the start URL immediately. If the page
does not load, check the LAN IP from the server machine and confirm the tablet can open
the same URL in Chrome.

## 4. Web view and zoom

**Settings > Web Content Settings**

| Setting                      | Value   | Why                                                                 |
| ---------------------------- | ------- | ------------------------------------------------------------------- |
| Web View Zoom / Initial Scale | 100     | pcwall scales itself with `--ui-scale`. Browser zoom stacks a second multiplier on top and breaks the fit. |
| Enable Zoom                  | Off     | Prevents accidental pinch zoom by small hands.                      |
| Use Wide Viewport            | On      | Lets the page use the device viewport width as reported.           |
| Load in Overview Mode        | Off     | Overview mode shrinks the page to fit width, which fights `--ui-scale`. |
| Desktop Mode                 | Off     | Desktop mode fakes a larger viewport and a desktop user agent; pcwall does not need it. |
| Enable Pull to Refresh       | Off     | A downward swipe on a routine card should not reload the page.      |
| Enable JavaScript            | On      | Required.                                                           |
| Enable Local Storage / Web Storage | On | pcwall stores the chosen scale and other per-device state here.  |
| Autoplay Videos / Media      | On      | Lets completion sounds play without a tap.                          |
| Hardware Acceleration        | On      | Smoother transitions on the RK3576-class GPU. Found under Advanced Web Settings in some versions. |

## 5. Screen, sleep and night schedule

**Settings > Device Management** and **Settings > Power Settings**

| Setting                                  | Value                 | Why                                                             |
| ---------------------------------------- | --------------------- | --------------------------------------------------------------- |
| Keep Screen On                           | On                    | Overrides the Android screen timeout while Fully is in front.   |
| Keep Screen On while plugged in only     | On                    | Lets the panel sleep if someone unplugs it to move it.          |
| Prevent from Sleep while Screen Off      | On                    | Keeps the app and its reload timers alive when the screen is dark. |
| Screen Brightness                        | Fixed, 60 to 80 %     | Match the room; avoid 100 % on a panel that runs all day.       |
| Screen Off Timer                         | 0 (disabled)          | pcwall is meant to be glanceable at all times during the day.   |
| Screensaver                              | Off, or Black Screen  | pcwall has its own night mode; a black screensaver is the only one that does not fight it. |
| Scheduled Screen Off                     | Household bedtime     | For example `21:00` on weekdays, later on weekends.             |
| Scheduled Screen On                      | Morning routine start | For example `06:30`. The wall is awake before the first routine starts. |
| Motion Detection / Wake on Motion        | Optional              | If enabled, pair it with a short screen-off delay so late-night bathroom trips wake the screen. |

Fully's scheduler takes clock times and weekday masks, so you can give school nights an
earlier off time than weekend nights. Times here are the tablet's local time, so step 1.5
matters. Pick a screen-on time a few minutes before the earliest routine start in your
family's configuration so the first kid up does not see a dark panel.

If the tablet is powered from a smart plug, do not schedule power cuts; the tablet will
boot into Android's launcher rather than Fully unless step 8 is complete.

## 6. Auto-reload

**Settings > Web Auto Reload**

pcwall refreshes its own state from the server, but a daily reload clears any browser
memory growth and picks up server-side code changes after an update.

| Setting                          | Value          | Why                                                             |
| -------------------------------- | -------------- | --------------------------------------------------------------- |
| Auto Reload on Idle              | 600 seconds    | Reload after ten minutes without touch; nobody notices and the client resyncs. |
| Auto Reload on Schedule          | Daily, e.g. `04:00` | Guarantees a fresh page every day even if the wall is touched constantly. |
| Reload on Screen On              | On             | Starts each morning from a clean page.                          |
| Reload on Network Reconnect      | On             | Recovers from Wi-Fi drops without a visit.                      |
| Reload Page on Error             | On, 10 seconds | Retries if the server was briefly restarting.                   |
| Clear Cache on Reload            | Off            | Fonts and scripts can stay cached.                              |
| Clear Web Storage on Reload      | Off            | **Important.** Clearing storage wipes the stored scale and every other per-device choice on each reload. |

Reloads land on the start URL, so a device that has not yet stored a scale will re-read
`?preset=` each time; a device that has stored one keeps it.

## 7. Remote administration

**Settings > Remote Administration (PLUS)**

1. Turn on **Enable Remote Administration**.
2. Set a strong **Remote Admin Password**. It is sent in the clear over HTTP, so treat it
   as a LAN-only convenience and never expose the admin port through the router.
3. Note the port (default `2323`). From a laptop on the same network, open
   `http://<tablet-ip>:2323` and sign in.

The remote admin page lets you change the start URL, trigger a reload, take a screenshot
to check the layout, view device info including the reported screen size and pixel
ratio, and clear cache or web storage without touching the tablet. It is the easiest way
to verify a new preset: change the URL, clear web storage, reload, screenshot.

## 8. Kiosk lock mode

**Settings > Kiosk Mode (PLUS)**

Lock mode keeps kids inside pcwall. Nothing short of the kiosk PIN leaves the app.

1. Set a **Kiosk Exit PIN**. Do not reuse the pcwall parent PIN from `config/family.json`;
   the kiosk PIN guards the device, the parent PIN guards star balances.
2. Turn on **Enable Kiosk Mode**. Fully asks to become the default launcher; accept.
3. Turn on **Disable Status Bar**, **Disable Home Button**, **Disable Power Button** (if
   the device supports it) and **Disable Volume Buttons** if the panel has no speaker.
   Leave volume enabled if completion sounds are wanted.
4. Turn on **Single App Mode** (sometimes called Lock Task Mode) if the device is a
   device owner or a fresh factory reset was provisioned with Fully. This is the strongest
   lock. On a consumer tablet without device-owner rights, Fully's own kiosk mode is
   still enough for a family wall.
5. Under **Settings > Advanced Web Settings**, turn on **Fullscreen Mode** and **Immersive
   Mode**. Immersive hides the navigation bar; fullscreen hides the status bar inside the
   WebView. Set **Show Navigation Bar** to Off.
6. Turn on **Launch on Boot** and **Launch on Unlock** under **Device Management** so a
   reboot comes back to the wall without help.
7. Reboot the tablet and confirm it boots straight into pcwall at the right scale.

To exit later, tap the screen seven times quickly (or swipe from the left edge if you
left the gesture on) and enter the kiosk PIN. The pcwall parent view is still reachable
inside the page with the parent PIN; lock mode does not interfere with it.

## 9. Verify the layout

With everything set, confirm the scale is right before mounting the tablet:

1. Reload the page from the remote admin.
2. Take a screenshot from the remote admin and check that all configured kids and the
   current routine's steps are visible without a scrollbar.
3. If anything is cut off, open pcwall's Settings on the tablet and move the scale down
   one preset, or switch to `auto`. If text is small with empty space below, move it up.
4. If you want to retest the URL seeding itself, clear web storage from the remote admin
   and reload; the device then re-reads `?preset=` from the start URL.

Common fixes:

| Symptom                                    | Fix                                                                      |
| ------------------------------------------ | ------------------------------------------------------------------------ |
| Page is too large and scrolls              | Web View Zoom is above 100, or the wrong preset is stored. Reset zoom, then pick `tablet_15_6` or `auto` in Settings. |
| Page looks like a phone layout             | Desktop Mode is on, or Use Wide Viewport is off.                         |
| Scale resets to default every reload       | Clear Web Storage on Reload is on. Turn it off.                          |
| Screen goes dark mid-day                   | Screen Off Timer is set, or Keep Screen On is limited to plugged-in and the charger is loose. |
| Wall does not wake in the morning          | Scheduled Screen On is unset, or the tablet's time zone is wrong.        |
| Tablet boots to the Android launcher       | Launch on Boot is off, or Fully is not the default launcher.             |
| Kids leave the app                         | Kiosk Mode is off or the exit gesture is still enabled.                  |

## Recommended settings at a glance

```text
Start URL                 http://<lan-ip>:8080/?preset=tablet_15_6
Web View Zoom             100      Enable Zoom           Off
Desktop Mode              Off      Pull to Refresh       Off
Hardware Acceleration     On       Web Storage           On
Keep Screen On            On       Screen Off Timer      0
Scheduled Screen Off      bedtime  Scheduled Screen On   first routine start
Auto Reload on Idle       600 s    Scheduled Reload      daily, early morning
Clear Web Storage         Off      Reload on Screen On   On
Fullscreen + Immersive    On       Kiosk Mode            On, with exit PIN
Launch on Boot            On       Remote Admin          On, LAN only, strong password
```

Fully can export this configuration as a JSON file from **Settings > Other Settings >
Export Settings**. Keep that file alongside your server backup so a replacement tablet
can be set up with one import.
