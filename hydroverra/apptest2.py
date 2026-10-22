from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=180).run()
assert not at.exception, at.exception
n0 = len(at.session_state["suppliers"])
print("rows at start:", n0, "| editor version:", at.session_state["sup_ver"])
labels = [o for o in at.multiselect(key="sup_drop").options if "PV Supplier B" in o]
at.multiselect(key="sup_drop").set_value(labels).run()
at.button(key="sup_delete").click().run()
assert not at.exception, at.exception
sup = at.session_state["suppliers"]
print("rows after delete:", len(sup), "| PV Supplier B still there:", "PV Supplier B" in sup.supplier.tolist(),
      "| version:", at.session_state["sup_ver"])
print("dropdown options:", at.selectbox(key="sup_pv").options)
# a second rerun must keep the deletion (this was the bug)
at.run()
print("rows after extra rerun:", len(at.session_state["suppliers"]))
at.button(key="sup_reload").click().run()
print("rows after reload:", len(at.session_state["suppliers"]))
