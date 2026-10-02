"""谱图处理的纯函数（driver/spectrum.py）：拉曼位移换算、排序、裁剪、合并像素、取整。"""
from __future__ import annotations

import math

import pytest

from driver.spectrum import SpectrumError, ascending, average, binned, coverage, crop, process, raman_shift


def test_raman_shift_is_zero_at_the_laser_line_and_grows_with_wavelength():
    shifts = raman_shift([785.0, 833.48, 844.18], 785.0)
    assert shifts[0] == pytest.approx(0, abs=1e-9)
    assert shifts[1] == pytest.approx(741, abs=0.5) and shifts[2] == pytest.approx(893, abs=0.5)
    with pytest.raises(SpectrumError, match="非正数"):
        raman_shift([0.0, 800.0], 785.0)


def test_descending_wavelengths_are_flipped_but_a_broken_calibration_is_refused():
    assert ascending([3.0, 2.0, 1.0], [30.0, 20.0, 10.0]) == ([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
    with pytest.raises(SpectrumError, match="不单调"):
        ascending([1.0, 3.0, 2.0], [1.0, 1.0, 1.0])
    with pytest.raises(SpectrumError, match="对不上"):
        ascending([1.0, 2.0], [1.0])


def test_process_crops_rounds_and_keeps_x_strictly_ascending():
    wavelengths = [790 + index * 0.05 for index in range(2000)]  # 790–890 nm，0.05 nm 一个像素
    counts = [100.0 + index / 3 for index in range(2000)]
    spectrum = process(wavelengths[::-1], counts[::-1], laser_nm=785, shift_range=(150, 1000), max_points=20000)
    x, y = spectrum["x"], spectrum["y"]
    assert 150 <= x[0] < 151 and 999 < x[-1] <= 1000
    assert all(b > a for a, b in zip(x, x[1:]))
    assert all(round(value, 2) == value for value in x) and all(round(value, 1) == value for value in y)
    assert y[0] < y[-1], "倒序的像素翻过来之后计数跟着走"


def test_crop_and_coverage():
    assert crop([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0], 2, 3) == ([2.0, 3.0], [20.0, 30.0])
    # 800 / 850 / 900 nm 在 785 nm 激发下是 239 / 974 / 1628 cm-1
    assert coverage([800.0, 850.0, 900.0], 785, 150, 2000) == 3
    assert coverage([800.0, 850.0, 900.0], 785, 1000, 2000) == 1


def test_binning_merges_neighbours():
    x = [float(index) for index in range(10)]
    y = [float(index % 2) for index in range(10)]
    assert binned(x, y, 5) == ([0.5, 2.5, 4.5, 6.5, 8.5], [0.5] * 5)
    assert binned(x, y, 10) == (x, y)
    merged_x, _ = binned(x, y, 4)  # 每 3 个一组，最后一组只剩 1 个
    assert merged_x == [1.0, 4.0, 7.0, 9.0]


def test_average_and_refusals():
    assert average([[1.0, 2.0], [3.0, 4.0]]) == [2.0, 3.0]
    with pytest.raises(SpectrumError, match="像素数不一样"):
        average([[1.0], [1.0, 2.0]])
    with pytest.raises(SpectrumError, match="只有 0 个像素"):
        process([800.0, 801.0], [1.0, 2.0], laser_nm=785, shift_range=(1000, 2000), max_points=100)
    with pytest.raises(SpectrumError, match="非有限"):
        process([800.0, 801.0], [1.0, math.nan], laser_nm=785, shift_range=(150, 2000), max_points=100)
