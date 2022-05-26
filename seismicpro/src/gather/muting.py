"""Implements Muter class to define a boundary above which gather values will be zeroed out"""

import numpy as np
from numba import njit, prange
from scipy.interpolate import interp1d
from sklearn.linear_model import LinearRegression

from ..utils import read_single_vfunc
from .utils.general_utils import compute_crossovers_times


class Muter:
    """A class to define an offset-time boundary above which gather values will be muted i.e. zeroed out.

    Usually muting is performed to attenuate any strong, coherent noise that was generated by the shot, e.g. high
    amplitudes near the first breaks. Such kind of noise strongly affects several processing routines, such as
    :func:`~Gather.calculate_semblance`.

    A muter object can be created from three different types of data by calling a corresponding `classmethod`:
    * `from_points` - create a muter from 1d arrays of offsets and times,
    * `from_file` - create a muter from a file in VFUNC format with offset-time pairs,
    * `from_first_breaks` - create a muter from 1d arrays of offsets and times of first breaks.

    The created object is callable and returns times up to which muting should be performed for given offsets. If a
    muter is created by direct instantiation, zero time will be returned for every offset.

    Examples
    --------
    >>> muter = Muter.from_points(offsets=[100, 1000, 2000], times=[200, 2000, 3000])
    >>> muter([0, 100, 500, 1000, 1500, 2000])
    array([   0.,  200., 1000., 2000., 2500., 3000.])

    Attributes
    ----------
    muter : callable
        Return muting times for given offsets. `muter` argument must be either numeric or 1d array-like.
    """
    def __init__(self):
        self.muter = lambda offsets: np.zeros_like(offsets)

    @classmethod
    def from_points(cls, offsets, times, fill_value="extrapolate"):
        """Create a muter from 1d arrays of offsets and times.

        The resulting muter performs linear time interpolation between points given, its behavior outside of the
        offsets' range is defined by the `fill_value` argument.

        Parameters
        ----------
        offsets : 1d array-like
            An array with offset values. Measured in meters.
        times : 1d array-like
            An array with muting times, matching the length of `offsets`. Measured in milliseconds.
        fill_value : float or (float, float) or "extrapolate", optional, defaults to "extrapolate"
            - If float, this value is used to fill in for requested points outside of the data range,
            - If a two-element tuple, then its elements are used as fill values before min offset and after max offset
              given respectively,
            - If "extrapolate", then points outside the data range will be linearly extrapolated.

        Returns
        -------
        self : Muter
            Created muter.
        """
        self = cls()
        self.muter = interp1d(offsets, times, fill_value=fill_value)
        return self

    @classmethod
    def from_file(cls, path, **kwargs):
        """Create a muter from a file with vertical functions in Paradigm Echos VFUNC format.

        The file must have exactly one record with the following structure:
        VFUNC [inline] [crossline]
        [offset_1] [time_1] [offset_2] [time_2] ... [offset_n] [time_n]

        The loaded data is directly passed to :func:`~Muter.from_points`. The resulting muter performs linear time
        interpolation between points given, its behavior outside of the offsets' range is defined by the
        `fill_value` argument.

        Parameters
        ----------
        path : str
            A path to the file with muting in VFUNC format.
        kwargs : misc, optional
            Additional keyword arguments to :func:`~Muter.from_points`.

        Returns
        -------
        self : Muter
            Created muter.
        """
        _, _, offsets, times = read_single_vfunc(path)
        return cls.from_points(offsets, times, **kwargs)

    @classmethod
    def from_first_breaks(cls, offsets, times, velocity_reduction=0):
        """Create a muter from 1d arrays of offsets and times of first breaks.

        The muter estimates seismic wave velocity in the weathering layer using linear regression, decrements it by
        `velocity_reduction` in order to attenuate amplitudes immediately following the first breaks and uses the
        resulting velocity to get muting times by offsets passed.

        Parameters
        ----------
        offsets : 1d array-like
            An array with offset values. Measured in meters.
        times : 1d array-like
            An array with times of first breaks, matching the length of `offsets`. Measured in milliseconds.
        velocity_reduction : float, optional, defaults to 0
            A value used to decrement the found velocity in the weathering layer to attenuate amplitudes immediately
            following the first breaks. Measured in meters/seconds.

        Returns
        -------
        self : Muter
            Created muter.
        """
        velocity_reduction = velocity_reduction / 1000  # from m/s to m/ms
        lin_reg = LinearRegression(fit_intercept=True)
        lin_reg.fit(np.array(times).reshape(-1, 1), np.array(offsets))

        # The fitted velocity is reduced by velocity_reduction in order to mute amplitudes near first breaks
        intercept = lin_reg.intercept_
        velocity = lin_reg.coef_ - velocity_reduction

        self = cls()
        self.muter = lambda offsets: (offsets - intercept) / velocity
        return self

    @classmethod
    def from_stacking_velocity(cls, stacking_velocity, max_stretch_factor=0.65, crossover_mute=True, times=None, offsets=None):
        """ docs """
        # if (times is None) and (stacking_velocity.times is not None):
        #     times = stacking_velocity.times
        # else:
        #     raise ValueError('Provide times')
        
        velocity = stacking_velocity(times) / 1000
        stretch_offsets = velocity * times * np.sqrt((1 + max_stretch_factor)**2 - 1)
        muter = cls.from_points(stretch_offsets, times)
        
        if crossover_mute:
            crossover_times = compute_crossovers_times(times, offsets, velocity)
            muter = cls.from_points(offsets, np.maximum(crossover_times, muter(offsets)))
        
        return muter 

    def __call__(self, offsets):
        """Returns times up to which muting should be performed for given offsets.

        Notes
        -----
        If the muter was created by direct instantiation, zero time will be returned for every offset.

        Parameters
        ----------
        offsets : 1d array-like
            An array with offset values. Measured in meters.

        Returns
        -------
        times : 1d array-like
            An array with muting times, matching the length of `offsets`. Measured in milliseconds.
        """
        return self.muter(offsets)
