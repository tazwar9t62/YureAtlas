# Catalogue sources and basemap

## USGS

[Official event service](https://earthquake.usgs.gov/fdsnws/event/1/) and [GeoJSON feeds](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php), consulted 2026-09-29. The query service caps responses at 20,000 records. Imports use 30-day chunks, preflight counts, recursive subdivision at 19,000, and count-versus-payload verification. Inclusive service boundaries are filtered locally to half-open intervals. Requests have timeouts, bounded exponential retry, and a rate limit. Recent feeds supplement reconciliation queries; feed delivery alone never establishes complete scoring coverage.

USGS selects a preferred magnitude whose type may vary. `usgs_preferred_m4_v1` targets that magnitude >= 4.0; it is neither uniform Mj nor uniform Mw. The input threshold is 3.0. Data attribution: USGS Earthquake Hazards Program, public domain. A successful query establishes retrieval health, not physical-event catalogue completeness.

Source namespaces include query domain and input threshold so narrower retrieval cannot establish negative labels for a wider experiment. Absence from any query does not create a deletion. Explicit deletion revisions do. Origin, provider update, and local receipt timestamps remain separate. Late delivery of an older provider version does not supersede a newer one. Raw content is content-addressed and verified on reuse.

## JMA

[Official catalogue](https://www.data.jma.go.jp/eqev/data/bulletin/hypo_e.html), [96-byte record format](https://www.data.jma.go.jp/eqev/data/bulletin/data/format/hypfmt_e.html), and [usage terms](https://www.jma.go.jp/jma/en/copyright.html). The implementation was checked against the official `h2023.zip` download, not a converted JSON API. The one-line test fixture comes from that file (JMA attribution retained here).

Parser: JST origin time converted to UTC; FORTRAN implied decimal fields; depth-slice integer fields; missing depth retained as null; JMA agency J and natural-earthquake flags 1/2/5; selected magnitude type J/D/d/V/v. Non-JMA determinations, unaccepted hypocentres, and unusable magnitudes are reported and excluded. Negative magnitude letter notation is supported. A separate `jma_mj4_v1` experiment is required. The JMA magnitude family is recorded exactly, not relabelled as uniform Mw.

This file format has **no stable event ID**. Identity is explicitly `file namespace:line number`. Reimport the identical file under the same namespace to deduplicate. A revised file with reordered/inserted lines must use a new source namespace/experiment; automatic cross-file event matching is not claimed. No ZIP extraction is performed by the application: provide a bounded uncompressed official file.

Canonical CSV columns: `provider_id,origin_time,longitude,latitude,depth_km,magnitude,magnitude_type,provider_updated,status`. Optional depth/update/status may be blank; timestamps must carry a timezone; coordinates are decimal degrees; depth is km. Explicit column mapping is supported by the parser. Import reports retain accepted/rejected counts and row errors. CSV imports cannot silently mix providers. User-attested coverage and rejected rows remain visible.

## Offline basemap

Natural Earth 1:50m land, release 5.1.2, clipped and simplified for the Japan region. [Public domain terms](https://www.naturalearthdata.com/about/terms-of-use/). Attribution: “Made with Natural Earth.” The local GeoJSON requires no runtime internet connection, tiles, fonts, or map API key. It is contextual cartography; forecast cells use the independent versioned EPSG:6933 domain.
