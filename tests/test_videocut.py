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
