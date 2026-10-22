"""Excel report for the HydroVerra model."""
from __future__ import annotations

import io
from dataclasses import asdict

import pandas as pd

from model import ASSUMPTIONS, Design


def build_excel(design: Design, summary: dict, tank: dict, hourly: pd.DataFrame,
                spec_df: pd.DataFrame, scenarios: pd.DataFrame | None,
                weather_src: str) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        inputs = pd.DataFrame(asdict(design).items(), columns=["parameter", "value"])
        inputs.loc[len(inputs)] = ["weather_source", weather_src]
        inputs.to_excel(xw, sheet_name="Inputs", index=False)
        res = pd.DataFrame({**summary, **{f"tank_{k}": v for k, v in tank.items()}}.items(),
                           columns=["result", "value"])
        res.to_excel(xw, sheet_name="Results", index=False)
        spec_df.to_excel(xw, sheet_name="Spec check", index=False)
        if scenarios is not None and len(scenarios):
            scenarios.to_excel(xw, sheet_name="Scenarios", index=False)
        pd.DataFrame({"assumption": ASSUMPTIONS}).to_excel(xw, sheet_name="Assumptions", index=False)
        h = hourly.copy()
        h.index = h.index.tz_localize(None) if getattr(h.index, "tz", None) is not None else h.index
        h.round(3).to_excel(xw, sheet_name="Hourly")
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    60, max(12, max(len(str(c.value)) if c.value is not None else 0 for c in col[:50]) + 2))
    return buf.getvalue()
