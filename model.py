"""HydroVerra dispatch model (oemof-solph). Hourly energy balance.

Sources: PV, wind. Chain: PEM electrolyser -> H2 tank -> fuel cell -> load.
No grid. All values come from the Design object or from suppliers.csv.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace, asdict
from pathlib import Path

import numpy as np
import pandas as pd
from oemof import solph

logging.getLogger("pyomo").setLevel(logging.ERROR)
SUPPLIERS_CSV = Path(__file__).parent / "suppliers.csv"


@dataclass(frozen=True)
class Design:
    # sizes (sliders)
    pv_kw: float = 100.0
    wind_kw: float = 0.0
    el_kw: float = 100.0
    tank_kg: float = 40.0
    fc_kw: float = 30.0
    load_kw: float = 20.0
    # supplier-derived values
    pv_pr: float = 0.80
    wind_cut_in: float = 3.0
    wind_rated_ms: float = 12.0
    wind_cut_out: float = 25.0
    el_kwh_per_kg: float = 55.0
    el_min: float = 0.10
    fc_kwh_per_kg: float = 16.0
    fc_min: float = 0.0
    # capex per unit (currency per kW, per kg)
    pv_capex: float = 0.0
    wind_capex: float = 0.0
    el_capex: float = 0.0
    fc_capex: float = 0.0
    tank_capex: float = 0.0
    # modelling choices
    el_startup_cost: float = 5.0
    el_min_uptime: int = 2
    shortage_cost: float = 1000.0
    tank_initial: float = 0.5  # share of tank full at start
    tank_balanced: bool = True  # True: end level = start level

    @property
    def capex(self) -> float:
        return (
            self.pv_kw * self.pv_capex
            + self.wind_kw * self.wind_capex
            + self.el_kw * self.el_capex
            + self.fc_kw * self.fc_capex
            + self.tank_kg * self.tank_capex
        )


def load_suppliers(path: Path | str = SUPPLIERS_CSV) -> pd.DataFrame:
    return pd.read_csv(path)


def apply_supplier(d: Design, row: pd.Series) -> Design:
    """Copy the values of one supplier row into the design."""
    c = row["category"]
    g = lambda k, default: default if pd.isna(row.get(k)) else float(row[k])
    if c == "pv":
        return replace(d, pv_pr=g("pr", d.pv_pr), pv_capex=g("capex_per_kw", 0))
    if c == "wind":
        return replace(
            d,
            wind_cut_in=g("cut_in_ms", d.wind_cut_in),
            wind_rated_ms=g("rated_ms", d.wind_rated_ms),
            wind_cut_out=g("cut_out_ms", d.wind_cut_out),
            wind_capex=g("capex_per_kw", 0),
        )
    if c == "electrolyser":
        return replace(
            d,
            el_kwh_per_kg=g("kwh_per_kg", d.el_kwh_per_kg),
            el_min=g("min_load", d.el_min),
            el_capex=g("capex_per_kw", 0),
        )
    if c == "fuel_cell":
        return replace(
            d,
            fc_kwh_per_kg=g("kwh_per_kg", d.fc_kwh_per_kg),
            fc_min=g("min_load", d.fc_min),
            fc_capex=g("capex_per_kw", 0),
        )
    if c == "tank":
        return replace(d, tank_capex=g("capex_per_kg", 0))
    raise ValueError(f"unknown category {c}")


def pv_cf(ghi_wm2: np.ndarray, pr: float) -> np.ndarray:
    """Capacity factor of PV from horizontal irradiance. Simple: no tilt model."""
    return np.clip(ghi_wm2 / 1000.0 * pr, 0, 1)


def wind_cf(ws, cut_in, rated, cut_out) -> np.ndarray:
    """Generic power curve: cubic between cut-in and rated. Replace with the real curve."""
    ws = np.asarray(ws, dtype=float)
    cf = np.where(
        ws < cut_in,
        0.0,
        np.where(
            ws < rated,
            (ws**3 - cut_in**3) / (rated**3 - cut_in**3),
            np.where(ws <= cut_out, 1.0, 0.0),
        ),
    )
    return np.clip(cf, 0, 1)


def run(d: Design, weather: pd.DataFrame, time_limit: int = 60, load=None) -> dict:
    """Solve one design on one weather slice. Returns dict with df and summary."""
    n = len(weather)
    idx = weather.index.tz_localize(None) if weather.index.tz is not None else weather.index
    pvf = pv_cf(weather["ghi_wm2"].to_numpy(), d.pv_pr)
    wdf = wind_cf(weather["wind_ms"].to_numpy(), d.wind_cut_in, d.wind_rated_ms, d.wind_cut_out)
    load = np.full(n, d.load_kw) if load is None else np.asarray(load, dtype=float)
    assert len(load) == n, 'load profile length must match the weather period'
    el_eff = 1.0 / d.el_kwh_per_kg  # kg H2 per kWh in
    fc_eff = d.fc_kwh_per_kg  # kWh out per kg H2

    es = solph.EnergySystem(timeindex=idx, infer_last_interval=True)
    b_el = solph.buses.Bus(label="el")
    b_h2 = solph.buses.Bus(label="h2")
    nodes = [b_el, b_h2]

    if d.pv_kw > 0:
        nodes.append(solph.components.Source(
            label="pv",
            outputs={b_el: solph.flows.Flow(nominal_capacity=d.pv_kw, maximum=pvf)}))
    if d.wind_kw > 0:
        nodes.append(solph.components.Source(
            label="wind",
            outputs={b_el: solph.flows.Flow(nominal_capacity=d.wind_kw, maximum=wdf)}))
    nodes += [
        solph.components.Sink(
            label="load", inputs={b_el: solph.flows.Flow(nominal_capacity=1, fix=load)}),
        solph.components.Sink(label="curtail", inputs={b_el: solph.flows.Flow()}),
        solph.components.Source(
            label="shortage",
            outputs={b_el: solph.flows.Flow(variable_costs=d.shortage_cost)}),
    ]
    if d.el_kw > 0 and d.tank_kg > 0:
        nodes.append(solph.components.Converter(
            label="electrolyser",
            inputs={b_el: solph.flows.Flow(
                nominal_capacity=d.el_kw, minimum=d.el_min, maximum=1,
                nonconvex=solph.NonConvex(
                    startup_costs=d.el_startup_cost, minimum_uptime=d.el_min_uptime))},
            outputs={b_h2: solph.flows.Flow()},
            conversion_factors={b_h2: el_eff}))
        fc_kwargs = dict(nominal_capacity=d.fc_kw)
        if d.fc_min > 0:
            fc_kwargs.update(minimum=d.fc_min, maximum=1, nonconvex=solph.NonConvex())
        if d.fc_kw > 0:
            nodes.append(solph.components.Converter(
                label="fuel_cell",
                inputs={b_h2: solph.flows.Flow()},
                outputs={b_el: solph.flows.Flow(**fc_kwargs)},
                conversion_factors={b_el: fc_eff}))
        nodes.append(solph.components.GenericStorage(
            label="h2_tank", nominal_capacity=d.tank_kg,
            inputs={b_h2: solph.flows.Flow()}, outputs={b_h2: solph.flows.Flow()},
            initial_storage_level=d.tank_initial, balanced=d.tank_balanced))
    es.add(*nodes)

    model = solph.Model(es)
    t0 = time.time()
    model.solve(solver="highs", cmdline_options={"time_limit": time_limit, "mip_rel_gap": 0.005})
    secs = time.time() - t0
    res = solph.processing.results(model)

    def flow(a, b):
        try:
            return solph.views.node(res, a)["sequences"][(a, b), "flow"]
        except KeyError:
            return pd.Series(0.0, index=idx)

    df = pd.DataFrame({
        "pv_kw": flow("pv", "el"), "wind_kw": flow("wind", "el"),
        "fuel_cell_kw": flow("fuel_cell", "el"), "shortage_kw": flow("shortage", "el"),
        "load_kw": flow("el", "load"), "electrolyser_kw": flow("el", "electrolyser"),
        "curtail_kw": flow("el", "curtail"),
    }).iloc[:-1]
    try:
        df["tank_kg"] = solph.views.node(res, "h2_tank")["sequences"][
            ("h2_tank", "None"), "storage_content"].iloc[:-1]
    except KeyError:
        df["tank_kg"] = 0.0
    on = (df.electrolyser_kw > 1e-6).astype(int)
    starts = int((on.diff().fillna(on.iloc[0]) > 0).sum())
    demand = float(load.sum())
    unserved = float(df.shortage_kw.sum())
    summary = {
        "hours": n,
        "demand_kwh": demand,
        "unserved_kwh": unserved,
        "served_pct": 100.0 * (1 - unserved / demand) if demand else 100.0,
        "h2_kg": float((df.electrolyser_kw * el_eff).sum()),
        "pv_kwh": float(df.pv_kw.sum()),
        "wind_kwh": float(df.wind_kw.sum()),
        "curtailed_kwh": float(df.curtail_kw.sum()),
        "electrolyser_starts": starts,
        "capex": d.capex,
        "solve_seconds": round(secs, 2),
    }
    return {"df": df, "summary": summary, "design": d}


def sweep(base: Design, field: str, values, weather: pd.DataFrame, load=None) -> pd.DataFrame:
    rows = []
    for v in values:
        s = run(replace(base, **{field: float(v)}), weather, load=load)["summary"]
        rows.append({field: v, **s})
    return pd.DataFrame(rows)


def compare_suppliers(base: Design, category: str, sup: pd.DataFrame, weather: pd.DataFrame, load=None) -> pd.DataFrame:
    rows = []
    for _, r in sup[sup.category == category].iterrows():
        s = run(apply_supplier(base, r), weather, load=load)["summary"]
        rows.append({"supplier": r["supplier"], "status": r.get("status", ""), **s})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- client spec
SPEC = dict(
    fc_nameplate_kw=177.2,  # includes 10 % design margin
    fc_rated_kw=161.1,  # 177.2 / 1.10 = load the fuel cell must carry
    fc_kg_per_h=10.74,
    fc_kwh_per_kg=15.0,  # derived: 161.1 / 10.74
    tank_kg=283.5,  # 24 h x 10.74 kg/h x 1.10
    autonomy_h=24,
    el_kw=70.2,
    el_kg_per_h=1.2,
    el_kwh_per_kg=54.0,
    refill_h=120,
    fc_dc_window_v=(450, 750),
    el_bus_v=800,
)
SPEC["refill_kg_per_h"] = SPEC["fc_kg_per_h"] * SPEC["autonomy_h"] / SPEC["refill_h"]  # 2.148


def spec_check(sup: pd.DataFrame) -> pd.DataFrame:
    """Compare each electrolyser, fuel cell and tank row with the client spec."""
    S = SPEC
    rows = []

    def add(cat, name, item, req, val, ok):
        res = "NO DATA" if val is None else ("PASS" if ok else "FAIL")
        rows.append({"category": cat, "supplier": name, "item": item,
                     "requirement": req, "offered": val, "result": res})

    num = lambda r, k: None if pd.isna(r.get(k)) else float(r[k])
    for _, r in sup.iterrows():
        c, n = r["category"], r["supplier"]
        if c == "electrolyser":
            kg_h = num(r, "h2_kg_per_h")
            kwh = num(r, "kwh_per_kg")
            if kg_h is None and num(r, "rated_kw") and kwh:
                kg_h = num(r, "rated_kw") / kwh
            add(c, n, "H2 rate (kg/h) for 120 h refill", f">= {S['refill_kg_per_h']:.2f}",
                None if kg_h is None else round(kg_h, 2), kg_h is not None and kg_h >= S["refill_kg_per_h"])
            add(c, n, "Specific energy (kWh/kg)", f"<= {S['el_kwh_per_kg']}", kwh,
                kwh is not None and kwh <= S["el_kwh_per_kg"])
            if num(r, "rated_kw") and num(r, "h2_kg_per_h"):
                implied = num(r, "rated_kw") / num(r, "h2_kg_per_h")
                add(c, n, "Own numbers agree: rated kW / kg/h", f"~ {kwh} kWh/kg",
                    round(implied, 1), kwh is not None and abs(implied - kwh) / kwh < 0.03)
        elif c == "fuel_cell":
            kw = num(r, "rated_kw")
            add(c, n, "Nameplate (kW)", f">= {S['fc_nameplate_kw']}", kw,
                kw is not None and kw >= S["fc_nameplate_kw"])
            kwh = num(r, "kwh_per_kg")
            add(c, n, "Electric out per kg H2 (kWh/kg)", f">= {S['fc_kwh_per_kg']}", kwh,
                kwh is not None and kwh >= S["fc_kwh_per_kg"])
        elif c == "tank":
            cap = num(r, "capacity_kg")
            add(c, n, "Usable H2 (kg)", f">= {S['tank_kg']}", cap,
                cap is not None and cap >= S["tank_kg"])
    return pd.DataFrame(rows)


def autonomy_test(d: Design, outage_h: int = 24, refill_h: int = 120,
                  gen_kw: float = 400.0, time_limit: int = 60) -> dict:
    """No generation for outage_h, then constant gen_kw for refill_h.
    The tank starts full and must end full. Unserved energy shows the gap."""
    n = outage_h + refill_h
    idx = pd.date_range("2023-01-10", periods=n, freq="h", tz="UTC")
    wx = pd.DataFrame({"ghi_wm2": 0.0, "wind_ms": np.where(np.arange(n) < outage_h, 0.0, 15.0),
                       "temp_c": 5.0}, index=idx)
    dd = replace(d, pv_kw=0.0, wind_kw=gen_kw, wind_cut_in=3.0, wind_rated_ms=12.0,
                 wind_cut_out=25.0, tank_initial=1.0, tank_balanced=True)
    out = run(dd, wx, time_limit)
    df, s = out["df"], out["summary"]
    return {
        "unserved_outage_kwh": float(df.shortage_kw.iloc[:outage_h].sum()),
        "unserved_total_kwh": s["unserved_kwh"],
        "min_tank_kg": float(df.tank_kg.min()),
        "h2_made_kg": s["h2_kg"],
        "passed": s["unserved_kwh"] < 1.0,
        "df": df,
    }


# ---------------------------------------------------------------- reporting
ASSUMPTIONS = [
    "Client spec (RFQ): fuel cell 177.2 kW nameplate (161.1 kW rated), 10.74 kg/h, tank 283.5 kg, electrolyser 70.2 kW, 54 kWh/kg, 120 h refill. The fuel cell 15.0 kWh/kg is derived. The electrolyser minimum load (10 %) is an assumption.",
    "The spec has two known conflicts: 70.2 kW / 1.2 kg/h is 58.5 kWh/kg, and 1.2 kg/h refills only 144 kg in 120 h. See the Spec check and Autonomy test tabs.",
    "PV: capacity factor = horizontal irradiance / 1000 x performance ratio. No tilt or temperature model.",
    "Wind: generic power curve from 100 m ERA5 wind. Replace with the real curve from the supplier.",
    "Electrolyser: minimum load, kWh per kg, start-up cost (placeholder 5), minimum uptime 2 h. Constant efficiency. No degradation, no warm-up.",
    "Tank: starts at 50 % and ends at 50 % in normal runs. No compressor energy, no losses.",
    "Fuel cell: constant efficiency. One-hour steps: ramp rate and response time are NOT tested.",
    "Shortage is allowed at a high cost, so the model shows unserved load instead of failing.",
    "Capex is size x supplier unit cost only. No installation, opex, or lifetime.",
]


def tank_stats(df: pd.DataFrame, tank_kg: float) -> dict:
    """Numbers that replace the tank graph."""
    t = df["tank_kg"].clip(lower=0.0)
    pct = lambda v: 100.0 * v / tank_kg if tank_kg else 0.0
    return {
        "start_kg": float(t.iloc[0]), "end_kg": float(t.iloc[-1]),
        "min_kg": float(t.min()), "max_kg": float(t.max()),
        "min_pct": pct(float(t.min())), "max_pct": pct(float(t.max())),
        "min_at": str(t.idxmin()), "max_at": str(t.idxmax()),
        "hours_empty": int((t <= 0.01).sum()),
        "hours_full": int((t >= tank_kg * 0.999).sum()),
    }


def scenario_row(name: str, d: Design, summary: dict, tank: dict, names: dict,
                 period: str, weather_src: str, load_mode: str) -> dict:
    return {
        "scenario": name, "period": period, "weather": weather_src, "load_mode": load_mode,
        "electrolyser": names.get("electrolyser", "(defaults)"),
        "fuel_cell": names.get("fuel_cell", "(defaults)"),
        "tank_supplier": names.get("tank", "(defaults)"),
        "pv_kw": d.pv_kw, "wind_kw": d.wind_kw, "el_kw": d.el_kw, "tank_kg": d.tank_kg,
        "fc_kw": d.fc_kw, "load_kw": d.load_kw,
        "served_pct": round(summary["served_pct"], 2), "unserved_kwh": round(summary["unserved_kwh"], 1),
        "h2_kg": round(summary["h2_kg"], 1), "curtailed_kwh": round(summary["curtailed_kwh"], 1),
        "starts": summary["electrolyser_starts"], "capex": round(summary["capex"]),
        "tank_min_kg": round(tank["min_kg"], 1), "tank_max_kg": round(tank["max_kg"], 1),
        "hours_tank_empty": tank["hours_empty"],
    }
