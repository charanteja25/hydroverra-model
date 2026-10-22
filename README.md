# HydroVerra model (v0.5)

    uv sync
    uv run streamlit run app.py        # sliders, supplier comparison, spec check, autonomy test
    uv run --with pytest python -m pytest -q test_model.py

- The app starts with the client spec (RFQ) preset. Choose "Manual" to move away from it.
- Add one row per supplier offer in suppliers.csv (or the "Supplier table" tab). Fill rated_kw, h2_kg_per_h, capacity_kg, kwh_per_kg.
- "Spec check" shows PASS / FAIL / NO DATA for each offer against the spec.
- "Autonomy test": 24 h outage, then 120 h refill. Tank starts full and must end full.
- First start downloads Aberdeen weather (Open-Meteo ERA5, no key) and caches it in data/.
  If the red "SYNTHETIC" banner shows, the download failed and results are NOT Aberdeen results.
- All supplier rows are PLACEHOLDER or SPEC until you replace them with quotes.
- Result tab: tank min/max numbers, a values table behind each graph, 'Save this run', Excel report.
- Scenarios tab: saved runs side by side. They live only in the browser session: download before closing.
- Load shape: flat, day/night, or an uploaded hourly CSV (one column, kW; repeated or trimmed to the period).
- Supplier table: edit cells, add rows, or delete rows with the buttons. Press Save to keep edits.
