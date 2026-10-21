from snappie.a11y import match_frames, surface_of


def win(wid, caption, client, buffer=None, pid=1):
    """A KWin window dict as kwin.list_windows returns it (frame unused here)."""
    return {"id": wid, "pid": pid, "caption": caption, "client": list(client),
            "buffer": list(buffer or client), "frame": list(client)}


# -- which geometry the frame is relative to --

def test_server_side_decorations_use_the_surface():
    w = win("a", "A", (10, 40, 800, 600))
    assert surface_of(w, 800, 600) == "buffer"


def test_xwayland_client_inside_a_bigger_buffer():
    w = win("a", "A", client=(10, 40, 800, 600), buffer=(10, 10, 800, 630))
    assert surface_of(w, 800, 600) == "client"
    assert surface_of(w, 800, 630) == "buffer"


def test_csd_shadows_are_part_of_the_buffer():
    w = win("a", "A", client=(0, 0, 1080, 734), buffer=(-16, -10, 1112, 776))
    assert surface_of(w, 1112, 776) == "buffer"
    assert surface_of(w, 1080, 734) == "client"


def test_rounding_is_tolerated_but_other_sizes_are_not():
    w = win("a", "A", (0, 0, 1134, 1001))
    assert surface_of(w, 1133, 1002) == "buffer"
    assert surface_of(w, 1000, 1001) is None


# -- pairing windows of one process with its frames --

def test_same_titles_matched_by_size():
    ws = [win("s", "", (0, 0, 1618, 1001)), win("f", "", (0, 0, 290, 491)), win("g", "", (0, 0, 620, 532))]
    frames = [("", 620, 532), ("", 1618, 1001), ("", 290, 491)]
    assert match_frames(ws, frames) == {"s": (1, "buffer"), "f": (2, "buffer"), "g": (0, "buffer")}


def test_same_size_matched_by_title():
    ws = [win("1", "Projects — Konsole", (0, 0, 927, 416)), win("2", "veldin — Konsole", (0, 0, 927, 416))]
    frames = [("veldin — Konsole", 927, 416), ("Projects — Konsole", 927, 416)]
    assert match_frames(ws, frames) == {"1": (1, "buffer"), "2": (0, "buffer")}


def test_identical_windows_are_left_unmatched():
    ws = [win("1", "Home — Dolphin", (0, 0, 928, 481)), win("2", "Home — Dolphin", (500, 0, 928, 481))]
    frames = [("Home — Dolphin", 928, 481), ("Home — Dolphin", 928, 481)]
    assert match_frames(ws, frames) == {}


def test_a_stale_title_is_resolved_once_the_other_window_is_taken():
    ws = [win("1", "vim — Konsole", (0, 0, 927, 416)), win("2", "bash — Konsole", (0, 0, 927, 416))]
    frames = [("bash — Konsole", 927, 416), ("make — Konsole", 927, 416)]
    assert match_frames(ws, frames) == {"2": (0, "buffer"), "1": (1, "buffer")}


def test_a_title_alone_is_not_enough():
    ws = [win("1", "Settings", (0, 0, 1326, 983))]
    assert match_frames(ws, [("Settings", 640, 480)]) == {}


def test_one_frame_two_candidates_is_ambiguous():
    ws = [win("1", "", (0, 0, 800, 600)), win("2", "", (900, 0, 800, 600))]
    assert match_frames(ws, [("", 800, 600)]) == {}
