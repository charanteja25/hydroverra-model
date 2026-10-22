from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=180).run()
assert not at.exception, at.exception
at.button(key="solve_btn").click().run()
assert not at.exception, at.exception
labels = [m.label for m in at.metric]
print("tank metrics present:", all(l in labels for l in ["Minimum", "Maximum", "Start", "End", "Hours empty / full"]))
print("tank:", [(m.label, m.value) for m in at.metric if m.label in ("Minimum", "Maximum")])
print("expanders:", [e.label for e in at.expander])
at.button(key="save_scen").click().run()
assert not at.exception, at.exception
print("saved scenarios:", len(at.session_state["scen"]), "| weather col:", at.session_state["scen"][0]["weather"][:30])
at.radio(key="load_mode").set_value("Day / night").run()
at.button(key="solve_btn").click().run()
assert not at.exception, at.exception
print("day/night served:", [m.value for m in at.metric if m.label == "Load served"])
at.button(key="save_scen").click().run()
print("saved scenarios:", len(at.session_state["scen"]), [r["load_mode"] for r in at.session_state["scen"]])
at.button(key="auto_btn").click().run()
assert not at.exception, at.exception
print("autonomy ok")
