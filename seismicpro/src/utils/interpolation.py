"""Implements a class for linear 1d interpolation and extrapolation"""

import numpy as np
from numba import njit, prange


@njit(nogil=True)
def interpolate(x_new, x, y, left_slope, right_slope):
    """Return a 1d piecewise linear interpolant to a function defined by pairs of data points `(x, y)`, evaluated at
    `x_new`. Function values at points outside the `x` range will be linearly extrapolated using passed slopes."""
    res = np.interp(x_new, x, y)
    for i, curr_x in enumerate(x_new):
        if curr_x < x[0]:
            res[i] = y[0] - left_slope * (x[0] - curr_x)
        elif curr_x > x[-1]:
            res[i] = y[-1] + right_slope * (curr_x - x[-1])
    return res


#pylint: disable=invalid-name
class interp1d:
    """Return a 1d piecewise linear interpolant to a function defined by pairs of data points `(x, y)`. Function values
    at points outside the `x` range will be linearly extrapolated.

    Parameters
    ----------
    x : 1d array-like
        X coordinates of function values.
    y : 1d array-like
        Function values, evaluated at `x`. Must match the length of `x`.
    """
    def __init__(self, x, y):
        x = np.array(x, dtype=np.float32)
        y = np.array(y, dtype=np.float32)

        if len(x) < 2:
            raise ValueError("At least two points should be passed to perform interpolation")

        ind = np.argsort(x, kind="mergesort")
        self.x = x[ind]
        self.y = y[ind]

        self.left_slope = (self.y[1] - self.y[0]) / (self.x[1] - self.x[0])
        self.right_slope = (self.y[-1] - self.y[-2]) / (self.x[-1] - self.x[-2])

    def __call__(self, x):
        """Evaluate the interpolant at passed coordinates `x`.

        Parameters
        ----------
        x : 1d array-like
            Points to evaluate the interpolant at.

        Returns
        -------
        y : 1d array-like
            Interpolated values, matching the length of `x`.
        """
        x = np.array(x)
        is_scalar_input = (x.ndim == 0)
        res = interpolate(x.ravel(), self.x, self.y, self.left_slope, self.right_slope)
        return res.item() if is_scalar_input else res


@njit(nogil=True)
def _times_to_indices(times, samples, round):
    left_slope = 1 / (samples[1] - samples[0])
    right_slope = 1 / (samples[-1] - samples[-2])
    float_position = interpolate(times, samples, np.arange(len(samples), dtype=np.float32), left_slope, right_slope)
    return np.rint(float_position) if round else float_position


@njit(nogil=True)
def calculate_basis_polynomials(x_new, x, n, secure_edges):
    """ Calculate the values of basis polynomials for Lagrange interpolation. """
    N = n + 1
    sample_rate = x[1] - x[0]
    polynomials = np.ones((len(x_new), N))

    # for given point, n + 1 neighbor samples are required to construct polynomial, find the index of leftmsot one
    leftmost_indices = _times_to_indices(x_new - (sample_rate * n / 2), x, True).astype(np.int32)

    if secure_edges:
        indices = np.array([ [x + i for i in range(N) ] for x in leftmost_indices ])        
        indices = np.where(indices < len(x) - 1, np.abs(indices), len(x) - (indices - len(x)) - 2)
    else:
        leftmost_indices = np.clip(leftmost_indices, 0, len(x) - n - 1)
        indices = np.array([ [x + i for i in range(N) ] for x in leftmost_indices ])
    
    y = (x_new - x[np.abs(leftmost_indices)] * np.sign(leftmost_indices + 1e-9) ) / sample_rate
    
    for i, iy in enumerate(y):
        for k in range(n + 1):
            for j in range(n + 1):
                if k != j:
                    polynomials[i, k] *= (iy - j) / (k - j)       
    return polynomials, indices


@njit(nogil=True, parallel=True)
def piecewise_polynomial(x_new, x, y, n, secure_edges=True):
    """" Perform piecewise polynomial (with degree n) interpolation ."""
    is_1d = (y.ndim == 1)
    y = np.atleast_2d(y)
    res = np.zeros((len(y), len(x_new)), dtype=y.dtype)

    # calculate Lagrange basis polynomials only once: they are the same at given position for all the traces
    polynomials, indices = calculate_basis_polynomials(x_new, x, n, secure_edges)

    for j in prange(len(y)):  # pylint: disable=not-an-iterable
        for i, ix in enumerate(indices):
            # interpolate at given point: multiply base polynomials and correspondoing function values and sum
            for p in range(n + 1):
                res[j, i] += polynomials[i, p] * y[j, ix[p]]

    if is_1d:
        return res[0]
    return res
