# WordPress Inventory Site — Data Source Reference

Reference for where [inventory.joinbuyerslist.com](https://inventory.joinbuyerslist.com/) loads listing data from.

Related: [wordpress.md](./wordpress.md) (RichListings → WordPress publish pipeline).

---

## Quick answer

| Item | Value |
|------|--------|
| Live site | `https://inventory.joinbuyerslist.com` |
| Server path | `/home/linkglob/inventory.joinbuyerslist.com` |
| MySQL database | `linkglob_jblinv` |
| Table prefix | `listing_` |
| Primary content table | **`listing_posts`** (`post_type = 'house'`) |
| Custom fields (price, etc.) | **`listing_postmeta`** |
| Taxonomies (county, region, type) | **`listing_terms`**, **`listing_term_taxonomy`**, **`listing_term_relationships`** |

The homepage does **not** read MongoDB / RichAI `parsed_listings` directly. RichListings syncs into WordPress; the public site queries WordPress MySQL.

---

## Hosting / cPanel

| Item | Value |
|------|--------|
| cPanel URL | `https://joinbuyerslist.com/cpanel` (also `:2083`) |
| Account user | `linkglob` |
| Docroot for inventory | `/home/linkglob/inventory.joinbuyerslist.com` |
| Theme | `joinbuyer` |

---

## How the homepage loads data

1. Front page is WordPress **page ID 2** (“All Deals”).
2. Template: `wp-content/themes/joinbuyer/all-deals.php`.
3. Template queries published posts with `post_type => 'house'`, filtered by taxonomy `country_deals`, ordered by meta `asking_price`.
4. Search/filter form POSTs to `admin-ajax.php` with action **`datefilter`**.
5. Handler: `wp-content/themes/joinbuyer/templates/ajax-function/datefilter.php` (same `house` post type + tax/meta queries).

### Core query (from `all-deals.php`)

```php
$args = array(
    'post_type'      => 'house',
    'posts_per_page' => -1,
    'post_status'    => 'publish',
    'order'          => 'ASC',
    'orderby'        => 'meta_value_num',
    'meta_key'       => 'asking_price',
    'tax_query'      => array(
        array(
            'taxonomy' => 'country_deals',
            'field'    => 'id',
            'terms'    => $term->term_id,
        ),
    ),
);
$deals = get_posts($args);
```

---

## WordPress content model

### Custom post type

| CPT | Theme evidence | Stored as |
|-----|----------------|-----------|
| `house` | `single-house.php`, queries in `all-deals.php` | Rows in `listing_posts` with `post_type = 'house'` |

### Taxonomies (on `house`)

| Taxonomy | Slug / URL | Role |
|----------|------------|------|
| `country_deals` | `/country-deals/...` | Counties + property types (e.g. Broward, Single Family) |
| `region` | `/region/...` | Florida regions / counties hierarchy |
| `property_name` | — | Property type labels |
| `deal_type` | — | Deal types |
| `newest_deals` | — | e.g. “Todays Deal” |

Registered in theme `functions.php` (e.g. `create_taxonomy(... 'country_deals' ... 'house')`).

### Important post meta (live counts, top listing-related keys)

| Meta key | Approx. rows | Used for |
|----------|--------------|----------|
| `address` | 3337 | Property address |
| `asking_price` | 3334 | Sort + price filter on homepage / AJAX search |
| `zip_code` | 3322 | Zip filter / display |
| `picture_button_url` | 2568 | Gallery / “Click Here for Pictures” link |
| `custom_title` | 2552 | Alternate title |
| `slider_image` | 2358 | Slider / featured image ref |
| `google_map_external_link` | 2358 | Map link |
| `featured_property` | 2358 | Featured flag |
| `_thumbnail_id` | 3161 | WP featured image attachment ID |

ACF-style underscore twins also exist (`_asking_price`, `_address`, …). Media attachments use `_wp_attached_file` / `_wp_attachment_metadata`.

RichListings writes several of these via the `addproperty/v1` REST plugin.

---

## MySQL tables involved (WordPress core shape)

Prefix: `listing_`

| Table | Role for inventory page |
|-------|-------------------------|
| `listing_posts` | Property posts (`post_type = 'house'`) and pages |
| `listing_postmeta` | `asking_price` and other custom fields |
| `listing_terms` | Term names/slugs |
| `listing_term_taxonomy` | Taxonomy type + counts (`country_deals`, `region`, …) |
| `listing_term_relationships` | Links posts ↔ terms |
| `listing_options` | Site options (URLs, permalinks, etc.) |
| `listing_users` / `listing_usermeta` | WP users (not listing content) |

Typical join path for a listing card:

```
listing_posts (post_type='house', post_status='publish')
  ├─ listing_postmeta (asking_price, …)
  └─ listing_term_relationships
       └─ listing_term_taxonomy (taxonomy='country_deals'|…)
            └─ listing_terms
```

---

## Relation to RichListings (MongoDB)

| System | Store | Role |
|--------|--------|------|
| RichAI / RichListings | MongoDB `parsed_listings` | Pipeline, AI text, sync state (`wp_status`, `post_id`) |
| Inventory WordPress | MySQL `linkglob_jblinv` | What the public site displays |

Sync API: `https://inventory.joinbuyerslist.com/wp-json/addproperty/v1` (`WP_API_TOKEN`). See [wordpress.md](./wordpress.md).

---

## Database schema

Inspected live via cPanel → phpMyAdmin on **2026-07-21**. Database: `linkglob_jblinv`. Prefix: `listing_`.

Column types below are from phpMyAdmin structure pages. Primary keys are typically `bigint(20) unsigned` + `AUTO_INCREMENT` (standard WordPress); the UI sometimes omits `unsigned` / `auto_increment` in the type cell.

### Live row counts (`listing_posts`)

| `post_type` | `post_status` | Count |
|-------------|---------------|------:|
| `attachment` | `inherit` | 23231 |
| `house` | `trash` | 2428 |
| `house` | `publish` | **677** |
| `house` | `private` | 236 |
| `revision` | `inherit` | 162 |
| `page` | `publish` | 34 |
| `acf-field` | `publish` | 65 |
| other | various | small |

Homepage inventory = **`house` + `publish`** (~677 at inspection time).

### Taxonomy term counts (`listing_term_taxonomy`)

| taxonomy | term rows |
|----------|----------:|
| `country_deals` | 345 |
| `region` | 66 |
| `property_name` | 6 |
| `newest_deals` | 3 |
| `deal_type` | 2 |
| `category` / `post_tag` | 1 each |

### `listing_posts` (23 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `ID` | bigint(20) | NO | — |
| `post_author` | bigint(20) | NO | 0 |
| `post_date` | datetime | NO | 0000-00-00 00:00:00 |
| `post_date_gmt` | datetime | NO | 0000-00-00 00:00:00 |
| `post_content` | longtext | NO | — |
| `post_title` | text | NO | — |
| `post_excerpt` | text | NO | — |
| `post_status` | varchar(20) | NO | publish |
| `comment_status` | varchar(20) | NO | open |
| `ping_status` | varchar(20) | NO | open |
| `post_password` | varchar(255) | NO | — |
| `post_name` | varchar(200) | NO | — |
| `to_ping` | text | NO | — |
| `pinged` | text | NO | — |
| `post_modified` | datetime | NO | 0000-00-00 00:00:00 |
| `post_modified_gmt` | datetime | NO | 0000-00-00 00:00:00 |
| `post_content_filtered` | longtext | NO | — |
| `post_parent` | bigint(20) | NO | 0 |
| `guid` | varchar(255) | NO | — |
| `menu_order` | int(11) | NO | 0 |
| `post_type` | varchar(20) | NO | post |
| `post_mime_type` | varchar(100) | NO | — |
| `comment_count` | bigint(20) | NO | 0 |

### `listing_postmeta` (4 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `meta_id` | bigint(20) | NO | — |
| `post_id` | bigint(20) | NO | 0 |
| `meta_key` | varchar(255) | YES | NULL |
| `meta_value` | longtext | YES | NULL |

### `listing_terms` (5 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `term_id` | bigint(20) | NO | — |
| `name` | varchar(200) | NO | — |
| `slug` | varchar(200) | NO | — |
| `term_group` | bigint(10) | NO | 0 |
| `term_order` | int(4) | YES | 0 |

### `listing_term_taxonomy` (6 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `term_taxonomy_id` | bigint(20) | NO | — |
| `term_id` | bigint(20) | NO | 0 |
| `taxonomy` | varchar(32) | NO | — |
| `description` | longtext | NO | — |
| `parent` | bigint(20) | NO | 0 |
| `count` | bigint(20) | NO | 0 |

### `listing_term_relationships` (3 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `object_id` | bigint(20) | NO | 0 |
| `term_taxonomy_id` | bigint(20) | NO | 0 |
| `term_order` | int(11) | NO | 0 |

### `listing_termmeta` (4 columns)

| Column | Type | Null | Default |
|--------|------|------|---------|
| `meta_id` | bigint(20) | NO | — |
| `term_id` | bigint(20) | NO | 0 |
| `meta_key` | varchar(255) | YES | NULL |
| `meta_value` | longtext | YES | NULL |

### All tables in `linkglob_jblinv` (51)

WordPress core / content:

- `listing_posts`, `listing_postmeta`
- `listing_terms`, `listing_termmeta`, `listing_term_taxonomy`, `listing_term_relationships`
- `listing_options`, `listing_users`, `listing_usermeta`
- `listing_comments`, `listing_commentmeta`, `listing_links`

Plugin / membership / tooling (not used by homepage `house` query):

- Action Scheduler: `listing_actionscheduler_*`
- Indeed Membership Pro (`ihc_*`, `indeed_members_payments`)
- Pretty Links / short links: `listing_kc_us_*`
- WP All Export: `listing_pmxe_*`
- Duplicator: `listing_duplicator_packages`
- Misc: `listing_ddp_log`

---

## Useful paths on server

```
/home/linkglob/inventory.joinbuyerslist.com/
  wp-config.php                          # DB_NAME, table_prefix
  wp-content/themes/joinbuyer/
    all-deals.php                        # Homepage listing query
    functions.php                        # CPT/taxonomy + AJAX includes
    single-house.php                     # Single property template
    taxonomy-country_deals.php           # County/type archive
    templates/ajax-function/datefilter.php
```

---

## Notes

- WordPress version observed on site: **5.9.x**.
- Other MySQL DBs exist on the same cPanel account (`linkglob_jblmain`, etc.); inventory site uses **`linkglob_jblinv` only**.
- Do not commit cPanel or DB passwords into this repo.
