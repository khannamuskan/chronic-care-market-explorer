"""Central configuration for the Chronic Care Market Prioritization Explorer.

Everything that a reviewer might want to tweak (indicator scope, API endpoint,
quality thresholds, scoring weights) lives here rather than being scattered
through the ETL and app code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
LOG_DIR = PROJECT_ROOT / "logs"

for _d in (RAW_DIR, PROCESSED_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Reference data is checked in, unlike anything under data/. It is a small,
# slow-moving denominator that must be reviewable in a diff rather than
# re-fetched (and silently changed) on every run.
REFERENCE_DIR = PROJECT_ROOT / "reference"
POPULATION_REFERENCE_PATH = REFERENCE_DIR / "state_adult_population.csv"

DUCKDB_PATH = PROCESSED_DIR / "warehouse.duckdb"
RUN_MANIFEST_PATH = PROCESSED_DIR / "run_manifest.json"
DQ_SUMMARY_PATH = PROCESSED_DIR / "dq_summary.json"

# --------------------------------------------------------------------------
# Source API  --  CDC Chronic Disease Indicators (keyless, public domain)
# https://data.cdc.gov/Chronic-Disease-Indicators/U-S-Chronic-Disease-Indicators/hksd-2xuw
# --------------------------------------------------------------------------
API_BASE_URL = "https://data.cdc.gov/resource/hksd-2xuw.json"
API_DATASET_ID = "hksd-2xuw"
API_PAGE_SIZE = 5_000
API_MAX_RETRIES = 4
API_BACKOFF_SECONDS = 2.0
API_TIMEOUT_SECONDS = 60
# Socrata is happier with an explicit UA; it 403s some default client agents.
API_USER_AGENT = "chronic-care-market-explorer/1.0 (assignment; python-requests)"
# Optional: a free Socrata app token raises rate limits. Not required.
APP_TOKEN_ENV_VAR = "SOCRATA_APP_TOKEN"


# --------------------------------------------------------------------------
# Product scope: the four chronic-care pillars a digital health company
# (diabetes / hypertension / weight / behavioural health) actually sells into.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Indicator:
    indicator_id: str  # CDC questionid
    topic_id: str  # CDC topicid
    pillar: str
    short_label: str
    polarity: str  # "risk" -> higher is worse ; "protective" -> higher is better
    measure_role: str  # "burden" -> feeds burden index ; "care_gap" -> treatment coverage


INDICATORS: tuple[Indicator, ...] = (
    Indicator("DIA01", "DIA", "Diabetes", "Diabetes prevalence", "risk", "burden"),
    Indicator("CVD01", "CVD", "Cardiovascular", "High blood pressure", "risk", "burden"),
    Indicator("CVD03", "CVD", "Cardiovascular", "High cholesterol", "risk", "burden"),
    Indicator("CVD02", "CVD", "Cardiovascular", "BP medication taken", "protective", "care_gap"),
    Indicator("CVD04", "CVD", "Cardiovascular", "Cholesterol medication taken", "protective", "care_gap"),
    Indicator("NPW14", "NPAW", "Weight & Activity", "Obesity", "risk", "burden"),
    Indicator("NPW06", "NPAW", "Weight & Activity", "No leisure-time physical activity", "risk", "burden"),
    Indicator("MEN02", "MEN", "Behavioral Health", "Depression", "risk", "burden"),
    Indicator("MEN05", "MEN", "Behavioral Health", "Frequent mental distress", "risk", "burden"),
)

INDICATOR_IDS: tuple[str, ...] = tuple(i.indicator_id for i in INDICATORS)
PILLARS: tuple[str, ...] = tuple(dict.fromkeys(i.pillar for i in INDICATORS))

# Prevalence measures only. Age-adjusted is preferred for cross-state comparison
# because state age structures differ materially; crude is the fallback.
MEASURE_TYPES: tuple[str, ...] = ("AGEADJPREV", "CRDPREV")
PREFERRED_MEASURE = "AGEADJPREV"
FALLBACK_MEASURE = "CRDPREV"

# Demographic stratifications we keep. CDC also ships GRADE bands that only
# apply to youth surveys, which are out of scope for an adult-care product.
ALLOWED_STRATUM_CATEGORIES: tuple[str, ...] = ("OVERALL", "RACE", "SEX", "AGE")
OVERALL_STRATUM_ID = "OVR"

# CDC ships 50 states + DC + territories + a national rollup.
# We treat the 50 states + DC as the addressable commercial market.
TERRITORY_ABBRS: frozenset[str] = frozenset({"PR", "GU", "VI", "AS", "MP", "PW", "FM", "MH"})
NATIONAL_ABBR = "US"

CENSUS_REGION: dict[str, str] = {
    # Northeast
    "CT": "Northeast", "ME": "Northeast", "MA": "Northeast", "NH": "Northeast",
    "RI": "Northeast", "VT": "Northeast", "NJ": "Northeast", "NY": "Northeast",
    "PA": "Northeast",
    # Midwest
    "IL": "Midwest", "IN": "Midwest", "MI": "Midwest", "OH": "Midwest",
    "WI": "Midwest", "IA": "Midwest", "KS": "Midwest", "MN": "Midwest",
    "MO": "Midwest", "NE": "Midwest", "ND": "Midwest", "SD": "Midwest",
    # South
    "DE": "South", "DC": "South", "FL": "South", "GA": "South", "MD": "South",
    "NC": "South", "SC": "South", "VA": "South", "WV": "South", "AL": "South",
    "KY": "South", "MS": "South", "TN": "South", "AR": "South", "LA": "South",
    "OK": "South", "TX": "South",
    # West
    "AZ": "West", "CO": "West", "ID": "West", "MT": "West", "NV": "West",
    "NM": "West", "UT": "West", "WY": "West", "AK": "West", "CA": "West",
    "HI": "West", "OR": "West", "WA": "West",
}

# --------------------------------------------------------------------------
# Data quality thresholds
# --------------------------------------------------------------------------
# CDC suppresses estimates with unreliable sample sizes. Some suppression is
# expected and normal; a spike means the extract or the source broke.
MAX_SUPPRESSION_RATE_FAIL = 0.35
MAX_SUPPRESSION_RATE_WARN = 0.20
# A national indicator-year should cover most of the 51 state-level geographies.
MIN_STATE_COVERAGE = 45
N_ADDRESSABLE_STATES = 51  # 50 states + DC
# Prevalence is a percentage.
VALUE_MIN, VALUE_MAX = 0.0, 100.0
# Robust z-score cut-off for flagging implausible estimates.
OUTLIER_Z_THRESHOLD = 4.0
# The CDI release lags real time; anything older than this is stale.
MAX_DATA_AGE_YEARS = 4

# --------------------------------------------------------------------------
# Opportunity score weights (documented in README; tunable in the app sidebar)
# --------------------------------------------------------------------------
SCORE_WEIGHTS: dict[str, float] = {
    "burden": 0.45,      # how sick the population is today
    "trend": 0.20,       # is it getting worse
    "equity_gap": 0.20,  # unmet need concentrated in specific groups
    "care_gap": 0.15,    # people diagnosed but not on treatment
}

TREND_WINDOW_YEARS = 5

# --------------------------------------------------------------------------
# Market sizing
# --------------------------------------------------------------------------
# Vintage of the committed population reference. Kept explicit so a mismatch
# between the scoring year and the denominator year is visible rather than
# assumed -- analytics records both and the app reports the gap.
POPULATION_YEAR = 2023
POPULATION_SOURCE = (
    "U.S. Census Bureau, Population Estimates Program, Vintage 2024 "
    "(civilian population by single year of age, 18+)"
)

# Two defensible ways to rank a market, and they disagree:
#   rate  -- where chronic disease is most concentrated (efficiency)
#   lives -- where the most affected people actually live (volume)
RANKING_BASES = ("rate", "lives")
DEFAULT_RANKING_BASIS = "rate"
