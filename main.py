import os
import time
import logging
from datetime import datetime
import pandas as pd
import requests
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import google.protobuf.field_mask_pb2

# --- LOGGING & CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
TARGET_ROAS = float(os.getenv("TARGET_ROAS", 4.0))  # Benchmark ROAS (4.0 = 400%)
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", 14400))  # 4-hour cycle


# =====================================================================
# 1. SIGNAL INGESTION: WEATHER & DEMAND INDEX
# =====================================================================

def get_weather_data(city: str, api_key: str) -> dict:
    """Fetch real-time weather conditions and temperature from OpenWeatherMap."""
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return {
            "condition": data["weather"][0]["main"],  # Rain, Snow, Clear, Clouds, Extreme
            "temp_c": data["main"]["temp"]
        }
    except requests.RequestException as e:
        logging.warning(f"Weather lookup failed for {city}: {e}. Defaulting to baseline.")
        return {"condition": "Clear", "temp_c": 20.0}


def calculate_demand_multiplier(category: str, weather: dict, demand_index: float) -> float:
    """
    Computes external demand multiplier combining real-time weather catalysts
    with regional search/trend velocity index (1.0 = baseline demand).
    """
    condition = weather["condition"]
    temp = weather["temp_c"]
    weather_multiplier = 1.00

    if category == "Winter Accessories":  # Chains, anti-freeze, winter mats, ice scrapers
        if condition == "Snow" or temp <= 0:
            weather_multiplier = 1.60
        elif temp <= 5:
            weather_multiplier = 1.25

    elif category == "Wipers & Vision":   # Wiper blades, washer fluid, rain repellents
        if condition in ["Rain", "Thunderstorm", "Drizzle"]:
            weather_multiplier = 1.50
        elif condition == "Snow":
            weather_multiplier = 1.30

    elif category == "Summer & Cooling":  # Sunshades, AC recharge, seat coolers
        if temp >= 30:
            weather_multiplier = 1.45
        elif temp >= 25 and condition == "Clear":
            weather_multiplier = 1.20

    elif category == "Batteries & Electrical":  # Extreme temperatures stress car batteries
        if temp <= -5 or temp >= 35:
            weather_multiplier = 1.35

    # Scale weather factor by local search trend / demand velocity (e.g., 1.2 = +20% surge)
    combined_demand_multiplier = weather_multiplier * demand_index
    return combined_demand_multiplier


# =====================================================================
# 2. SIGNAL INGESTION: INVENTORY & PRICING/MARGINS
# =====================================================================

def get_inventory_multiplier(stock_status: str, stock_on_hand: int, days_of_supply: int) -> float:
    """
    Throttles bids on constrained inventory; accelerates bidding on aging/overstocked items.
    """
    if stock_status == "Out of Stock" or stock_on_hand == 0:
        return 0.50  # Lower bound reduction to preserve budget
    elif stock_status == "Low Stock" or days_of_supply < 7:
        return 0.70  # Preserve remaining stock for direct high-margin visits
    elif days_of_supply > 60:
        return 1.25  # High holding cost / liquidation priority: push volume
    return 1.00


def get_pricing_margin_multiplier(margin_tier: str, price_competitiveness: str) -> float:
    """
    Calculates multiplier based on gross margin tier and competitive pricing advantage.
    - margin_tier: 'High' (>40%), 'Medium' (20-40%), 'Low' (<20%)
    - price_competitiveness: 'Advantage' (lower than market), 'Parity', 'Premium'
    """
    margin_weights = {
        "High": 1.25,
        "Medium": 1.00,
        "Low": 0.85
    }
    comp_weights = {
        "Advantage": 1.15,  # Higher expected conversion rate due to price advantage
        "Parity": 1.00,
        "Premium": 0.90
    }
    
    m_factor = margin_weights.get(margin_tier, 1.00)
    c_factor = comp_weights.get(price_competitiveness, 1.00)
    return m_factor * c_factor


# =====================================================================
# 3. PERFORMANCE CLOSED-LOOP: GOOGLE ADS API
# =====================================================================

