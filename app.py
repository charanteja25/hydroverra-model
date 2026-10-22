"""HydroVerra sizing and supplier comparison. Run: uv run streamlit run app.py"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from model import (ASSUMPTIONS, SPEC, SUPPLIERS_CSV, Design, apply_supplier, autonomy_test,
                   compare_suppliers, run, scenario_row, spec_check, sweep, tank_stats)
from report import build_excel
from weather import ABERDEEN, load_weather

st.set_page_config(page_title="HydroVerra model", layout="wide")
st.title("HydroVerra: dispatch and supplier model")
st.caption("Hourly energy balance with oemof-solph. Steady state, no degradation yet.")

CATS = ["pv", "wind", "electrolyser", "fuel_cell", "tank"]
SPEC_NAME = "Client spec (RFQ)"

if "suppliers" not in st.session_state:
    st.session_state.suppliers = pd.read_csv(SUPPLIERS_CSV)
    st.session_state.suppliers_base = st.session_state.suppliers.copy()  # input of the editor
    st.session_state.sup_ver = 0  # new version = fresh editor state
sup: pd.DataFrame = st.session_state.suppliers


def apply_preset():
    """Fill sliders and supplier choices from the client spec."""
    if st.session_state.get("preset") == SPEC_NAME:
        st.session_state.update(
            el_kw=SPEC["el_kw"], tank_kg=SPEC["tank_kg"], fc_kw=SPEC["fc_nameplate_kw"],
            load_kw=SPEC["fc_rated_kw"], sup_electrolyser=SPEC_NAME,
            sup_fuel_cell=SPEC_NAME, sup_tank=SPEC_NAME)


if "init" not in st.session_state:
    st.session_state.update(init=True, preset=SPEC_NAME, pv_kw=300.0, wind_kw=300.0,
                            use_pv=True, use_wind=True)
    apply_preset()


@st.cache_data(show_spinner="Loading weather...")
def get_weather(lat, lon, year):
    return load_weather(lat, lon, year)


def pick(cat: str):
    names = ["(defaults)"] + sup[sup.category == cat].supplier.tolist()
    if st.session_state.get(f"sup_{cat}") not in names:
        st.session_state[f"sup_{cat}"] = "(defaults)"
    choice = st.sidebar.selectbox(f"{cat} supplier", names, key=f"sup_{cat}")
    if choice == "(defaults)":
        return None
    return sup[(sup.category == cat) & (sup.supplier == choice)].iloc[0]


# ---------------- Sidebar ----------------
st.sidebar.selectbox("Preset", [SPEC_NAME, "Manual"], key="preset", on_change=apply_preset,
                     help="Client spec fills the sizes and the electrolyser, fuel cell and tank rows.")
st.sidebar.header("Site and period")
lat = st.sidebar.number_input("Latitude", value=ABERDEEN[0], format="%.2f")
lon = st.sidebar.number_input("Longitude", value=ABERDEEN[1], format="%.2f")
year = st.sidebar.number_input("Weather year", 2015, 2024, 2023)
upload = st.sidebar.file_uploader("Or upload weather CSV (time, ghi_wm2, wind_ms)", type="csv")
if upload is not None:
    wx_all, src = pd.read_csv(upload, index_col=0, parse_dates=True), "uploaded file"
else:
    wx_all, src = get_weather(lat, lon, int(year))
start = st.sidebar.date_input("Start date", value=pd.Timestamp(f"{int(year)}-01-10"))
days = st.sidebar.slider("Days", 1, 90, 7)

st.sidebar.header("Design (sliders)")
use_pv = st.sidebar.checkbox("Solar PV", key="use_pv")
use_wind = st.sidebar.checkbox("Wind turbine", key="use_wind")
pv_kw = st.sidebar.slider("PV size (kWp)", 0.0, 3000.0, step=10.0, key="pv_kw", disabled=not use_pv)
wind_kw = st.sidebar.slider("Wind size (kW)", 0.0, 3000.0, step=10.0, key="wind_kw", disabled=not use_wind)
el_kw = st.sidebar.slider("Electrolyser (kW in)", 10.0, 800.0, step=0.1, key="el_kw")
tank_kg = st.sidebar.slider("H2 tank (kg)", 1.0, 600.0, step=0.1, key="tank_kg")
fc_kw = st.sidebar.slider("Fuel cell (kW out)", 0.0, 400.0, step=0.1, key="fc_kw")
load_kw = st.sidebar.slider("Load (kW; day level if day/night)", 1.0, 400.0, step=0.1, key="load_kw")

st.sidebar.header("Load profile")
load_mode = st.sidebar.radio("Load shape", ["Flat", "Day / night", "Upload CSV"], key="load_mode")
night_kw = st.sidebar.slider("Night load (kW, 22:00-06:00 UTC)", 0.0, 400.0, 80.0, 0.1,
                             disabled=load_mode != "Day / night")
load_file = st.sidebar.file_uploader("Hourly load CSV (one column, kW)", type="csv",
                                     disabled=load_mode != "Upload CSV")

st.sidebar.header("Suppliers")
use_sizes = st.sidebar.checkbox("Use supplier's own size when the table has it", value=False)
chosen = [r for r in (pick(c) for c in CATS) if r is not None]
design = Design(pv_kw=pv_kw if use_pv else 0, wind_kw=wind_kw if use_wind else 0,
                el_kw=el_kw, tank_kg=tank_kg, fc_kw=fc_kw, load_kw=load_kw)
for row in chosen:
    design = apply_supplier(design, row)
    if use_sizes:
        from dataclasses import replace
        if row.category == "electrolyser" and pd.notna(row.rated_kw):
            design = replace(design, el_kw=float(row.rated_kw))
        if row.category == "fuel_cell" and pd.notna(row.rated_kw):
            design = replace(design, fc_kw=float(row.rated_kw))
        if row.category == "tank" and pd.notna(row.capacity_kg):
            design = replace(design, tank_kg=float(row.capacity_kg))

t0 = pd.Timestamp(start, tz="UTC")
wx = wx_all.loc[t0: t0 + pd.Timedelta(days=days) - pd.Timedelta(hours=1)]

def make_load(index):
    """Hourly load array for the period, or None for a flat load."""
    if load_mode == "Day / night":
        return np.where((index.hour >= 6) & (index.hour < 22), load_kw, night_kw)
    if load_mode == "Upload CSV" and load_file is not None:
        vals = pd.to_numeric(pd.read_csv(load_file, header=None).iloc[:, -1], errors="coerce").dropna()
        return np.resize(vals.to_numpy(float), len(index))  # repeats or trims to the period
    return None


load_arr = make_load(wx.index)
period_label = f"{start} + {days} d"
names = {r["category"]: r["supplier"] for r in chosen}

if "SYNTHETIC" in src:
    st.error(f"Weather is SYNTHETIC ({src}). Results are NOT Aberdeen results. Check internet access.")
else:
    st.caption(f"Weather source: {src}. Hours in period: {len(wx)}")
if any(str(r.get("status")) in ("PLACEHOLDER", "SPEC") for r in chosen) or not chosen:
    st.warning("Supplier values are PLACEHOLDERS, SPEC, or defaults. Do not use these results as quotes.")
if len(wx) < 24:
    st.stop()

tab_run, tab_scen, tab_auto, tab_spec, tab_cmp, tab_sweep, tab_sup, tab_about = st.tabs(
    ["Result", "Scenarios", "Autonomy test", "Spec check", "Compare suppliers", "Sweep a size",
     "Supplier table", "Assumptions"])

with tab_scen:
    sc = pd.DataFrame(st.session_state.get("scen", []))
    if sc.empty:
        st.info("No saved runs yet. Press Solve, then 'Save this run'.")
    else:
        st.dataframe(sc, hide_index=True, width="stretch")
        st.download_button("Download scenarios (CSV)", sc.to_csv(index=False).encode(),
                           "scenarios.csv", key="dl_scen")
        if st.button("Clear scenarios", key="clear_scen"):
            st.session_state.scen = []
            st.rerun()
        st.caption("Scenarios live only in this browser session. Download them before you close the page.")

with tab_run:
    if st.button("Solve", key="solve_btn", type="primary"):
        st.session_state.last = run(design, wx, load=load_arr)
        st.session_state.last_ctx = (design, period_label, src, load_mode, dict(names))
    out = st.session_state.get("last")
    if out is None:
        st.info("Set the sliders and press Solve.")
    else:
        s, df = out["summary"], out["df"]
        d_run, per, wsrc, lmode, nms = st.session_state.last_ctx
        tk = tank_stats(df, d_run.tank_kg)
        c = st.columns(5)
        c[0].metric("Load served", f"{s['served_pct']:.1f} %")
        c[1].metric("Unserved", f"{s['unserved_kwh']:.0f} kWh")
        c[2].metric("H2 produced", f"{s['h2_kg']:.1f} kg")
        c[3].metric("Curtailed", f"{s['curtailed_kwh']:.0f} kWh")
        c[4].metric("Capex (table units)", f"{s['capex']:,.0f}")
        st.caption(f"Electrolyser starts: {s['electrolyser_starts']} | solve time {s['solve_seconds']} s")

        st.subheader("Hydrogen tank")
        t = st.columns(5)
        t[0].metric("Minimum", f"{tk['min_kg']:.1f} kg", f"{tk['min_pct']:.0f} % of tank", delta_color="off")
        t[1].metric("Maximum", f"{tk['max_kg']:.1f} kg", f"{tk['max_pct']:.0f} % of tank", delta_color="off")
        t[2].metric("Start", f"{tk['start_kg']:.1f} kg")
        t[3].metric("End", f"{tk['end_kg']:.1f} kg")
        t[4].metric("Hours empty / full", f"{tk['hours_empty']} / {tk['hours_full']}")
        st.caption(f"Lowest at {tk['min_at'][:16]} UTC | highest at {tk['max_at'][:16]} UTC")
        if tk["hours_empty"] > 0:
            st.error(f"Tank is empty for {tk['hours_empty']} h. The fuel cell cannot run in those hours.")
        elif tk["hours_full"] > 24:
            st.warning(f"Tank is full for {tk['hours_full']} h. The electrolyser may curtail surplus power.")

        st.subheader("Power (kW)")
        pcols = ["pv_kw", "wind_kw", "load_kw", "electrolyser_kw", "fuel_cell_kw", "shortage_kw"]
        st.line_chart(df[pcols])
        with st.expander("Show values behind this graph"):
            st.dataframe(df[pcols].round(2), width="stretch")
            st.download_button("Download table (CSV)", df[pcols].round(3).to_csv().encode(),
                               "power_kw.csv", key="dl_power")
        with st.expander("Show tank level table (kg)"):
            st.dataframe(df[["tank_kg"]].clip(lower=0).round(2), width="stretch")

        st.subheader("Save and export")
        sc1, sc2 = st.columns([2, 1])
        scen_name = sc1.text_input("Scenario name", value=f"Run {len(st.session_state.get('scen', [])) + 1}")
        if sc2.button("Save this run", key="save_scen"):
            st.session_state.setdefault("scen", []).append(
                scenario_row(scen_name, d_run, s, tk, nms, per, wsrc, lmode))
            st.success(f"Saved '{scen_name}'. See the Scenarios tab.")
        scen_df = pd.DataFrame(st.session_state.get("scen", []))
        st.download_button(
            "Download Excel report", build_excel(d_run, s, tk, df, spec_check(sup), scen_df, wsrc),
            "hydroverra_report.xlsx", key="dl_xlsx")
        st.download_button("Download hourly results (CSV)", df.to_csv().encode(), "hydroverra_results.csv")

with tab_auto:
    st.write("No generation for the outage, then constant generation for the refill. "
             "The tank starts full and must end full. Unserved energy shows the gap.")
    a1, a2, a3 = st.columns(3)
    out_h = a1.number_input("Outage (h)", 1, 72, SPEC["autonomy_h"])
    ref_h = a2.number_input("Refill window (h)", 12, 240, SPEC["refill_h"])
    gen = a3.number_input("Generation during refill (kW)", 10.0, 5000.0, 400.0)
    need = design.load_kw / design.fc_kwh_per_kg * out_h / ref_h
    st.caption(f"Refill rate needed: {need:.2f} kg/h = {need * design.el_kwh_per_kg:.0f} kW electrolyser "
               f"(without margin). Your electrolyser: {design.el_kw / design.el_kwh_per_kg:.2f} kg/h.")
    if st.button("Run autonomy test", key="auto_btn"):
        st.session_state.auto = autonomy_test(design, int(out_h), int(ref_h), gen)
    r = st.session_state.get("auto")
    if r:
        (st.success if r["passed"] else st.error)(
            f"{'PASS' if r['passed'] else 'FAIL'}: unserved {r['unserved_total_kwh']:.0f} kWh "
            f"| H2 made {r['h2_made_kg']:.1f} kg | lowest tank {r['min_tank_kg']:.1f} kg")
        st.line_chart(r["df"][["tank_kg"]])

with tab_spec:
    st.write("Each row of the supplier table against the client spec. NO DATA means the cell is empty.")
    st.dataframe(spec_check(sup), hide_index=True)

with tab_cmp:
    cat = st.selectbox("Category to compare", CATS)
    if st.button("Compare suppliers"):
        st.session_state.cmp = compare_suppliers(design, cat, sup, wx, load=load_arr)
    if "cmp" in st.session_state:
        t = st.session_state.cmp
        st.dataframe(t[["supplier", "status", "served_pct", "unserved_kwh", "h2_kg",
                        "curtailed_kwh", "electrolyser_starts", "capex"]].round(1), hide_index=True)
        st.bar_chart(t.set_index("supplier")[["unserved_kwh"]])

with tab_sweep:
    fld = st.selectbox("Size to sweep", ["pv_kw", "wind_kw", "tank_kg", "el_kw", "fc_kw"])
    lo, hi = st.columns(2)
    vmin = lo.number_input("From", value=0.0)
    vmax = hi.number_input("To", value=300.0)
    steps = st.slider("Steps", 3, 15, 7)
    if st.button("Run sweep"):
        vals = [vmin + i * (vmax - vmin) / (steps - 1) for i in range(steps)]
        st.session_state.sw = (fld, sweep(design, fld, vals, wx, load=load_arr))
    if "sw" in st.session_state:
        f, t = st.session_state.sw
        st.line_chart(t.set_index(f)[["unserved_kwh"]])
        st.dataframe(t[[f, "served_pct", "unserved_kwh", "h2_kg", "curtailed_kwh", "capex"]].round(1),
                     hide_index=True)
        ok = t[t.served_pct >= 99.99]
        if len(ok):
            st.success(f"Smallest tested {f} with 100 % served: {ok[f].min():.0f}")
        else:
            st.warning(f"No tested {f} reaches 100 % served. Change other sizes or the range.")

with tab_sup:
    st.write("Edit cells, or add a row at the bottom. To clear a cell: click it and press Delete. "
             "To remove a row: tick the box at the left of the row and press the bin icon, "
             "or use the buttons below. Set status to QUOTE or DATASHEET when the number is real.")
    edited = st.data_editor(
        st.session_state.suppliers_base, num_rows="dynamic",
        key=f"sup_editor_{st.session_state.sup_ver}")
    st.session_state.suppliers = edited.reset_index(drop=True)

    def reset_editor(new_base):
        st.session_state.suppliers_base = new_base.reset_index(drop=True)
        st.session_state.suppliers = st.session_state.suppliers_base.copy()
        st.session_state.sup_ver += 1

    c1, c2, c3 = st.columns(3)
    if c1.button("Save table to suppliers.csv", key="sup_save"):
        st.session_state.suppliers.to_csv(SUPPLIERS_CSV, index=False)
        reset_editor(st.session_state.suppliers)
        st.rerun()
    if c2.button("Reload from file (drop unsaved edits)", key="sup_reload"):
        reset_editor(pd.read_csv(SUPPLIERS_CSV))
        st.rerun()
    def row_label(i):
        r = st.session_state.suppliers.loc[i]
        return f"{i}: {r['category']} | {r['supplier']}"

    def delete_rows():  # runs before the widgets are drawn, so it may reset the multiselect
        drop = set(st.session_state.get("sup_drop", []))
        keep = [i for i in st.session_state.suppliers.index if row_label(i) not in drop]
        reset_editor(st.session_state.suppliers.loc[keep])
        st.session_state.sup_drop = []

    st.multiselect("Rows to delete", [row_label(i) for i in st.session_state.suppliers.index],
                   key="sup_drop")
    st.button("Delete selected rows", key="sup_delete", on_click=delete_rows)
    st.caption("Edits stay in this browser session until you press Save.")

with tab_about:
    st.markdown("\n".join(f"- {x}" for x in ASSUMPTIONS))
