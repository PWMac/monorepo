# Oracle Edge brand pack

**Screen ◆ Rank ◆ Signal · Discover value**

The mark is a jeweler's loupe over a rough stone. Through the lens, the stone is a cut diamond: the market sees rock, Oracle Edge sees the gem.

## Quick install (Flask/static site)

1. Copy this folder to `static/brand/`.
2. Paste `theme/head-snippet.html` into your page `<head>`.
3. Replace the header with the markup at the bottom of that snippet, and add the class `oe-topbar` to it.

## What's inside

| Folder | Files | Use |
|---|---|---|
| `logo/` | `oracle-edge-mark.svg` | Full mark (vector). 48px and up. |
| | `oracle-edge-mark-small.svg` | Simplified mark (lens and diamond only). Below 48px. |
| | `oracle-edge-header-tagline.png` (+@2x) | **Default header logo**, for dark green. 390×66. |
| | `oracle-edge-header.png` (+@2x) | Header without the tagline. |
| | `…-light.png` (+@2x) | The same two, for light backgrounds. |
| `emblem/` | `oracle-edge-emblem-768.png` (the full-size 1536×1024 original stays in the brand pack) | Full crest: Today hero, Guide/About page, splash. |
| `social/` | `og-image.png` (1200×630) | Link previews. |
| `favicon/` | `.ico`, `.svg`, 16/32 PNG, `apple-touch-icon`, 192/512, maskable 512, `site.webmanifest` | Browser and app icons. |
| `theme/` | `oracle-edge-theme.css`, `tokens.json`, `head-snippet.html` | Colors, type, components, dark mode. |
| `fonts/` | `Cinzel.ttf` + `OFL.txt` | Display font (SIL Open Font License). |

## Rules

- **Header:** use the horizontal logo, never the emblem. Use the deep green bar (`#0D341A`) with a 2px gold rule under it.
- **Size:** use the full mark at 48px and up. Below 48px, switch to the small mark.
- **Clear space:** leave at least the lens's ring thickness × 2 around the mark.
- **The diamond (◆):** it's the brand's punctuation. Use it for separators, bullets and section markers. Don't use dots.
- **Cherry red (`#C8102E`):** it appears exactly once, as the single red glint on the diamond. Never use it in UI, charts or text. For losses, use `--oe-negative` (`#A8322D`).
- **Gold text on light backgrounds:** only at large sizes (18px and up), because its contrast is about 3.75:1. Use green for small text.
- **Cinzel:** reserve it for brand moments: the logo, tagline, "Discover value" and hero titles. The UI stays in Inter or Segoe UI.

## Two easter eggs (please don't explain them in the UI)

- **The diamond:** Berkshire owns Borsheims, the Omaha jeweler, and Helzberg Diamonds.
- **The cherry-red glint:** The Oracle's Cherry Coke.
