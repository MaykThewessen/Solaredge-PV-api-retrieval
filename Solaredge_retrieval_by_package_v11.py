#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Jan 07 2025

@author: mayk
"""

from datetime import datetime, timedelta
import pandas as pd
import solaredge
import pytz
import time

def get_energy_data_in_chunks(s, site_id, start_date, end_date):
    frames = []
    current_start = start_date
    
    while current_start < end_date:
        # Calculate chunk end (1 month from current start or end_date, whichever is earlier)
        current_end = min(  current_start + timedelta(days=31)  , end_date )
        
        try:
            chunk_df = s.get_energy_details_dataframe( site_id, current_start,  current_end,  time_unit='QUARTER_OF_AN_HOUR' )
            if not chunk_df.empty:  # Only append if the DataFrame is not empty
                frames.append(chunk_df)
                print(f"Successfully retrieved data from {current_start} to {current_end}")
            else:
                print(f"No data retrieved from {current_start} to {current_end}")
            time.sleep(0.05)
        except Exception as e:
            print(f"Error retrieving data for period {current_start} to {current_end}: {e}")
        
        current_start = current_end
        
    if frames:
        return pd.concat(frames)
    else:
        return pd.DataFrame()

# Main code

## importing the load_dotenv from the python-dotenv module
from dotenv import load_dotenv
load_dotenv()

# Setup Environment & # Load API Key & Configurations
import os
os.system('clear')  # Clear console (MacOS)

# --- Configuratie ---
SITE_ID = os.getenv("SOLAREDGE_SITE_ID")
API_KEY = os.getenv("SOLAREDGE_API_KEY")

s = solaredge.Solaredge(API_KEY)  # Replace with your actual API key
site_id = SITE_ID # replace with your actual site ID



# Get timezone and setup dates
timezone = s.get_timezone(site_id)
print(timezone)
#start_date = pytz.timezone(timezone).localize(datetime.strptime("2024-01-01 00:00", "%Y-%m-%d %H:%M"))
#end_date = pytz.timezone(timezone).localize(datetime.strptime("2024-03-03 03:00", "%Y-%m-%d %H:%M"))
start_date = datetime.strptime("2024-01-01 00:00", "%Y-%m-%d %H:%M")
end_date = datetime.strptime("2024-03-03 03:00", "%Y-%m-%d %H:%M")

#tz = pytz.timezone(timezone)
#start_date = tz.localize(datetime.strptime("2024-01-01 00:00", "%Y-%m-%d %H:%M"), ambiguous=False)
#end_date = tz.localize(datetime.strptime("2025-01-01 00:00", "%Y-%m-%d %H:%M"), ambiguous=False)

# Retrieve data in chunks
energy_prod_df = get_energy_data_in_chunks(s, site_id, start_date, end_date)
#print(energy_prod_df)



# Reorder columns: 'Production' first, followed by others sorted alphabetically
columns_order = ['Production'] + sorted([col for col in energy_prod_df.columns if col != 'Production'])
energy_prod_df = energy_prod_df.reindex(columns=columns_order)

print(energy_prod_df)


# Save the final dataframe to a CSV file
energy_prod_df.to_csv('SolarEdge_Kr12_2024_PV_date_15min_raw.csv', index=True)


# Convert the index column, called 'date' to timezone-unaware datetime objects
energy_prod_df.index = energy_prod_df.index.tz_localize(None)


# Save the final dataframe to an Excel file
# Format the start and end dates for the filename
start_date_str = start_date.strftime("%Y%m%d")
end_date_str = end_date.strftime("%Y%m%d")

# Save the final dataframe to an Excel file with start and end dates in the filename
energy_prod_df.to_excel(f'SolarEdge_Kr12_2024_PV_date_15min_{start_date_str}_to_{end_date_str}_Energy.xlsx', index=True)



# Add data completeness check
def check_data_completeness(df, start_date, end_date):
    # Calculate expected number of 15-minute intervals
    expected_intervals = int((end_date - start_date).total_seconds() / (15 * 60))
    actual_intervals = len(df)
    
    # Check if we have all expected intervals
    if actual_intervals == expected_intervals:
        print(f"\nData completeness check: PASSED")
        print(f"Expected intervals: {expected_intervals}")
        print(f"Actual intervals: {actual_intervals}")
    else:
        print(f"\nData completeness check: FAILED")
        print(f"Expected intervals: {expected_intervals}")
        print(f"Actual intervals: {actual_intervals}")
        print(f"Missing intervals: {expected_intervals - actual_intervals}")
        
        # Find missing timestamps
        expected_dates = pd.date_range(start=start_date, end=end_date, freq='15min')
        missing_dates = expected_dates[~expected_dates.isin(df.index)]
        if len(missing_dates) > 0:
            print("\nFirst 10 missing timestamps:")
            print(missing_dates[:10])

# Run the completeness check
check_data_completeness(energy_prod_df, start_date, end_date)






# Create a DataFrame with the start and end dates at 15-minute intervals
dates_df = pd.DataFrame({'Date': pd.date_range(start=start_date, end=end_date, freq='15min')})

# Concatenate the dates DataFrame with the energy production DataFrame
final_df = pd.concat([dates_df, energy_prod_df.reset_index(drop=True)], axis=1)

# Convert 'Date' column to timezone-unaware datetime objects
final_df['Date'] = final_df['Date'].dt.tz_localize(None)


print(final_df)





# Save the final dataframe to an Excel file with start and end dates in the filename
final_df.to_excel(f'SolarEdge_Kr12_{start_date_str}_to_{end_date_str}_15min_Final.xlsx', index=False)