def get_geo_roas_performance(client: GoogleAdsClient, customer_id: str) -> pd.DataFrame:
    """Pull 7-day ROAS (Conversion Value / Cost) per geographic target from Google Ads API."""
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            geographic_view.country_criterion_id,
            metrics.cost_micros,
            metrics.conversions_value
        FROM geographic_view
        WHERE segments.date DURING LAST_7_DAYS
    """
    records = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                cost = (row.metrics.cost_micros / 1_000_000)
                conv_val = row.metrics.conversions_value
                roas = (conv_val / cost) if cost > 0 else 0.0
                records.append({
                    "geo_id": str(row.geographic_view.country_criterion_id),
                    "roas": round(roas, 2)
                })
        return pd.DataFrame(records)
    except GoogleAdsException as ex:
        logging.error(f"Google Ads API Error: {ex}")
        return pd.DataFrame(columns=["geo_id", "roas"])


def update_conversion_value_rule(client: GoogleAdsClient, customer_id: str, rule_id: str, multiplier: float):
    """Mutate a Conversion Value Rule with the newly computed composite multiplier."""
    service = client.get_service("ConversionValueRuleService")
    operation = client.get_type("ConversionValueRuleOperation")

    rule = operation.update
    rule.resource_name = service.conversion_value_rule_path(customer_id, rule_id)
    rule.action.value = multiplier

    client.copy_from(
        operation.update_mask,
        google.protobuf.field_mask_pb2.FieldMask(paths=["action.value"])
    )

    try:
        service.mutate_conversion_value_rules(customer_id=customer_id, operations=[operation])
        logging.info(f"Updated Rule {rule_id} -> Multiplier: {multiplier}x")
    except GoogleAdsException as ex:
        logging.error(f"Failed to mutate rule {rule_id}: {ex}")


# =====================================================================
# 4. MASTER ORCHESTRATION PIPELINE
# =====================================================================

def run_strategic_update(client: GoogleAdsClient, catalog_feed_df: pd.DataFrame):
    """Orchestrates signal gathering, composite calculation, and value rule mutation."""
    logging.info(f"--- Starting Sync Cycle: {datetime.now()} ---")
    perf_df = get_geo_roas_performance(client, CUSTOMER_ID)

    for _, row in catalog_feed_df.iterrows():
        # Signal 1: External Demand & Weather
        weather = get_weather_data(row["city"], OPENWEATHER_API_KEY)
        demand_mult = calculate_demand_multiplier(row["category"], weather, row["demand_index"])

        # Signal 2: Inventory Constraints & Liquidation
        inventory_mult = get_inventory_multiplier(
            row["stock_status"], 
            row["stock_on_hand"], 
            row["days_of_supply"]
        )

        # Signal 3: Margin & Pricing Competitiveness
        margin_mult = get_pricing_margin_multiplier(
            row["margin_tier"], 
            row["price_competitiveness"]
        )

        # Signal 4: ROAS Feedback Loop
        roas_mult = 1.00
        if not perf_df.empty and str(row["geo_id"]) in perf_df["geo_id"].values:
            actual_roas = perf_df.loc[perf_df["geo_id"] == str(row["geo_id"]), "roas"].values[0]
            if actual_roas > TARGET_ROAS * 1.20:
                roas_mult = 1.15  # Outperforming: scale bids
            elif 0 < actual_roas < TARGET_ROAS * 0.80:
                roas_mult = 0.85  # Lagging: throttle bids

        # Master Composite Multiplier Calculation
        raw_multiplier = demand_mult * inventory_mult * margin_mult * roas_mult

        # Clamp between Google Ads permitted range (0.5x to 10.0x)
        final_multiplier = round(max(0.5, min(raw_multiplier, 10.0)), 2)

        logging.info(
            f"[{row['city']} - {row['category']}] "
            f"Weather: {weather['condition']} ({weather['temp_c']}°C) | "
            f"Stock: {row['stock_status']} | Margin: {row['margin_tier']} | "
            f"Target Multiplier -> {final_multiplier}x"
        )

        update_conversion_value_rule(client, CUSTOMER_ID, str(row["rule_id"]), final_multiplier)

    logging.info("--- Sync Cycle Completed Successfully ---")


# =====================================================================
# 5. EXECUTION ENTRY POINT
# =====================================================================

if __name__ == "__main__":
    # Load Google Ads client using google-ads.yaml in the root folder
    ads_client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    # Sample input mapping operational/ERP data to Google Ads Value Rule IDs
    catalog_targets = pd.DataFrame([
        {
            "city": "Budapest",
            "geo_id": "1014570",
            "rule_id": "9001001",
            "category": "Winter Accessories",
            "demand_index": 1.20,
            "stock_status": "In Stock",
            "stock_on_hand": 140,
            "days_of_supply": 45,
            "margin_tier": "High",
            "price_competitiveness": "Advantage"
        },
        {
            "city": "Eger",
            "geo_id": "1027744",
            "rule_id": "9001002",
            "category": "Wipers & Vision",
            "demand_index": 1.05,
            "stock_status": "In Stock",
            "stock_on_hand": 85,
            "days_of_supply": 30,
            "margin_tier": "Medium",
            "price_competitiveness": "Parity"
        },
        {
            "city": "Bedford",
            "geo_id": "1013442",
            "rule_id": "9001003",
            "category": "Summer & Cooling",
            "demand_index": 1.30,
            "stock_status": "Low Stock",
            "stock_on_hand": 8,
            "days_of_supply": 4,
            "margin_tier": "High",
            "price_competitiveness": "Premium"
        },
        {
            "city": "Praga",
            "geo_id": "1016367",
            "rule_id": "9001004",
            "category": "Batteries & Electrical",
            "demand_index": 1.00,
            "stock_status": "In Stock",
            "stock_on_hand": 350,
            "days_of_supply": 75,
            "margin_tier": "Low",
            "price_competitiveness": "Parity"
        }
    ])

    # Run loop
    while True:
        try:
            run_strategic_update(ads_client, catalog_targets)
        except Exception as err:
            logging.error(f"Execution loop failed: {err}")
        time.sleep(LOOP_INTERVAL_SECONDS)
      feat: add master value rules automation script
