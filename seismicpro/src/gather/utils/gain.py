"""Implements functions for gather gain amplifications"""

import numpy as np
from numba import njit, prange

@njit(nogil=True)
def process_amp(amp, mode):
    """Process trace amplitude to use in AGC coefficient calculation."""
    if np.isnan(amp):
        amp = 0
    non_zero = 1 if amp != 0 else 0
    amp = amp**2 if mode=='rms' else abs(amp)
    return amp, non_zero


@njit(nogil=True, parallel=True)
def apply_agc(data, window_size=125, mode='rms'):
    """Calculate instantaneous of RMS amplitude AGC coefficients and apply them to gather data.

    Parameters
    ----------
    data : 2d np.ndarray
        Gather data to apply AGC to.
    window_size : int
        Window size to calculate AGC scaling coefficient in, measured in samples. Defaults to 125.
    mode : str
        Mode for AGC: if 'rms', root mean squared value of non-zero amplitudes in the given window is used as scaling
        coefficient(RMS amplitude AGC), if 'abs' - mean of absolute non-zero amplitudes (instantaneous AGC).
        Defaults to 'rms'.

    Returns
    -------
    data : 2d array
        Gather data with applied AGC.
    """
    n_traces, trace_len = data.shape
    win_left, win_right = window_size // 2, window_size - window_size // 2
    # start is the first trace index that fits the full window, end is the last.
    # AGC coefficients before start and after end are extrapolated.
    start, end = win_left, trace_len - win_right

    for i in prange(n_traces):  # pylint: disable=not-an-iterable
        trace = data[i]
        coefs = np.empty_like(trace)

        # Calculate AGC scaling factor for the first window
        win_sum = np.float64(0)
        win_count = 0
        for j in range(window_size):
            amp, non_zero = process_amp(trace[j], mode)
            win_count += non_zero
            win_sum += amp
        coefs[start] = win_count / (win_sum + 1e-15)
        if mode == 'rms':
            coefs[start] = np.sqrt(coefs[start])

        # Move the window by one trace element and recalculate the AGC factor
        for j in range(start + 1, end):
            amp, non_zero = process_amp(trace[j + win_right - 1], mode)
            win_count += non_zero
            win_sum += amp

            amp, non_zero = process_amp(trace[j - win_left - 1], mode)
            win_count -= non_zero
            win_sum -= amp

            coefs[j] = win_count / (win_sum + 1e-15)
            if mode == 'rms':
                coefs[j] = np.sqrt(coefs[j])

        # Extrapolate AGC coefs for trace indices that don't fit the full window
        coefs[:start] = coefs[start]
        coefs[end:] = coefs[end-1]

        data[i] *= coefs
    return data


@njit(nogil=True, parallel=True)
def calculate_sdc_coefficient(v_pow, velocities, t_pow, times):
    """Calculate spherical divergence correction coefficients."""
    sdc_coefficient = velocities**v_pow * times**t_pow
    # Scale sdc_coefficient to be 1 at maximum time
    sdc_coefficient /= sdc_coefficient[-1]
    return sdc_coefficient


@njit(nogil=True, parallel=True)
def apply_sdc(data, v_pow, velocities, t_pow, times):
    """Calculate spherical divergence correction coefficients and apply them to gather data.

    SDC coefficients are a function of time and velocity:
    .. math::
        g(t) ~ velocities^{v_{pow}} * times^{t_{pow}}}

    Parameters
    ----------
    data : 2d np.ndarray
        Gather data to apply SDC to.
    v_pow : float
        Velocity power value.
    velocities: 1d np.ndarray
        Array of RMS velocities at provided `times`, measured in meters / second.
    t_pow: float
        Time power value.
    times : 1d np.ndarray
        Array of times for each sample, measured in milliseconds.

    Returns
    -------
    data : 2d array
        Gather data with applied SDC.
    """
    sdc_coefficient = calculate_sdc_coefficient(v_pow, velocities, t_pow, times)
    for i in prange(len(data)):  # pylint: disable=not-an-iterable
        data[i] *= sdc_coefficient
    return data

@njit(nogil=True, parallel=True)
def undo_sdc(data, v_pow, velocities, t_pow, times):
    """Calculate spherical divergence correction coefficients and use them to undo previously applied SDC.

    SDC coefficients are a function of time and velocity:
    .. math::
        g(t) ~ velocities^{v_{pow}} * times^{t_{pow}}

    Parameters
    ----------
    data : 2d np.ndarray
        Gather data with applied SDC.
    v_pow : float
        Velocity power value.
    velocities: 1d np.ndarray
        Array of RMS velocities at provided `times`, measured in meters / second.
    t_pow: float
        Time power value.
    times : 1d np.ndarray
        Array of times for each sample, measured in milliseconds.

    Returns
    -------
    data : 2d array
        Gather data without SDC.
    """
    sdc_coefficient = calculate_sdc_coefficient(v_pow, velocities, t_pow, times)
    for i in prange(len(data)):  # pylint: disable=not-an-iterable
        data[i] /= sdc_coefficient
    return data
