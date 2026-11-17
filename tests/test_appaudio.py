from snappie import appaudio, cli

SINK = "snappie_apps_1"


def node(nid, **props):
    return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def port(pid, nid, direction, channel):
    return {"id": pid, "type": "PipeWire:Interface:Port",
            "info": {"props": {"node.id": nid, "port.direction": direction, "audio.channel": channel}}}


def link(lid, out_node, out_port, in_node, in_port):
    return {"id": lid, "type": "PipeWire:Interface:Link",
            "info": {"output-node-id": out_node, "output-port-id": out_port,
                     "input-node-id": in_node, "input-port-id": in_port}}


def graph(*extra):
    """Firefox (stereo), vesktop (Flatpak, stereo), a mono game, an EasyEffects output, our sink."""
    out = "Stream/Output/Audio"
    return [
        node(10, **{"media.class": out, "application.name": "Firefox", "node.name": "Firefox",
                    "application.process.binary": "firefox", "application.process.id": 500}),
        port(11, 10, "out", "FL"), port(12, 10, "out", "FR"),
        node(20, **{"media.class": out, "application.name": "vesktop", "node.name": "vesktop",
                    "application.process.binary": "vesktop.bin", "pipewire.access.portal.app_id": "dev.vencord.Vesktop"}),
        port(21, 20, "out", "FL"), port(22, 20, "out", "FR"),
        node(30, **{"media.class": out, "application.name": "Game", "application.process.binary": "game.exe"}),
        port(31, 30, "out", "MONO"),
        node(40, **{"media.class": out, "node.name": "ee_soe_output_level", "node.link-group": "ee_sink_group"}),
        port(41, 40, "out", "FL"),
        node(50, **{"media.class": "Stream/Output/Audio", "application.name": "snappie",
                    "application.process.id": 1}),   # our own start sound
        port(51, 50, "out", "FL"),
        node(90, **{"media.class": "Audio/Sink", "node.name": SINK}),
        port(91, 90, "in", "FL"), port(92, 90, "in", "FR"),
        *extra,
    ]


def linked_apps(d, sel):
    create, remove = appaudio.plan(d, SINK, sel, own_pid=1)
    return sorted({c[0] for c in create}), remove


def test_except_leaves_out_voice_chat_filters_and_ourselves():
    sel = appaudio.Selection("except", ["vesktop"])
    assert linked_apps(graph(), sel) == ([10, 30], [])


def test_only_matches_binary_and_flatpak_id_case_insensitively():
    assert linked_apps(graph(), appaudio.Selection("only", ["FIREFOX"]))[0] == [10]
    assert linked_apps(graph(), appaudio.Selection("only", ["game.exe"]))[0] == [30]
    assert linked_apps(graph(), appaudio.Selection("only", ["dev.vencord.vesktop"]))[0] == [20]
    assert linked_apps(graph(), appaudio.Selection("only", []))[0] == []


def test_channels_map_to_stereo():
    create, _ = appaudio.plan(graph(), SINK, appaudio.Selection("only", ["firefox", "game"]), own_pid=1)
    assert set(create) == {(10, 11, 90, 91), (10, 12, 90, 92), (30, 31, 90, 91), (30, 31, 90, 92)}
    assert appaudio.channel_targets("LFE") == ()
    assert appaudio.channel_targets("SL") == ("FL",)
    assert appaudio.channel_targets("RR") == ("FR",)


def test_existing_links_are_kept_and_unwanted_ones_removed():
    d = graph(link(100, 10, 11, 90, 91), link(101, 20, 21, 90, 91))
    create, remove = appaudio.plan(d, SINK, appaudio.Selection("except", ["vesktop"]), own_pid=1)
    assert (10, 11, 90, 91) not in create and (10, 12, 90, 92) in create
    assert remove == [101]


def test_ticks_override_the_rule():
    sel = appaudio.Selection("except", ["vesktop"])
    sel.set("Firefox", False)
    sel.set("vesktop", True)
    assert linked_apps(graph(), sel)[0] == [20, 30]
    assert appaudio.menu_items(graph(), sel, own_pid=1) == [("Firefox", False), ("Game", True), ("vesktop", True)]


def test_no_sink_yet():
    d = [o for o in graph() if o["id"] != 90]
    assert appaudio.plan(d, SINK, appaudio.Selection("only", ["firefox"])) is None


def test_apps_lists_real_apps_only():
    assert appaudio.apps(graph(), own_pid=1) == ["Firefox", "Game", "vesktop"]


def test_cli_app_options():
    ns = cli.parse(["record", "--app", "firefox", "--app", "game"])
    assert ns.app == ["firefox", "game"] and ns.exclude_app is None
    ns = cli.parse(["record", "--exclude-app", "vesktop"])
    assert ns.exclude_app == ["vesktop"]
    assert cli.absolute_paths(["record", "--app", "firefox"]) == ["record", "--app", "firefox"]


def test_cli_apps_override_settings(monkeypatch):
    from snappie import app, settings
    monkeypatch.setattr(settings, "get", lambda k: {"rec.audio_apps": "except",
                                                    "rec.audio_app_list": ["vesktop"]}.get(k))
    assert app._audio_apps(cli.parse(["record"])) == ("except", ["vesktop"])
    assert app._audio_apps(cli.parse(["record", "--app", "mpv"])) == ("only", ["mpv"])


def with_mic(*extra):
    meta = {"id": 2, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
            "metadata": [{"subject": 0, "key": "default.audio.source", "value": {"name": "headset_mic"}}]}
    return graph(meta, node(70, **{"media.class": "Audio/Source", "node.name": "headset_mic"}),
                 port(71, 70, "out", "MONO"), *extra)


def test_all_records_every_app():
    assert linked_apps(graph(), appaudio.Selection("all", ["vesktop"]))[0] == [10, 20, 30]


def test_mic_is_linked_only_when_on():
    sel = appaudio.Selection("only", [])
    assert appaudio.plan(with_mic(), SINK, sel, own_pid=1) == ([], [])
    sel.mic = True
    assert appaudio.plan(with_mic(), SINK, sel, own_pid=1)[0] == [(70, 71, 90, 91), (70, 71, 90, 92)]
    sel.mic = False   # muting removes its links
    d = with_mic(link(200, 70, 71, 90, 91), link(201, 70, 71, 90, 92))
    assert appaudio.plan(d, SINK, sel, own_pid=1) == ([], [200, 201])


def test_default_source_missing():
    assert appaudio.default_source(graph()) is None
    assert appaudio.default_source(with_mic()) == (70, {"MONO": 71})


def test_remembered_choices():
    ticks = {"vesktop": (False, {"vesktop", "dev.vencord.vesktop"}), "Firefox": (True, {"firefox"})}
    assert appaudio.remembered("all", [], ticks) == ("except", ["vesktop"])
    # turning an excluded app back on removes it, also when it was listed by its Flatpak id
    assert appaudio.remembered("except", ["dev.vencord.Vesktop", "Spotify"],
                               {"vesktop": (True, {"vesktop", "dev.vencord.vesktop"})}) == ("except", ["Spotify"])
    assert appaudio.remembered("except", ["vesktop"], {"vesktop": (True, {"vesktop"})}) == ("all", [])
    assert appaudio.remembered("only", ["Game"], ticks) == ("only", ["Game", "Firefox"])


def test_ticks_from_the_chooser():
    a = appaudio.AppAudio("all", [], mic=True, ticks={"vesktop": False})
    assert linked_apps(graph(), a.selection)[0] == [10, 30] and a.mic
