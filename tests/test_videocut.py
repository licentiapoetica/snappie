from snappie.videocut import VideoCut


def test_split_remove_and_merge():
    v = VideoCut(10, 1920, 1080)
    assert v.segments() is None
    assert v.split(3) and v.split(6) and not v.split(6.01)        # too close to a split
    assert v.toggle(1)                                            # remove 3–6
    assert v.kept() == [(0, 3), (6, 10)] and v.kept_duration() == 7
    assert v.toggle(1) and v.segments() is None                   # back: neighbors merge


def test_keeps_at_least_one_piece():
    v = VideoCut(4, 100, 100)
    assert not v.toggle(0)
    v.split(2)
    assert v.toggle(0) and not v.toggle(1)


def test_trim_and_undo_redo():
    v = VideoCut(10, 100, 100)
    assert v.trim_start(2) and v.trim_end(8)
    assert v.kept() == [(2, 8)]
    assert not v.trim_start(9)                                    # would remove everything
    assert v.undo() and v.kept() == [(2, 10)]
    assert v.undo() and v.kept() == [(0, 10)] and not v.undo()
    assert v.redo() and v.kept() == [(2, 10)]
    v.split(5)                                                    # a new change drops the redo steps
    assert not v.redo()


def test_skip_to_jumps_over_removed_pieces():
    v = VideoCut(10, 100, 100)
    v.split(2), v.split(4), v.split(8)
    v.toggle(1), v.toggle(3)
    assert v.skip_to(1) is None
    assert v.skip_to(3) == 4
    assert v.skip_to(9) == 10                                     # nothing kept after: the end


def test_crop_is_clamped_even_and_undoable():
    v = VideoCut(5, 1920, 1080)
    assert v.set_crop((-10, 100, 1001, 5000))
    assert v.crop == (0, 0, 1000, 1080)
    assert not v.set_crop((0, 0, 1000, 1080))                     # unchanged
    assert v.set_crop((0, 0, 1920, 1080)) and v.crop is None      # the whole picture = no crop
    assert v.undo() and v.crop == (0, 0, 1000, 1080)
    assert v.aspect_crop(1.0) == (420, 0, 1080, 1080)
    assert v.aspect_crop(9 / 16) == (656, 0, 608, 1080)


def test_remove_a_dragged_range():
    v = VideoCut(10, 100, 100)
    assert v.set_range(6, 3, False)                               # either direction
    assert [p[:2] for p in v.pieces] == [[0, 3], [3, 6], [6, 10]]
    assert v.kept() == [(0, 3), (6, 10)]
    assert not v.set_range(3, 6, False)                           # already removed
    assert not v.range_kept(3, 6) and v.range_kept(2, 4)
    assert v.set_range(3, 6, True) and v.segments() is None       # restored
    assert v.undo() and v.kept() == [(0, 3), (6, 10)]


def test_range_across_splits_becomes_one_piece():
    v = VideoCut(10, 100, 100)
    v.split(2), v.split(4), v.split(8)
    v.toggle(1)                                                   # 2–4 removed
    assert v.set_range(3, 9, False)
    assert v.pieces == [[0, 2, True], [2, 3, False], [3, 9, False], [9, 10, True]]
    assert v.kept() == [(0, 2), (9, 10)]


def test_range_snaps_to_split_points_and_keeps_something():
    v = VideoCut(10, 100, 100)
    v.split(5)
    assert v.set_range(5.02, 7, False)                            # snapped to the split at 5
    assert v.pieces == [[0, 5, True], [5, 7, False], [7, 10, True]]
    assert not v.set_range(4, 4.03, False)                        # too short
    assert not v.set_range(0.01, 9.99, False)                     # snaps to the ends: nothing left
    assert v.set_range(-1, 4, False) and v.kept() == [(4, 5), (7, 10)]


def test_range_inside_a_removed_piece_changes_nothing():
    v = VideoCut(10, 100, 100)
    v.split(5), v.toggle(1)
    assert not v.set_range(6, 8, False)
    assert len(v.pieces) == 2


def test_segments_within_a_selection():
    v = VideoCut(10, 100, 100)
    assert v.segments(within=(6, 2)) == [(2, 6)]
    v.split(3), v.split(4), v.toggle(1)                           # 3–4 removed
    assert v.segments(within=(2, 6)) == [(2, 3), (4, 6)]
    assert v.segments(within=(4.02, 6)) == [(4, 6)]               # snapped to the split
    assert v.segments(within=(3, 4)) == []                        # only removed video
    assert v.segments(within=(0, 10)) == v.segments() == [(0, 3), (4, 10)]
