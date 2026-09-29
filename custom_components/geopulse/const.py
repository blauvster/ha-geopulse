"""Constants for the GeoPulse integration."""

DOMAIN = "geopulse"

# GeoPulse's GpsSourceType enum (backend/src/main/java/.../shared/gps/GpsSourceType.java),
# confirmed against tess1o/geopulse@main. HOME_ASSISTANT is always excluded from import
# selection since it's data this integration itself exports.
SOURCE_TYPE_OWNTRACKS = "OWNTRACKS"
SOURCE_TYPE_GPSLOGGER = "GPSLOGGER"
SOURCE_TYPE_OVERLAND = "OVERLAND"
SOURCE_TYPE_TRACCAR = "TRACCAR"
SOURCE_TYPE_GOOGLE_TIMELINE = "GOOGLE_TIMELINE"
SOURCE_TYPE_GPX = "GPX"
SOURCE_TYPE_DAWARICH = "DAWARICH"
SOURCE_TYPE_HOME_ASSISTANT = "HOME_ASSISTANT"
SOURCE_TYPE_GEOJSON = "GEOJSON"
SOURCE_TYPE_CSV = "CSV"
SOURCE_TYPE_COLOTA = "COLOTA"
SOURCE_TYPE_MANUAL = "MANUAL"
SOURCE_TYPE_MOBILE_APP = "MOBILE_APP"

# API paths, confirmed against backend source (see Plan.md §12).
API_PATH_GPS_SOURCE = "/api/gps/source/"
API_PATH_GPS_LAST_KNOWN_POSITION = "/api/gps/last-known-position"
# Paginated point list. Not /api/gps/points - that path is POST-only (mobile
# app ingest) and answers GET with 405; verified against a live server.
API_PATH_GPS_POINTS = "/api/gps"
API_PATH_FRIENDS = "/api/friends"
API_PATH_FRIEND_LOCATION = "/api/friends/{friend_id}/location"
API_PATH_FRIENDS_LOCATION_TRAILS = "/api/friends/location/trails"
API_PATH_HOMEASSISTANT_INGEST = "/api/homeassistant"
# Stays/trips/gaps plus path segments for the token owner and every friend
# sharing their timeline, in one call (card only).
API_PATH_MULTI_USER_TIMELINE = "/api/streaming-timeline/multi-user"

# GET /api/friends/location/trails caps how far back `minutes` can look.
FRIENDS_TRAILS_MAX_MINUTES = 1440

# Config entry / options keys.
CONF_BASE_URL = "base_url"
CONF_READ_TOKEN = "read_token"
CONF_IMPORT_SOURCE_TYPES = "import_source_types"
CONF_IMPORT_AGGREGATE_ACCOUNT = "import_aggregate_account"
CONF_IMPORT_FRIEND_IDS = "import_friend_ids"
# {entity_id: device_id} - device_id is the payload field GeoPulse uses to
# tell exported devices apart; defaults to the entity_id (Plan.md §3).
CONF_EXPORT_ENTITIES = "export_entities"
# {entity_id: location-source token} in entry.data. GeoPulse keeps one
# timeline per user, so each person's trackers go to their own account.
CONF_EXPORT_ENTITY_TOKENS = "export_entity_tokens"
CONF_EXPORT_RETRY_QUEUE = "export_retry_queue"
CONF_POLL_INTERVAL = "poll_interval"
CONF_RECORDER_EXCLUDE = "recorder_exclude"

DEFAULT_POLL_INTERVAL_SECONDS = 45
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 3600

# Display names for the import picker, keyed by GpsSourceType.
SOURCE_TYPE_LABELS = {
    SOURCE_TYPE_OWNTRACKS: "OwnTracks",
    SOURCE_TYPE_GPSLOGGER: "GPSLogger",
    SOURCE_TYPE_OVERLAND: "Overland",
    SOURCE_TYPE_TRACCAR: "Traccar",
    SOURCE_TYPE_GOOGLE_TIMELINE: "Google Timeline",
    SOURCE_TYPE_GPX: "GPX",
    SOURCE_TYPE_DAWARICH: "Dawarich",
    SOURCE_TYPE_HOME_ASSISTANT: "Home Assistant",
    SOURCE_TYPE_GEOJSON: "GeoJSON",
    SOURCE_TYPE_CSV: "CSV",
    SOURCE_TYPE_COLOTA: "Colota",
    SOURCE_TYPE_MANUAL: "Manual",
    SOURCE_TYPE_MOBILE_APP: "Mobile app",
}
