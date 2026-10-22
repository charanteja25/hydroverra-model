"""HydroVerra v1: off-grid dispatch model (PLACEHOLDER VALUES, not HydroVerra data)."""
import numpy as np
import pandas as pd
from oemof import solph

# ---- 1. Placeholder inputs (replace with real data) -------------------------
N_HOURS = 24 * 7
idx = pd.date_range("2026-06-01", periods=N_HOURS, freq="h")
hours = np.arange(N_HOURS)

# Synthetic PV profile (capacity factor 0..1). Replace with a real profile.
pv_cf = np.clip(np.sin((hours % 24 - 6) / 12 * np.pi), 0, None) ** 1.5
# Synthetic flat load in kW. Replace with the real load.
load_kw = np.full(N_HOURS, 20.0)

PV_KW = 150.0  # PV size
EL_KW = 100.0  # electrolyser input power
EL_MIN = 0.20  # minimum load, share of nominal
EL_KWH_PER_KG = 55.0  # kWh electricity per kg H2
TANK_KG = 40.0  # H2 storage
FC_KW = 30.0  # fuel cell electric output
FC_KWH_PER_KG = 16.0  # kWh electricity per kg H2 (about 48 percent)
LHV_NOTE = "kWh per kg are placeholder values"

# H2 is modelled in kg. Electrolyser: 1 kWh in -> 1/EL_KWH_PER_KG kg out.
el_eff = 1.0 / EL_KWH_PER_KG  # kg per kWh
fc_eff = FC_KWH_PER_KG  # kWh per kg

# ---- 2. Energy system -------------------------------------------------------
es = solph.EnergySystem(timeindex=idx, infer_last_interval=True)

b_el = solph.buses.Bus(label="el")
b_h2 = solph.buses.Bus(label="h2")

pv = solph.components.Source(
    label="pv",
    outputs={b_el: solph.flows.Flow(
        nominal_capacity=PV_KW, maximum=pv_cf, variable_costs=0)},
)
load = solph.components.Sink(
    label="load",
    inputs={b_el: solph.flows.Flow(nominal_capacity=1, fix=load_kw)},
)
# Excess power (curtailment) is allowed. Unserved load is allowed but costs a lot,
# so the model shows a shortage instead of failing.
curtail = solph.components.Sink(
    label="curtail", inputs={b_el: solph.flows.Flow()})
shortage = solph.components.Source(
    label="shortage",
    outputs={b_el: solph.flows.Flow(variable_costs=1000)},
)

electrolyser = solph.components.Converter(
    label="electrolyser",
    inputs={b_el: solph.flows.Flow(
        nominal_capacity=EL_KW,
        minimum=EL_MIN,
        maximum=1,
        nonconvex=solph.NonConvex(startup_costs=5, minimum_uptime=2),
    )},
    outputs={b_h2: solph.flows.Flow()},
    conversion_factors={b_h2: el_eff},
)
fuel_cell = solph.components.Converter(
    label="fuel_cell",
    inputs={b_h2: solph.flows.Flow()},
    outputs={b_el: solph.flows.Flow(nominal_capacity=FC_KW)},
    conversion_factors={b_el: fc_eff},
)
tank = solph.components.GenericStorage(
    label="h2_tank",
    nominal_capacity=TANK_KG,
    inputs={b_h2: solph.flows.Flow()},
    outputs={b_h2: solph.flows.Flow()},
    initial_storage_level=0.5,
    balanced=True,  # end level = start level (cyclic storage)
    loss_rate=0.0,
)

es.add(b_el, b_h2, pv, load, curtail, shortage, electrolyser, fuel_cell, tank)

# ---- 3. Solve ---------------------------------------------------------------
model = solph.Model(es)
model.solve(solver="highs", solve_kwargs={"tee": False})

# ---- 4. Results -------------------------------------------------------------
res = solph.processing.results(model)
get = lambda a, b: solph.views.node(res, a)["sequences"][(a, b), "flow"]
df = pd.DataFrame({
    "pv_kw": get("pv", "el"),
    "load_kw": get("el", "load"),
    "electrolyser_kw": get("el", "electrolyser"),
    "fuel_cell_kw": get("fuel_cell", "el"),
    "curtail_kw": get("el", "curtail"),
    "shortage_kw": get("shortage", "el"),
}).iloc[:-1]
df["tank_kg"] = solph.views.node(res, "h2_tank")["sequences"][
    ("h2_tank", "None"), "storage_content"].iloc[:-1]
print(df.describe().loc[["mean", "max"]].round(1))
print("Unserved energy (kWh):", round(df.shortage_kw.sum(), 2))
print("H2 produced (kg):", round((df.electrolyser_kw * el_eff).sum(), 2))
print("Objective:", round(model.objective(), 2))
df.to_csv("hydroverra_v1_results.csv")