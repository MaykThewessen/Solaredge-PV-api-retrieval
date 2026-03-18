#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun May  4 23:26:39 2025

@author: mayk
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from tqdm import tqdm
import time

## importing the load_dotenv from the python-dotenv module
from dotenv import load_dotenv
load_dotenv()

# Setup Environment & # Load API Key & Configurations
import os
os.system('clear')  # Clear console (MacOS)

# --- Configuratie ---
SITE_ID = os.getenv("SOLAREDGE_SITE_ID")
API_KEY = os.getenv("SOLAREDGE_API_KEY")

# Check if API_KEY and SITE_ID are loaded from environment variables
if not API_KEY:
    print("⚠️ API_KEY not found in environment variables. Using hardcoded key.")
    API_KEY = "abcdykapoi"  # Replace with your actual hardcoded API key
if not SITE_ID:
    print("⚠️ SITE_ID not found in environment variables. Using hardcoded site ID.")
    SITE_ID = "1010709"  # Replace with your actual hardcoded site ID




INTERVAL = "QUARTER_OF_AN_HOUR"
BASE_URL = "https://monitoringapi.solaredge.com"

print("🌍 Welkom bij de SolarEdge Data Retrieval Tool!")




#%% --- Datumbereik instellen ---
start_date = datetime(2024, 1, 1)
end_date = datetime(2024, 12, 31)

# SolarEdge accepteert max. 1 maand per API-call, dus we splitsen het jaar op
dates = pd.date_range(start=start_date, end=end_date, freq='MS')

all_data = []

print("⏳ Data ophalen van SolarEdge API...")
for date in tqdm(dates):
    from_date = date.strftime("%Y-%m-%d")
    to_date = (date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d")

    time.sleep(1)  # Pauze om API-limieten te respecteren

    # API-aanroep voor energiegegevens
    url = f"{BASE_URL}/site/{SITE_ID}/energy.json"
    params = {
        "api_key": API_KEY,
        "timeUnit": INTERVAL,
        "startDate": from_date,
        "endDate": to_date
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code != 200:
        print(f"⚠️ Fout bij {from_date} t/m {to_date}: {response.text}")
        print(f"⚠️ Retrying in 1 seconds...")
        time.sleep(1)  # Wait for 1 second before retrying
        response = requests.get(url, params=params)  # Retry the request
        if response.status_code != 200:
            print(f"⚠️ Fout bij {from_date} t/m {to_date}: {response.text} na herhaalde poging. Overslaan.")
            continue

    energy_data = response.json().get("energy", {}).get("values", [])
    for entry in energy_data:
        if entry['value'] is not None:
            all_data.append({
                "datetime": entry["date"],
                "energy_wh": entry["value"]
            })

# --- DataFrame maken ---
df = pd.DataFrame(all_data)
df['datetime'] = pd.to_datetime(df['datetime'])
df = df.sort_values("datetime")







#%% Ensure all months are present in the DataFrame
expected_months = pd.date_range(start=start_date, end=end_date, freq='MS')
present_months = df['datetime'].dt.to_period('M').unique()

missing_months = set(expected_months.to_period('M')) - set(present_months)

if missing_months:
    print("⚠️ Waarschuwing: De volgende maanden ontbreken in de data:")
    for month in missing_months:
        print(month)
else:
    print("✅ Alle maanden zijn aanwezig in de data.")




#%%Filter out rows where 'energy_wh' is 0
df_small = df.copy()
df_small = df_small[df_small['energy_wh'] != 0]


df_small.to_excel("solaredge_pv_energy_2024_small.xlsx", index=False)
print("✅ Klaar! Gegevens opgeslagen in 'solaredge_pv_energy_2024_small.xlsx'")




#%% --- DataFrame aanvullen ---
if 1 == 1:
    # Creëer een volledige reeks datums
    full_date_range = pd.date_range(start=start_date, end=end_date, freq='15min')

    # Converteer de bestaande DataFrame index naar een DateTimeIndex
    df = df.set_index('datetime')
    df.index = pd.to_datetime(df.index)

    # Reindexeer de DataFrame om alle datums op te nemen
    df = df.reindex(full_date_range, fill_value=0)

    # Reset de index om 'datetime' weer een kolom te maken
    df = df.reset_index()
    df.rename(columns={'index': 'datetime'}, inplace=True)


# Bereken 'power' kolom
df['power_kW'] = df['energy_wh'] * 4 / 1000
df['power_kW'] = df['power_kW'].round(2)


# Opslaan naar Excel
df.to_excel("solaredge_pv_energy_2024_large.xlsx", index=False)
print("✅ Klaar! Gegevens opgeslagen in 'solaredge_pv_energy_2024_large.xlsx'")




