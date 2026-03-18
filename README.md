# solaredge
Script(s) to extract SolarEdge data via the API

Initially the first file is this Python script.
https://github.com/sfirke/solaredge/blob/main/solaredge_retrieval.py

New update since May 2025:

Now works without python solaredge package (due to issues: https://github.com/EnergieID/solaredge/issues/1)

Script still operates by retrieving 15min energy values in chunks of 31 days together and appends months into a year

Exports two dataframes; 
1) a small one where only positive energy row values are saved
2) a large one, where every datetime 15min value is named, even during night hours where energy produced per 15min value is 0 Wh.
