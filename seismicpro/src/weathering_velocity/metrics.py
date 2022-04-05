"""Implements the weathering velocity metrics"""

import numpy as np

from ..metrics import PipelineMetric, pass_calc_args
from ..const import HDR_FIRST_BREAK


class WeatheringVelocityMetric(PipelineMetric):
    """Calculate a weathering metric. Weathering metric is fraction of first breaking times that stands out from
    a weathering velocity curve (piecewise linear function) more than threshold.
    """
    name = "weathering_metrics"
    vmin = 0
    vmax = 0.05
    is_lower_better = True
    views = ("plot", "plot_wv")

    @staticmethod
    def calc(gather, weathering_velocity, first_breaks_col=HDR_FIRST_BREAK, threshold_time=50):
        """Calculates the weathering metric value.

        Weathering metric calculated as fraction of first breaking times that stands out from a weathering velocity
        curve (piecewise linear function) more that `threshold_times` relative to the total number of first breaking
        times.

        Parameters
        ----------
        weathering_velocity : WeatheringVelocity
            Calculated WeatheringVelocity. Use `calculate_weathering_velocity` to calculate it.
        first_breaks_col : str, defaults to HDR_FIRST_BREAK
            Column name  from `self.headers` where first breaking times are stored.
        threshold_time: int or float, defaults to 50
            Threshold for the weathering metric calculation.

        Returns
        -------
        metric : float
            Fraction of the first breaks stands out from the weathering velocity curve more than given threshold time.
        """
        metric = np.abs(weathering_velocity(gather.offsets) - gather[first_breaks_col].ravel()) > threshold_time
        return np.mean(metric)

    @pass_calc_args
    def plot(cls, gather, weathering_velocity, ax, threshold_times=50, mode='seismogram',
             first_breaks_col=HDR_FIRST_BREAK, **kwargs):
        """Plot the gather and the first break points."""
        _ = weathering_velocity, threshold_times
        gather.plot(ax=ax, event_headers={'headers': first_breaks_col}, mode=mode, **kwargs)

    @pass_calc_args
    def plot_wv(cls, gather, weathering_velocity, ax, first_breaks_col=HDR_FIRST_BREAK, threshold_times=50, **kwargs):
        """Plot the first break picking points, weathering velocity curve, thresholds, and weathering model
        parameters."""
        _ = gather, first_breaks_col
        weathering_velocity.plot(ax=ax, threshold_time=threshold_times, **kwargs)