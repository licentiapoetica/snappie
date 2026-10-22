from snappie.kwin import script_ids


def test_script_ids_from_introspection():
    xml = ('<node><interface name="org.kde.kwin.Scripting"/>'
           '<node name="Script1"/><node name="Script12"/><node name="Other3"/></node>')
    assert script_ids(xml) == {1, 12}
    assert script_ids("<node/>") == set()
