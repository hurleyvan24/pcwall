# Display scaling

pcwall is designed to fill one screen without scrolling. Screens differ in physical
resolution, pixel density and viewing distance, so the wall exposes a single scale factor
that resizes the whole layout at once. This guide explains how that factor works, which
presets ship with pcwall, how a display picks its scale on load, and what to do when a
screen looks too big, too small or cut off.

## How scaling works

The whole UI hangs off one CSS custom property, `--ui-scale`, set on the document root.

```css
:root {
  --ui-scale: 1;
  font-size: calc(16px * var(--ui-scale));
}
```

Every layout measurement in `app.css` (padding, gaps, card widths, font sizes, icon
sizes) is written in `rem`, so it is a multiple of the root font size. Changing
`--ui-scale` therefore shrinks or grows the entire wall proportionally: at `0.85` a card
that was 20 rem wide is still 20 rem wide, but a rem is now 13.6 CSS pixels instead of 16.
Nothing reflows unexpectedly because the ratios between elements never change.

Scaling happens inside the page, not in the browser. That matters for kiosk browsers:
keep the browser's own zoom at 100 % and let `--ui-scale` do the work. Stacking browser
zoom on top of `--ui-scale` produces a second, invisible multiplier that is hard to
reason about. See [`scripts/fully_kiosk.md`](../scripts/fully_kiosk.md) for the Android
kiosk settings that follow from this.

## Presets

