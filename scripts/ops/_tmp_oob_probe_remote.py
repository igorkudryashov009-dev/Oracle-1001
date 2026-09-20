import traceback

try:
    from services.healthcheck import attach_oob_plane, build_oob_health_overlay

    print("overlay", build_oob_health_overlay())
except Exception:
    traceback.print_exc()

try:
    from services.config_keys import registry_status

    st = registry_status()
    print("registry", st.get("configured"), st.get("total"))
except Exception:
    traceback.print_exc()

try:
    from services.ais_health import build_health_document

    d = build_health_document()
    print("oob_in_doc", d.get("oob"))
except Exception:
    traceback.print_exc()
