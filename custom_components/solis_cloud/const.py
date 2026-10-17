"""Constants for Solis Cloud integration."""

DOMAIN = "solis_cloud"
DEFAULT_NAME = "Solis Cloud"
DEFAULT_API_URL = "https://www.soliscloud.com:13333"

# Config keys
CONF_KEY_ID = "key_id"
CONF_KEY_SECRET = "key_secret"
CONF_STATION_ID = "station_id"
CONF_API_URL = "api_url"

# Update interval in seconds
UPDATE_INTERVAL = 120  # 2 minutes

# === v1.1.0 (2026-10-17) - odpornosc na chwilowe bledy SolisCloud ===
# Diagnoza: w 10 dni 138 przerw (7,9 h niedostepnosci), WSZYSTKIE w godzinach
# produkcji (04-18), zero w nocy -> chmura SolisCloud odrzuca zapytania pod
# obciazeniem. Typowa przerwa 2,0-2,6 min = dokladnie JEDEN nieudany cykl,
# bo kazdy blad wywracal caly koordynator i gasil wszystkie 22 encje.
MAX_RETRIES = 3                 # liczba prob na pojedyncze zapytanie
RETRY_BACKOFF = [2, 5]          # sekundy przed 2. i 3. proba
MIN_REQUEST_SPACING = 3.0       # min. odstep miedzy DOWOLNYMI dwoma zapytaniami
                                # (SolisCloud limituje czestotliwosc na klucz API,
                                #  a cykl wysylal 3 zapytania jedno po drugim)