Presets are named scale values. Pick one from the Settings panel or seed it from the URL
(see [Choosing a scale](#choosing-a-scale)).

| Preset           | Scale | Intended display                                                  |
| ---------------- | ----- | ----------------------------------------------------------------- |
| `standard_1080p` | 1.00  | 1080p desktop monitors and TVs viewed at arm's length to a few metres |
| `tablet_15_6`    | 0.85  | 15.6 inch 1080p Android tablets, whose high-density viewport reports fewer CSS pixels than the panel has |
| `hd_720p`        | 0.75  | 720p compact panels and small displays where every row has to fit |
| `glanceable`     | 1.15  | High-visibility viewing from across the room, fewer kids or routines on screen |
| `auto`           | fit   | Dynamic auto-fit computed from the current viewport, clamped to the range 0.60 to 1.40 |

`auto` measures the viewport on load and on resize and picks the largest scale at which
the configured kids and routines fit without scrolling. The clamp stops a tiny preview
window from producing unreadable text and stops a huge TV from producing comically large
cards. If `auto` lands somewhere you dislike, pick a fixed preset or a custom value
instead; the slider in Settings accepts any value, not only the preset ones.

## Resolution and device pixel ratio

Browsers lay pages out in CSS pixels, not physical pixels. The ratio between the two is
the device pixel ratio, or DPR. A panel's advertised resolution tells you how many
physical pixels it has; the DPR tells you how many of them make up one CSS pixel. The
viewport the wall actually gets is the physical resolution divided by the DPR.

| Physical panel | DPR  | CSS viewport | Suggested preset | Notes                                                             |
| -------------- | ---- | ------------ | ---------------- | ----------------------------------------------------------------- |
| 1920 x 1080    | 1.0  | 1920 x 1080  | `standard_1080p` | Typical desktop monitor or TV in a browser at 100 % zoom          |
| 1920 x 1080    | 1.5  | 1280 x 720   | `tablet_15_6`    | Common on 15.6 inch Android tablets; at scale 0.85 everything fits without scrolling |
| 1920 x 1080    | 2.0  | 960 x 540    | `hd_720p` or `auto` | Phone-class density on a large panel; expect to go below 0.85  |
| 1280 x 720     | 1.0  | 1280 x 720   | `hd_720p`        | Small HDMI panels, older TVs                                      |
| 2560 x 1440    | 1.0  | 2560 x 1440  | `glanceable`     | Plenty of room; larger text reads better from a distance          |
| 3840 x 2160    | 2.0  | 1920 x 1080  | `standard_1080p` | A 4K TV at DPR 2 behaves exactly like a 1080p monitor             |

The second row is the one that surprises people. A 15.6 inch tablet sold as 1080p runs
Android with a DPR of 1.5, so the wall sees a 1280 x 720 viewport and renders the 1080p
layout with one third of it off the bottom. The `tablet_15_6` preset at 0.85 brings the
layout back inside the 720 CSS-pixel height, and because the panel really does have
1080 physical rows the text is still crisp.

To find your DPR, open the browser's developer console on the display and type
`window.devicePixelRatio`. To find the CSS viewport, type `window.innerWidth` and
`window.innerHeight`. In Fully Kiosk Browser the same values are visible on the remote
admin page under device info.

## Choosing a scale

The display resolves its scale from three sources, highest priority first:

1. **Stored setting on this device.** When someone picks a preset or moves the slider in
   Settings, the choice is saved in the browser's local storage and wins on every later
   load. Each device remembers its own value, so a tablet and a desktop pointed at the
   same server can use different scales.
2. **URL query parameter.** If nothing is stored yet, the page reads its own URL:

   | Query                   | Effect                                                   |
   | ----------------------- | -------------------------------------------------------- |
   | `?preset=tablet_15_6`   | Apply the named preset. Any preset id from the table above works, including `auto`. |
   | `?scale=0.85`           | Apply a raw scale factor. Values are clamped to 0.60 to 1.40. |

   Both forms seed the stored setting on first load, so the kiosk's start URL only has to
   carry the parameter once. If both are present, `preset` wins and `scale` is ignored.
   An unknown preset name or a non-numeric scale is ignored and resolution continues to
   the next source.
3. **Server default.** `settings.display.default_preset` in the server's settings
   document names the preset used when neither of the above applies. It ships as
   `standard_1080p`.

The practical consequence: put the right preset in the kiosk start URL once, and from
then on the device keeps that scale even if the URL is later opened without the query.
To force a change on a device that already has a stored value, change it in Settings or
clear the stored value as described below.

## Resetting and troubleshooting

**The layout is cut off at the bottom or scrolls.** The scale is too large for the CSS
viewport. Check `window.innerHeight` and the DPR table above, then pick the next smaller
preset or switch to `auto`. On Android tablets, confirm the kiosk browser's zoom is at
100 % before touching pcwall's scale; browser zoom above 100 % is the usual cause.

**Everything is tiny with empty space around it.** The scale is too small, or the
browser is reporting a larger viewport than expected. Try `glanceable` or move the slider
up. If the display is a 4K TV with DPR 1.0, `auto` will usually land near the top of the
clamp.

**Text is blurry.** Scaling by `--ui-scale` never causes blur because it changes layout
size rather than rasterising a bitmap. Blur comes from browser zoom, from the operating
system's display scaling, or from a panel running at a non-native resolution. Set all
three to 100 % or native and use pcwall's scale alone.

**A new URL query has no effect.** A stored setting exists and is taking precedence.
Open Settings on that device and pick the preset there, or clear the stored value.

**Clearing the stored value.** Any of the following works:

- Open Settings on the display and choose a preset; the stored value is replaced. Choosing
  the same preset the server default names is the simplest way to get back to baseline.
- Reload the page with an explicit `?preset=` or `?scale=` query after clearing storage,
  which re-seeds the stored value from the URL.
- In the browser's developer console run `localStorage.clear()` and reload. This also
  clears any other per-device state the wall keeps, so prefer the Settings controls.
- In Fully Kiosk Browser, use the clear cache and web storage action from the remote
  admin page, then reload the start URL.

**The scale reverts after a refresh.** The browser could not persist the choice, usually
because storage is full or the kiosk browser was configured to wipe web storage on
reload. The current session keeps the value you chose, but the next load falls back to
the URL query. Free up storage or disable the wipe, then select the preset again.

**Different kids see different layouts.** Scale is per device, not per kid. If two
tablets look different, compare their stored settings in Settings on each device.

## Quick reference

```text
Precedence : stored setting  >  ?preset= / ?scale=  >  settings.display.default_preset
Clamp      : 0.60 .. 1.40 (applies to ?scale=, the slider and auto)
Formula    : CSS viewport = physical pixels / devicePixelRatio
Rule       : browser zoom 100 %, OS scaling 100 %, panel at native resolution
```
