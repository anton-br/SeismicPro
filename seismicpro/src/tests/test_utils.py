""" Utilities test """

import pytest
import numpy as np


from seismicpro.src.utils import has_clips, get_clip_indicator, get_cliplen_indicator, get_maxabs_clips, has_maxabs_clips


CLIP_IND_PARAMS = [
    ([1, 2, 3, 3, 3, 4], 2, [0, 0, 1, 1, 0]),
    ([1, 2, 3, 3, 3, 4], 3, [0, 0, 1, 0]),
    ([1, 2, 3, 3, 3, 4], 4, [0, 0, 0]),
    ([1, 2, 3, 3, 3], 4, [0, 0]),
    ([1, 2, 3, 3, 3], 3, [0, 0, 1]),
    ([1, 1, 3, 3, 3], 2, [1, 0, 1, 1]),
    ([1, 1, 1], 2, [1, 1]),
    ([1, 1, 1, 1, 1], 3, [1, 1, 1]),
    (np.asarray([[1, 2, 2, 3]]).T, 2, np.asarray([[0, 1, 0],]).T),
    (np.asarray([[1, 2, 2, 3], [1, 1, 2, 3]]).T, 2, np.asarray([[0, 1, 0], [1, 0, 0]]).T),
]
@pytest.mark.parametrize("arr,clip_len,expected", CLIP_IND_PARAMS)
def test_clip_indicator(arr, clip_len, expected):
    assert np.allclose(get_clip_indicator(np.asarray(arr), clip_len).astype(int), np.asarray(expected))

@pytest.mark.parametrize("arr,clip_len", [([1, 2, 3], 1), ([1, 1, 1], 3)])
def test_clip_indicator_fails(arr, clip_len):
    with pytest.raises(ValueError, match="Incorrect `clip_len`"):
        get_clip_indicator(np.asarray(arr), clip_len)

HAS_CLIPS_PARAMS = [(np.arange(5), 3, False)]
HAS_CLIPS_PARAMS += [(np.concatenate([np.zeros(3), np.arange(5), np.zeros(3)]), lclip, False) for lclip in (3, 4)]
HAS_CLIPS_PARAMS += [([1, 2, 3, 3, 3, 4], lclip, res) for lclip, res in ((2, True), (3, True), (4, False))]
HAS_CLIPS_PARAMS += [([1, 2, 3, 3, 3], 2, False),
                     ([1, 2, 2, 3, 3], 2, True)]
@pytest.mark.parametrize("arr,clip_len,expected", HAS_CLIPS_PARAMS)
def test_has_clips(arr, clip_len, expected):
    assert has_clips(np.asarray(arr), clip_len) == expected

@pytest.mark.parametrize("arr", [np.zeros((10, 10))])
def test_has_clips_fails(arr):
    with pytest.raises(ValueError, match="Only 1-D traces are allowed"):
        has_clips(arr, 2)

CLIPLEN_IND_PARAMS = [
    ([1, 2, 3, 3, 3, 4], [0, 0, 1, 2, 0]),
    ([1, 1, 3, 3, 3], [1, 0, 1, 2]),
    ([1, 1, 1, 1, 1], [1, 2, 3, 4]),
    ([1, 2, 3, 3, 3, 4, 4, 4, 4], [0, 0, 1, 2, 0, 1, 2, 3]),
    ([1, 1, 1, 2, 1], [1, 2, 0, 0]),
    (np.asarray([[1, 2, 2, 3]]), np.asarray([[0, 1, 0],])),
    (np.asarray([[1, 2, 2, 3], [1, 1, 2, 3]]), np.asarray([[0, 1, 0], [1, 0, 0]])),
    (np.asarray([[ 0,  1,  0,  0], [ 0,  0,  0,  1]]) , np.asarray([[0, 0, 1], [1, 2, 0]])),
]
@pytest.mark.parametrize("arr,expected", CLIPLEN_IND_PARAMS)
def test_cliplen_indicator(arr, expected):
    assert np.allclose(get_cliplen_indicator(np.asarray(arr)).astype(int), np.asarray(expected))

MAXABS_CLIPS_PARAMS = [
    ([1, 2, 3, 3, 3, 4], [0, 0, 0, 0]),
    ([1, 2, 3, 3, 3, 2], [0, 0, 1, 0]),
    ([1, 2, 2, 3, 3, 2], [0, 0, 0, 0]),
    ([1, 2, -3, -3, -3, 2], [0, 0, 1, 0]),
    ([1, 2, 3, 3, -3, -3, 2], [0, 0, 0, 0, 0]),
    ([1, 1, 3, 3, 3], [0, 0, 1]),
    ([1, 1, 1, 1, 1], [1, 1, 1]),
    ([0, 0, 1, 1, 1], [0, 0, 1]),
    ([0, 0, 0, 1, 1], [0, 0, 0]),
    ([0, 0, 0, 0, -1], [0, 0, 0]),
    ([1, 2, 2, 2, -3, -3, -3], [0, 1, 0, 0, 1]),
    (np.asarray([[1, 2, 2, 2, 1]]), np.asarray([[0, 1, 0],])),
    (np.asarray([[1, 2, 2, 2, 1], [1, 2, 2, 2, 3]]), np.asarray([[0, 1, 0], [0, 0, 0]])),
]
@pytest.mark.parametrize("arr,expected", MAXABS_CLIPS_PARAMS)
def test_get_maxabs_clips(arr, expected):
    assert np.allclose(get_maxabs_clips(np.asarray(arr)).astype(int), np.asarray(expected))

HAS_MAXABS_CLIPS_PARAMS = [
    ([1, 2, 3, 3, 3, 4], False),
    ([1, 2, 3, 3, 3, 2], True),
    ([1, 2, 3, 3, 2, 2], False),
    (np.asarray([[1, 2, 2, 2, 1]]), np.asarray([True,])),
    (np.asarray([[1, 2, 2, 2, 1], [1, 2, 2, 2, 3]]), np.asarray([True, False])),
]
@pytest.mark.parametrize("arr,expected", HAS_MAXABS_CLIPS_PARAMS)
def test_has_maxabs_clips(arr, expected):
    assert np.all(has_maxabs_clips(np.asarray(arr)) == expected)