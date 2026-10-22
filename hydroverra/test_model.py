import numpy as np
from dataclasses import replace
from model import Design, run, load_suppliers, apply_supplier, compare_suppliers, wind_cf
from weather import synthetic


def wx(days=7, start="2023-01-10"):
    w = synthetic(57.15, 2023)
    return w.loc[start:].iloc[: 24 * days]


def test_balance_and_limits():
    d = Design(pv_kw=150, wind_kw=50)
    out = run(d, wx())
    df, s = out["df"], out["summary"]
    bal = df.pv_kw + df.wind_kw + df.fuel_cell_kw + df.shortage_kw - df.load_kw - df.electrolyser_kw - df.curtail_kw
    assert bal.abs().max() < 1e-6
    on = df.electrolyser_kw[df.electrolyser_kw > 1e-6]
    assert on.empty or on.min() >= d.el_kw * d.el_min - 1e-6
    assert df.tank_kg.max() <= d.tank_kg + 1e-6
    kg_in = (df.electrolyser_kw / d.el_kwh_per_kg).sum()
    kg_out = (df.fuel_cell_kw / d.fc_kwh_per_kg).sum()
    assert abs(kg_in - kg_out) < 1e-3  # tank is balanced: start level = end level
    assert 0 <= s["served_pct"] <= 100


def test_no_generation_means_no_service():
    s = run(Design(pv_kw=0, wind_kw=0), wx(2))["summary"]
    assert abs(s["served_pct"]) < 1e-6


def test_wind_curve():
    cf = wind_cf([0, 3, 12, 20, 26], 3, 12, 25)
    assert cf[0] == 0 and cf[2] == 1 and cf[3] == 1 and cf[4] == 0


def test_supplier_compare():
    sup = load_suppliers()
    t = compare_suppliers(Design(pv_kw=150), "electrolyser", sup, wx(3))
    assert len(t) == 3 and (t[t.status == 'PLACEHOLDER'].capex > 0).all()


def test_parse_open_meteo():
    from weather import parse_open_meteo
    p = {"hourly": {"time": ["2023-01-01T00:00", "2023-01-01T01:00"],
                    "shortwave_radiation": [0, 5.0], "wind_speed_100m": [7.0, None],
                    "temperature_2m": [3.0, 3.1]}}
    df = parse_open_meteo(p)
    assert list(df.columns) == ["ghi_wm2", "wind_ms", "temp_c"] and df.wind_ms.iloc[1] == 7.0


def test_spec_rows_and_checks():
    from model import spec_check, SPEC
    t = spec_check(load_suppliers())
    spec_el = t[(t.supplier == "Client spec (RFQ)") & (t.category == "electrolyser")]
    # the spec itself fails its own refill window: 1.2 kg/h < 2.15 kg/h
    assert (spec_el[spec_el.item.str.startswith("H2 rate")].result == "FAIL").all()
    assert abs(SPEC["refill_kg_per_h"] - 2.148) < 0.01
    fc = t[(t.supplier == "Client spec (RFQ)") & (t.category == "fuel_cell")]
    assert (fc.result == "PASS").all()


def test_autonomy_test():
    from model import autonomy_test, SPEC
    base = Design(el_kw=70.2, el_kwh_per_kg=54.0, tank_kg=283.5, fc_kw=177.2,
                  fc_kwh_per_kg=15.0, load_kw=161.1)
    small = autonomy_test(base)
    big = autonomy_test(replace(base, el_kw=140.0))
    assert not small["passed"] and abs(small["unserved_total_kwh"] - 1526) < 30
    assert big["passed"]


def test_load_profile_changes_demand():
    import numpy as np
    w = wx(3)
    flat = run(Design(pv_kw=150), w)["summary"]
    prof = np.where((w.index.hour >= 6) & (w.index.hour < 22), 20.0, 10.0)
    var = run(Design(pv_kw=150), w, load=prof)["summary"]
    assert abs(var["demand_kwh"] - prof.sum()) < 1e-6 and var["demand_kwh"] < flat["demand_kwh"]


def test_tank_stats_and_excel():
    import io, openpyxl
    from model import tank_stats, scenario_row, spec_check
    from report import build_excel
    d = Design(pv_kw=150, wind_kw=50)
    out = run(d, wx(3))
    tk = tank_stats(out["df"], d.tank_kg)
    assert 0 <= tk["min_kg"] <= tk["max_kg"] <= d.tank_kg + 1e-6
    row = scenario_row("t", d, out["summary"], tk, {}, "p", "SYNTHETIC", "Flat")
    assert row["weather"] == "SYNTHETIC"
    import pandas as pd
    blob = build_excel(d, out["summary"], tk, out["df"], spec_check(load_suppliers()),
                       pd.DataFrame([row]), "SYNTHETIC")
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert {"Inputs", "Results", "Spec check", "Scenarios", "Assumptions", "Hourly"} <= set(wb.sheetnames)
