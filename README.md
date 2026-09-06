## Home Assistant Discogs Enhanced Integration
![Discogs Enhanced](discogs.png)

[![hacs_badge](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=andreasc1&repository=homeassistant-discogs-enhanced&category=integration)

This custom integration for Home Assistant provides enhanced monitoring of your Discogs collection, building upon the official Discogs integration. Track your collection size, wantlist, format counts, and the estimated **minimum, median, and maximum market value** of your vinyl and CD collection.

### Why this Integration?

The official Home Assistant Discogs integration covers collection and wantlist counts and random record suggestions — but offers no insight into monetary value or format breakdowns. This integration adds exactly that:

* **Collection Value (Minimum/Median/Maximum):** Estimated market value sensors with dynamic currency detection based on your Discogs account settings.
* **Format Counts:** Separate sensors for the number of Vinyl records and CDs in your collection.

This project is heavily based on and inspired by the [official Home Assistant Discogs integration](https://www.home-assistant.io/integrations/discogs) originally developed by **[@thibmaek](https://github.com/thibmaek)**.

**A Note on Development:** As someone without extensive development experience, this integration was brought to life with significant assistance from AI models, which helped in understanding Home Assistant's architecture, refactoring the code, and implementing new features.

---

### Requirements

* Home Assistant 2023.1 or later
* A [Discogs personal access token](https://www.discogs.com/settings/developers)

---

### Installation

This integration is available through HACS (Home Assistant Community Store).

1. **Add this repository to HACS:**
   * Open HACS in your Home Assistant instance.
   * Go to **Integrations**.
   * Click the **three dots** in the top right corner and select **Custom repositories**.
   * Enter the URL: `https://github.com/andreasc1/homeassistant-discogs-enhanced`
   * Select **Category**: `Integration`.
   * Click **ADD**.
2. **Install the integration:**
   * Navigate back to the HACS **Integrations** tab.
   * Search for **Discogs Enhanced Integration**.
   * Click **Download** and select the latest version.
3. **Restart Home Assistant.**

---

### Configuration

It is strongly recommended to store your Discogs token in `secrets.yaml` rather than directly in `configuration.yaml`.

**`secrets.yaml`:**
```yaml
discogs_token: YOUR_DISCOGS_API_TOKEN
```

**`configuration.yaml`:**
```yaml
sensor:
  - platform: discogs_enhanced
    token: !secret discogs_token
    name: Discogs  # Optional, defaults to "Discogs". See note on entity IDs below.
    monitored_conditions:
      - collection
      - wantlist
      - random_record
      - collection_value_min
      - collection_value_median
      - collection_value_max
      - vinyl_count
      - cd_count
```

> **How the collection stays up to date:** the integration refreshes from the
> Discogs API every **30 minutes** by default. You no longer need to restart
> Home Assistant to see changes (see Changelog → v1.3.0).

---

### Sensors

Entity IDs are derived from the `name` you configure. With the default name
`Discogs`, the entities are:

| Sensor | Description |
|--------|-------------|
| `sensor.discogs_collection` | Total number of releases in your collection |
| `sensor.discogs_wantlist` | Total number of items in your wantlist |
| `sensor.discogs_random_record` | A random record from your collection, with `label`, `cat_no`, `format`, `released`, and `cover_image` attributes |
| `sensor.discogs_collection_value_min` | Estimated minimum market value of your collection |
| `sensor.discogs_collection_value_median` | Estimated median market value of your collection |
| `sensor.discogs_collection_value_max` | Estimated maximum market value of your collection |
| `sensor.discogs_vinyl_records` | Number of Vinyl records in your collection |
| `sensor.discogs_cds` | Number of CDs in your collection |

> If you set a custom `name`, the IDs change accordingly — e.g. `name: My Records`
> produces `sensor.my_records_collection`, `sensor.my_records_vinyl_records`, and so on.
> The dashboard examples below assume the default `Discogs` name.

---

### Dashboard / Lovelace Cards

![Cards](cards.png)

The screenshot above is produced by the configuration in this section: a stats
grid, and a "random record" card showing the cover art plus its metadata.

#### 1. Frontend dependencies

Install these from **HACS → Frontend** (they are third-party cards, not part of
Home Assistant core):

* [`button-card`](https://github.com/custom-cards/button-card)
* [`stack-in-card`](https://github.com/custom-cards/stack-in-card)
* [`card-mod`](https://github.com/thomasloven/lovelace-card-mod)

`picture-entity` is a built-in core card and needs no installation.

#### 2. The random-record camera

The `random_record` sensor exposes the cover-art URL as its `cover_image`
attribute, but a `picture-entity` card needs a **camera** entity to render an
image. Add a `generic` camera that reads that attribute:

**`configuration.yaml`:**
```yaml
camera:
  - platform: generic
    name: RandomRecordDiscogs
    still_image_url: "{{ state_attr('sensor.discogs_random_record', 'cover_image') }}"
    verify_ssl: true
```

This creates `camera.randomrecorddiscogs`, which updates automatically whenever
the sensor rolls a new random record.

#### 3. Lovelace configuration

Replace `YOUR_USERNAME` with your Discogs username (the profile links point to
your public collection/wantlist). Paste this into a dashboard in YAML mode, or
into a Manual card.

```yaml
type: grid
column_span: 1
cards:
  - type: heading
    icon: mdi:record-player
    heading: Discogs Collection
    heading_style: title

  # ── Stats grid ──────────────────────────────────────────────
  - type: custom:stack-in-card
    column_span: 2
    mode: vertical
    cards:
      - type: horizontal-stack
        cards:
          - type: custom:button-card
            entity: sensor.discogs_collection
            name: Total
            show_name: true
            show_state: true
            icon: mdi:music-box-multiple
            styles:
              card:
                - padding: 10px 4px
                - border: none
              grid:
                - grid-template-areas: '"i" "s" "n" "link"'
                - grid-template-rows: 32px auto auto 20px
              icon:
                - color: var(--primary-text-color)
                - width: 22px
              state:
                - font-size: 1.2em
                - font-weight: '600'
              name:
                - font-size: 0.6em
                - text-transform: uppercase
                - opacity: '0.5'
            custom_fields:
              link: >
                [[[ return `<a
                href="https://www.discogs.com/user/YOUR_USERNAME/collection"
                target="_blank" style="color: var(--primary-color);
                text-decoration: none;"><ha-icon icon="mdi:open-in-new"
                style="width: 14px; opacity: 0.6;"></ha-icon></a>` ]]]
          - type: custom:button-card
            entity: sensor.discogs_vinyl_records
            name: Vinyl
            show_name: true
            show_state: true
            icon: mdi:album
            styles:
              card:
                - padding: 10px 4px
              grid:
                - grid-template-areas: '"i" "s" "n" "link"'
                - grid-template-rows: 32px auto auto 20px
              icon:
                - color: '#2196f3'
                - width: 22px
              state:
                - font-size: 1.2em
                - color: '#2196f3'
              name:
                - font-size: 0.6em
                - text-transform: uppercase
                - opacity: '0.5'
            custom_fields:
              link: >
                [[[ return `<a
                href="https://www.discogs.com/user/YOUR_USERNAME/collection"
                target="_blank" style="color: #2196f3; text-decoration:
                none;"><ha-icon icon="mdi:open-in-new" style="width: 14px;
                opacity: 0.6;"></ha-icon></a>` ]]]
          - type: custom:button-card
            entity: sensor.discogs_cds
            name: CDs
            show_name: true
            show_state: true
            icon: mdi:disc
            styles:
              card:
                - padding: 10px 4px
              grid:
                - grid-template-areas: '"i" "s" "n" "link"'
                - grid-template-rows: 32px auto auto 20px
              icon:
                - color: '#ff9800'
                - width: 22px
              state:
                - font-size: 1.2em
                - color: '#ff9800'
              name:
                - font-size: 0.6em
                - text-transform: uppercase
                - opacity: '0.5'
            custom_fields:
              link: >
                [[[ return `<a
                href="https://www.discogs.com/user/YOUR_USERNAME/collection"
                target="_blank" style="color: #ff9800; text-decoration:
                none;"><ha-icon icon="mdi:open-in-new" style="width: 14px;
                opacity: 0.6;"></ha-icon></a>` ]]]
          - type: custom:button-card
            entity: sensor.discogs_wantlist
            name: Wants
            show_name: true
            show_state: true
            icon: mdi:heart
            styles:
              card:
                - padding: 10px 4px
              grid:
                - grid-template-areas: '"i" "s" "n" "link"'
                - grid-template-rows: 32px auto auto 20px
              icon:
                - color: '#e91e63'
                - width: 22px
              state:
                - font-size: 1.2em
                - color: '#e91e63'
              name:
                - font-size: 0.6em
                - text-transform: uppercase
                - opacity: '0.5'
            custom_fields:
              link: >
                [[[ return `<a
                href="https://www.discogs.com/user/YOUR_USERNAME/wantlist"
                target="_blank" style="color: #e91e63; text-decoration:
                none;"><ha-icon icon="mdi:open-in-new" style="width: 14px;
                opacity: 0.6;"></ha-icon></a>` ]]]
      - type: horizontal-stack
        cards:
          - type: custom:button-card
            entity: sensor.discogs_collection_value_min
            name: Min
            show_name: true
            show_state: true
            styles:
              card:
                - background: rgba(200, 147, 10, 0.1)
                - border-radius: 8px
                - padding: 6px
              state:
                - font-size: 0.9em
                - color: '#c8930a'
              name:
                - font-size: 0.55em
                - opacity: 0.6
          - type: custom:button-card
            entity: sensor.discogs_collection_value_median
            name: Median
            show_name: true
            show_state: true
            styles:
              card:
                - background: rgba(0, 135, 90, 0.1)
                - border-radius: 8px
                - padding: 6px
              state:
                - font-size: 0.9em
                - color: '#00875a'
              name:
                - font-size: 0.55em
                - opacity: 0.6
          - type: custom:button-card
            entity: sensor.discogs_collection_value_max
            name: Max
            show_name: true
            show_state: true
            styles:
              card:
                - background: rgba(185, 28, 28, 0.1)
                - border-radius: 8px
                - padding: 6px
              state:
                - font-size: 0.9em
                - color: '#b91c1c'
              name:
                - font-size: 0.55em
                - opacity: 0.6
    card_mod:
      style: |
        ha-card {
          background: rgba(255,255,255,0.03);
          padding: 8px;
          height: 100%;
        }

  # ── Random record card (cover art + metadata) ────────────────
  - type: custom:stack-in-card
    column_span: 1
    cards:
      - type: picture-entity
        entity: camera.randomrecorddiscogs
        camera_image: camera.randomrecorddiscogs
        show_state: false
        show_name: false
        camera_view: auto
        aspect_ratio: '1:1'
        fit_mode: cover
        style: 'ha-card { border-radius: 10px 10px 0 0; overflow: hidden; }'
      - type: custom:button-card
        entity: sensor.discogs_random_record
        show_name: false
        show_icon: false
        styles:
          card:
            - background: none
            - box-shadow: none
            - padding: 0px
          grid:
            - grid-template-areas: '"record_info"'
            - grid-template-columns: 100%
          custom_fields:
            record_info:
              - justify-self: start
              - width: 100%
              - padding: 0px
        custom_fields:
          record_info: |-
            [[[
              const name = entity.state ?? '—';
              const attrs = entity.attributes ?? {};
              const cat = attrs.cat_no ?? 'N/A';
              const label = attrs.label ?? 'N/A';
              const rel = attrs.released ?? 'N/A';

              // Point this at your own public collection
              const url = "https://www.discogs.com/user/YOUR_USERNAME/collection";

              const row = (icon, lbl, val, isHeader = false) => {
                const content = `
                  <div style="display:flex; align-items:center; gap:6px; padding:4px 0; ${!isHeader ? 'border-bottom:1px solid rgba(128,128,128,0.08)' : 'margin-bottom:8px'}">
                    <ha-icon icon="${icon}" style="width:${isHeader ? '16px' : '14px'}; height:${isHeader ? '16px' : '14px'}; opacity:${isHeader ? '1' : '0.38'}; flex-shrink:0; color:var(--primary-color)"></ha-icon>
                    ${lbl ? `<span style="opacity:0.42; font-size:0.7em; text-transform:uppercase; white-space:nowrap; margin-right: 2px;">${lbl}:</span>` : ''}
                    <span style="font-size:${isHeader ? '0.9em' : '0.75em'}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:${isHeader ? '500' : '400'}; flex:1; text-align:left;">${val}</span>
                    ${isHeader ? `<ha-icon icon="mdi:open-in-new" style="width:14px; opacity:0.5; margin-left:4px;"></ha-icon>` : ''}
                  </div>`;

                return isHeader
                  ? `<a href="${url}" target="_blank" style="text-decoration:none; color:inherit;">${content}</a>`
                  : content;
              };
              return `
              <div style="width:100%; padding: 12px 16px; box-sizing:border-box;">
                ${row('mdi:record-player', '', name, true)}
                ${row('mdi:barcode', 'Cat', cat)}
                ${row('mdi:label-outline', 'Label', label)}
                ${row('mdi:calendar-outline', 'Year', rel)}
              </div>`;
            ]]]
```

---

### Changelog

#### v1.3.0 — Periodic Refresh Fix
- **Fixed:** the collection now refreshes on a schedule instead of only at
  Home Assistant startup ([#2](https://github.com/andreasc1/homeassistant-discogs-enhanced/issues/2)).
  All data was previously fetched once during setup and cached; the periodic
  polls only re-read that snapshot, so counts and values stayed frozen until a
  restart.
- Reworked the platform around a `DataUpdateCoordinator`: a single scheduled
  API refresh is now shared across all sensors.
- Default refresh interval is **30 minutes** (a full refresh walks the
  collection folder, so a shorter interval is heavy on Discogs' API rate limit
  for large collections).
- The random-record pick now reuses the data from the format-count pass
  (reservoir sampling), so it costs no additional API calls.
- Transient errors on the value or folder endpoints keep the last-known values
  instead of resetting sensors to `0`.
- **Docs:** corrected the entity IDs in the Sensors table (they are
  `sensor.discogs_*`, not `sensor.discogs_enhanced_*`) and added a Dashboard
  section documenting the random-record camera and Lovelace cards.

#### v1.1.0 — Security Hardening
- Migrated to fully async setup; blocking API calls dispatched to executor
- API token no longer stored in shared state; masked in all log output
- Switched from `requests` to HA's built-in `aiohttp` with explicit TLS verification and timeouts
- Collection value parsing fixed (removed erroneous ×1000/÷1000 arithmetic)
- Cover image URLs validated against a trusted Discogs CDN allowlist
- Financial data downgraded from INFO to DEBUG log level
- Fixed invalid JSON in `hacs.json`

#### v1.0.0 — Initial Release
- Collection, wantlist, and random record sensors
- Collection value (min, median, max) sensors
- Vinyl and CD format count sensors

---

### Support & Contributions

If you encounter any issues or have suggestions, please open an issue on the [GitHub Issue Tracker](https://github.com/andreasc1/homeassistant-discogs-enhanced/issues).

Contributions are welcome — feel free to fork the repository and submit pull requests.
