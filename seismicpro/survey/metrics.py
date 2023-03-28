# pylint: disable=not-an-iterable
"""Implements survey metrics"""

import warnings
from functools import partial

import numpy as np
from numba import njit
from matplotlib import patches

from ..metrics import Metric
from .utils import isclose
from ..utils import times_to_indices

# Ignore all warnings related to empty slices or dividing by zero
warnings.simplefilter("ignore", category=RuntimeWarning)


class SurveyAttribute(Metric):
    """A utility metric class that reindexes given survey by `index_cols` and allows for plotting gathers by their
    indices. Does not implement any calculation logic."""
    def __init__(self, name=None):
        super().__init__(name=name)

        # Attributes set after context binding
        self.survey = None

    @property
    def header_cols(self):
        """Column names in survey.headers to srote metrics results."""
        return self.name

    def bind_context(self, metric_map, survey):
        """Process metric evaluation context: memorize the parent survey, reindexed by `index_cols`."""
        self.survey = survey.reindex(metric_map.index_cols)

    def plot(self, ax, coords, index, sort_by=None, **kwargs):
        """Plot a gather by its `index`. Optionally sort it."""
        _ = coords
        gather = self.survey.get_gather(index)
        if sort_by is not None:
            gather = gather.sort(by=sort_by)
        gather.plot(ax=ax, **kwargs)

    def get_views(self, sort_by=None, **kwargs):
        """Return a single view, that plots a gather sorted by `sort_by` by click coordinates."""
        return [partial(self.plot, sort_by=sort_by)], kwargs


class TracewiseMetric(SurveyAttribute):
    """Base class for tracewise metrics with addidional plotters and aggregations. Child classes should redefine
    `get_mask` method, and optionnaly `preprocess`."""
    threshold = None
    top_ax_y_scale = "linear"

    def __call__(self, gather):
        """Compute metric by applying `self.preprocess`, `self.get_mask` and `self.aggregate` to provided gather."""
        gather = self.preprocess(gather)
        mask = self.get_mask(gather)
        return self.aggregate(mask)

    @property
    def description(self):
        """String description of tracewise metric"""
        return NotImplementedError

    def preprocess(self, gather):
        """Preprocess gather before calculating metric. Identity by default."""
        _ = self
        return gather

    def get_mask(self, gather):
        """QC indicator implementation. Takes a gather as an argument and returns either a samplewise qc indicator with
        shape equal to `gather.shape` or a tracewize indicator with shape (`gather.n_traces`,).

        Since all metrics calculated in threads, it may be more effective to call directly numba-decorated function.
        Thus, depending on the case, implement QC indicator either here or in `self.numba_get_mask`."""
        return self.numba_get_mask(gather.data)

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """Parallel QC metric implemetation. """
        raise NotImplementedError

    def aggregate(self, mask):
        """Aggregate input mask depending on `self.is_lower_better` to select the worst mask value for each trace"""
        if self.is_lower_better is None:
            agg_fn = np.nanmean
        elif self.is_lower_better:
            agg_fn = np.nanmax
        else:
            agg_fn = np.nanmin
        return mask if mask.ndim == 1 else agg_fn(mask, axis=1)

    def binarize(self, mask, threshold=None):
        """Binarize input mask by `threshold` marking bad mask values as True. Depending on `self.is_lower_better`
        values greater or less than the `threshold` will be treated as a bad value. If `threshold` is None,
        `self.threshold` is used."""
        threshold = self.threshold if threshold is None else threshold
        if threshold is None:
            raise ValueError("Either `threshold` or `self.threshold` must be non None")

        if isinstance(threshold, (int, float, np.number)):
            if self.is_lower_better is None:
                raise ValueError("`threshold` cannot be single number if `is_lower_better` is None")
            bin_fn = np.greater_equal if self.is_lower_better else np.less_equal
            return bin_fn(mask, threshold)

        if len(threshold) != 2:
            raise ValueError(f"`threshold` should contain exactly 2 elements, not {len(threshold)}")

        return (mask <= threshold[0]) | (mask >= threshold[1])

    def plot(self, ax, coords, index, sort_by=None, threshold=None, top_ax_y_scale=None,  bad_only=False, **kwargs):
        """Gather plot where samples with indicator above/below `.threshold` are highlited."""
        threshold = self.threshold if threshold is None else threshold
        top_ax_y_scale = self.top_ax_y_scale if top_ax_y_scale is None else top_ax_y_scale
        _ = coords

        gather = self.survey.get_gather(index)
        if sort_by is not None:
            gather = gather.sort(sort_by)
        gather = self.preprocess(gather)

        mask = self.get_mask(gather)
        metric_vals = self.aggregate(mask)
        bin_mask = self.binarize(mask, threshold)
        if bad_only:
            gather.data[self.aggregate(bin_mask) == 0] = np.nan

        mode = kwargs.pop("mode", "wiggle")
        masks_dict = {"masks": bin_mask, "alpha": 0.8, "label": self.name or "metric", **kwargs.pop("masks", {})}
        gather.plot(ax=ax, mode=mode, top_header=metric_vals, masks=masks_dict, **kwargs)
        top_ax = ax.figure.axes[1]
        if threshold is not None:
            self._plot_threshold(ax=top_ax, threshold=threshold)
        top_ax.set_yscale(top_ax_y_scale)

    @staticmethod
    def _plot_threshold(ax, threshold):
        if isinstance(threshold, (int, float, np.number)):
            ax.axhline(threshold, alpha=0.5, color="blue")
        else:
            start, end = ax.get_xlim()
            ax.fill_between(np.arange(start+0.5, end+0.5), *threshold, alpha=0.3, color="blue")

    def get_views(self, sort_by=None, threshold=None, top_ax_y_scale=None, **kwargs):
        """Return plotters of the metric views and those `kwargs` that should be passed further to an interactive map
        plotter."""
        plot_kwargs = {"sort_by": sort_by, "threshold": threshold, "top_ax_y_scale": top_ax_y_scale}
        return [partial(self.plot, **plot_kwargs), partial(self.plot, bad_only=True, **plot_kwargs)], kwargs


class MuteTracewiseMetric(TracewiseMetric):  # pylint: disable=abstract-method
    """Base class for tracewise metric with implemented `self.preprocess` method which applies muting and standard
    scaling to the input gather. Child classes should redefine `get_mask` method."""

    def __init__(self, muter, name=None):
        super().__init__(name=name)
        self.muter = muter

    def __repr__(self):
        """String representation of the metric."""
        return f"{type(self).__name__}(name='{self.name}', muter='{self.muter}')"

    def preprocess(self, gather):
        """Apply muting with np.nan as a fill value and standard scaling to provided gather."""
        return gather.copy().mute(muter=self.muter, fill_value=np.nan).scale_standard()


class Spikes(MuteTracewiseMetric):
    """Spikes detection. The metric reacts to drastic changes in traces ampliutes in 1-width window around each
    amplitude value.

    The metric is highly depends on muter, if muter isn't strong enough, the metric will overreact to the first breaks.

    Parameters
    ----------
    muter : Muter
        A muter to use.
    name : str, optional, defaults to "spikes"
        Metrics name.

    Attributes
    ----------
    ?? Do we want to describe them ??
    """
    name = "spikes"
    min_value = 0
    max_value = None
    is_lower_better = True
    threshold = 2

    @property
    def description(self):
        """String description of tracewise metric"""
        return "Traces with spikes"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation.

        The resulted 2d mask shows the deviation of the ampluteds of an input gather.
        """
        traces = traces.copy()
        res = np.zeros_like(traces)
        for i in range(traces.shape[0]):
            nan_indices = np.nonzero(np.isnan(traces[i]))[0]
            if len(nan_indices) > 0:
                j = nan_indices[-1] + 1
                if j < traces.shape[1]:
                    traces[i, :j] = traces[i, j]
                else:
                    traces[i, :] = 0
            res[i, 1: -1] = np.abs(traces[i, 2:] + traces[i, :-2] - 2*traces[i, 1:-1]) / 3
        return res


class Autocorrelation(MuteTracewiseMetric):
    """Trace correlation with itself shifted by 1.

    The metric is highly depends on muter, if muter isn't strong enough, the metric will overreact to the first breaks.

    Parameters
    ----------
    muter : Muter
        A muter to use.
    name : str, optional, defaults to "autocorrelation"
        Metrics name.
    """
    name = "autocorrelation"
    min_value = -1
    max_value = 1
    is_lower_better = False
    threshold = 0.8

    def __init__(self, muter, name=None, nan_ratio=0.95):
        super().__init__(muter=muter, name=name)
        self.nan_ratio = nan_ratio

    @property
    def description(self):
        """String description of tracewise metric"""
        return f"Traces with autocorrelation less than {self.threshold}"

    def get_mask(self, gather):
        return self.numba_get_mask(gather.data, nan_ratio=self.nan_ratio)

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces, nan_ratio):
        """QC indicator implementation."""
        res = np.empty_like(traces[:, 0])
        for i in range(traces.shape[0]):
            if np.isnan(traces[i]).sum() > nan_ratio*len(traces[i]):
                res[i] = np.nan
            else:
                res[i] = np.nanmean(traces[i, 1:] * traces[i, :-1])
        return res

class TraceAbsMean(TracewiseMetric):
    """Absolute value of the trace's mean scaled by trace's std.

    Parameters
    ----------
    name : str, optional, defaults to "trace_absmean"
        Metrics name.
    """
    name = "trace_absmean"
    is_lower_better = True
    threshold = 0.1

    @property
    def description(self):
        """String description of tracewise metric"""
        return f"Traces with mean divided by std greater than {self.threshold}"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation."""
        res = np.empty_like(traces[:, 0])
        for i in range(traces.shape[0]):
            res[i] = np.abs(traces[i].mean() / (traces[i].std() + 1e-10))
        return res


class TraceMaxAbs(TracewiseMetric):
    """Maximum absolute amplitude value scaled by trace's std.

    Parameters
    ----------
    name : str, optional, defaults to "trace_maxabs"
        Metrics name.
    """
    name = "trace_maxabs"
    is_lower_better = True
    threshold = 15

    @property
    def description(self):
        """String description of tracewise metric"""
        return f"Traces with max abs to std ratio greater than {self.threshold}"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation."""
        res = np.empty_like(traces[:, 0])
        for i in range(traces.shape[0]):
            res[i] = np.max(np.abs(traces[i])) / (traces[i].std() + 1e-10)
        return res


class MaxClipsLen(TracewiseMetric):
    """Detecting minimum and maximun clips.
    #TODO: describe how will look the resulted mask, either here or in `get_mask`.

    Parameters
    ----------
    name : str, optional, defaults to "max_clips_len"
        Metrics name.
    """
    name = "max_clips_len"
    min_value = 1
    max_value = None
    is_lower_better = True
    threshold = 3

    @property
    def description(self):
        """String description of tracewise metric"""
        return f"Traces with more than {self.threshold} clipped samples in a row"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation."""
        def _update_counters(trace, i, j, value, counter, container):
            if isclose(trace, value):
                counter += 1
            else:
                if counter > 1:
                    container[i, j - counter: j] = counter
                counter = 0
            return counter

        maxes = np.zeros_like(traces)
        mins = np.zeros_like(traces)
        for i in range(traces.shape[0]):
            trace = traces[i]
            max_val = max(trace)
            max_counter = 0
            min_val = min(trace)
            min_counter = 0
            for j in range(trace.shape[0]):  # pylint: disable=consider-using-enumerate
                max_counter = _update_counters(trace[j], i, j, max_val, max_counter, maxes)
                min_counter = _update_counters(trace[j], i, j, min_val, min_counter, mins)

            if max_counter > 1:
                maxes[i, -max_counter:] = max_counter
            if min_counter > 1:
                mins[i, -min_counter:] = min_counter
        return (maxes + mins)


class MaxConstLen(TracewiseMetric):
    """Detecting constant subsequences.

    #TODO: describe how will look the resulted mask, either here or in `get_mask`.
    Parameters
    ----------
    name : str, optional, defaults to "const_len"
        Metrics name.
    """
    name = "const_len"
    is_lower_better = True
    threshold = 4

    @property
    def description(self):
        """String description of tracewise metric"""
        return f"Traces with more than {self.threshold} identical values in a row"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation."""
        indicator = np.zeros_like(traces)
        for i in range(traces.shape[0]):
            trace = traces[i]
            counter = 1
            for j in range(1, trace.shape[0]):  # pylint: disable=consider-using-enumerate
                if isclose(trace[j], trace[j-1]):
                    counter += 1
                else:
                    if counter > 1:
                        indicator[i, j - counter: j] = counter
                    counter = 1

            if counter > 1:
                indicator[i, -counter:] = counter
        return indicator


class DeadTrace(TracewiseMetric):
    """Detects constant traces.

    Parameters
    ----------
    name : str, optional, defaults to "dead_trace"
        Metrics name.
    """
    name = "dead_trace"
    min_value = 0
    max_value = 1
    is_lower_better = True
    threshold = 0.5

    @property
    def description(self):
        return "Number of dead traces"

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces):
        """QC indicator implementation."""
        res = np.empty_like(traces[:, 0])
        for i in range(traces.shape[0]):
            res[i] = isclose(max(traces[i]), min(traces[i]))
        return res


class BaseWindowMetric(TracewiseMetric):
    """Base class for all window based metric that provide method for computing sum of squares of traces amplitudes in
    provided windows defined by start and end indices, and length of windows for every trace. Also, provide a method
    `self.aggregate_headers` that is aggregating the results by passed `index_cols` or `coords_cols`."""

    def __call__(self, gather, return_rms=True):
        """Compute metric by applying `self.preprocess` and `self.get_mask` to provided gather."""
        gather = self.preprocess(gather)
        squares, nums = self.get_mask(gather)
        if return_rms:
            return self.compute_rms(squares, nums)
        return squares, nums

    @property
    def header_cols(self):
        """Column names in survey.headers to srote metrics results."""
        return [self.name+"_sum", self.name+"_n"]

    @staticmethod
    def compute_rms(squares, nums):
        return np.sqrt(np.sum(squares) / np.sum(nums))

    @staticmethod
    @njit(nogil=True)
    def compute_stats_by_ixs(data, start_ixs, end_ixs):
        """TODO"""
        sum_squares = np.empty_like(data[:, 0])
        nums = np.empty_like(data[:, 0])

        for i in range(data.shape[0]):
            trace = data[i]
            start_ix = start_ixs[i]
            end_ix = end_ixs[i]
            sum_squares[i] = sum(trace[start_ix: end_ix] ** 2)
            nums[i] = len(trace[start_ix: end_ix])
        return sum_squares, nums

    def construct_map(self, coords, values, *, coords_cols=None, index=None, index_cols=None, agg=None, bin_size=None,
                      calculate_immediately=True):
        """TODO"""
        sum_square_map = super().construct_map(coords, values.iloc[:, 0], coords_cols=coords_cols, index=index,
                                               index_cols=index_cols, agg="sum")
        nums_map = super().construct_map(coords, values.iloc[:, 1], coords_cols=coords_cols, index=index,
                                         index_cols=index_cols, agg="sum")
        sum_square_map.index_data.drop(columns=sum_square_map.coords_cols, inplace=True)
        sum_df = sum_square_map.index_data.merge(nums_map.index_data, on=nums_map.index_cols)
        sum_df[self.name] = np.sqrt(sum_df[self.name+"_x"] / sum_df[self.name+"_y"])
        return super().construct_map(sum_df[coords.columns], sum_df[self.name], index=sum_df[index.columns], agg=agg,
                                     bin_size=bin_size, calculate_immediately=calculate_immediately)

    def plot(self, ax, coords, index, threshold=None, top_ax_y_scale=None, bad_only=False, color="lime",
             **kwargs):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows"""
        threshold = self.threshold if threshold is None else threshold
        top_ax_y_scale = self.top_ax_y_scale if top_ax_y_scale is None else top_ax_y_scale
        _ = coords
        gather = self.survey.get_gather(index).sort("offset")
        squares, nums = self(gather, return_rms=False)
        tracewise_metric = np.sqrt(squares / nums)
        tracewise_metric[tracewise_metric == 0] = np.nan
        if bad_only:
            bin_mask = self.binarize(tracewise_metric, threshold)
            gather.data[self.aggregate(bin_mask) == 0] = np.nan

        gather.plot(ax=ax, top_header=tracewise_metric, **kwargs)
        top_ax = ax.figure.axes[1]
        if threshold is not None:
            self._plot_threshold(ax=top_ax, threshold=threshold)
        top_ax.set_yscale(top_ax_y_scale)
        self._plot(ax=ax, gather=gather, color=color)

    def _plot(self, ax, gather):
        """Add any additional metric related graphs on plot"""
        pass

    def get_views(self, threshold=None, top_ax_y_scale=None, **kwargs):
        """Return plotters of the metric views and those `kwargs` that should be passed further to an interactive map
        plotter."""
        plot_kwargs = {"threshold": threshold, "top_ax_y_scale": top_ax_y_scale}
        return [partial(self.plot, **plot_kwargs), partial(self.plot, bad_only=True, **plot_kwargs)], kwargs


class MetricsRatio(TracewiseMetric):
    is_lower_better = False
    threshold = None

    def __init__(self, numerator, denominator, name=None):
        for metric in [numerator, denominator]:
            if not isinstance(metric, BaseWindowMetric):
                raise ValueError()

        name = f"{numerator.name} to {denominator.name} ratio" if name is None else name
        super().__init__(name=name)

        self.numerator = numerator
        self.denominator = denominator

    @property
    def header_cols(self):
        return self.numerator.header_cols + self.denominator.header_cols

    def construct_map(self, coords, values, *, coords_cols=None, index=None, index_cols=None, agg=None, bin_size=None,
                      calculate_immediately=True):
        mmaps_1 = self.numerator.construct_map(coords, values[self.numerator.header_cols], coords_cols=coords_cols,
                                               index=index, index_cols=index_cols)
        mmaps_2 = self.denominator.construct_map(coords, values[self.denominator.header_cols], coords_cols=coords_cols,
                                                 index=index, index_cols=index_cols)

        mmaps_1.index_data.drop(columns=mmaps_1.coords_cols, inplace=True)
        ratio_df = mmaps_1.index_data.merge(mmaps_2.index_data, on=mmaps_1.index_cols)
        ratio_df[self.name] = ratio_df[self.numerator.name] / ratio_df[self.denominator.name]
        coords = ratio_df[coords.columns]
        values = ratio_df[self.name]
        index = ratio_df[index.columns]
        return super().construct_map(coords, values, index=index, agg=agg, bin_size=bin_size,
                                     calculate_immediately=calculate_immediately)

    def plot(self, ax, coords, index, threshold=None, top_ax_y_scale=None, bad_only=False, **kwargs):
        threshold = self.threshold if threshold is None else threshold
        top_ax_y_scale = self.top_ax_y_scale if top_ax_y_scale is None else top_ax_y_scale
        _ = coords
        gather = self.survey.get_gather(index).sort("offset")

        squares_numerator, nums_numerator = self.numerator(gather, return_rms=False)
        squares_denominator, nums_denominator = self.denominator(gather, return_rms=False)

        tracewise_numerator = np.sqrt(squares_numerator / nums_numerator)
        tracewise_denominator = np.sqrt(squares_denominator / nums_denominator)
        tracewise_metric = tracewise_numerator / tracewise_denominator
        tracewise_metric[tracewise_metric == 0] = np.nan

        if bad_only:
            bin_mask = self.binarize(tracewise_metric, threshold)
            gather.data[self.aggregate(bin_mask) == 0] = np.nan

        gather.plot(ax=ax, top_header=tracewise_metric, **kwargs)
        top_ax = ax.figure.axes[1]
        if threshold is not None:
            self._plot_threshold(ax=top_ax, threshold=threshold)
        top_ax.set_yscale(top_ax_y_scale)

        self.numerator._plot(ax=ax, gather=gather, color="lime", legend="numerator window")
        self.denominator._plot(ax=ax, gather=gather, color="magenta", legend="denominator window")
        ax.legend()

    def get_views(self, threshold=None, top_ax_y_scale=None, **kwargs):
        """Return plotters of the metric views and those `kwargs` that should be passed further to an interactive map
        plotter."""
        plot_kwargs = {"threshold": threshold, "top_ax_y_scale": top_ax_y_scale}
        return [partial(self.plot, **plot_kwargs), partial(self.plot, bad_only=True, **plot_kwargs)], kwargs

class WindowRMS(BaseWindowMetric):
    """Computes traces RMS for provided window by offsets and times.

    Parameters
    ----------
    offsets : tuple of 2 ints
        Offset range to use for calcualtion.
    times : tuple of 2 ints
        Time range to use for calcualtion, measured in ms.
    name : str, optional, defaults to "rms"
        Metrics name.
    """
    name = "rms"
    is_lower_better = None
    threshold = None

    def __init__(self, offsets, times, name=None):
        if len(offsets) != 2:
            raise ValueError(f"`offsets` must contain 2 elements, not {len(offsets)}")

        if len(times) != 2:
            raise ValueError(f"`times` must contain 2 elements, not {len(times)}")

        super().__init__(name=name)
        self.offsets = np.array(offsets)
        self.times = np.array(times)

    def __repr__(self):
        """String representation of the metric."""
        return f"{type(self).__name__}(name='{self.name}', offsets='{self.offsets}', times='{self.times}')"

    def get_mask(self, gather):
        """QC indicator implementation."""
        return self.numba_get_mask(gather.data, gather.samples, gather.offsets, self.times, self.offsets,
                                   self._get_time_ixs, self.compute_stats_by_ixs)

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces, gather_samples, gather_offests, times, offsets, _get_time_ixs,
                       compute_stats_by_ixs):
        """QC indicator implementation."""
        times = _get_time_ixs(gather_samples, times)

        window_ixs = (gather_offests >= offsets[0]) & (gather_offests <= offsets[1])
        start_ixs = np.full(sum(window_ixs), fill_value=times[0], dtype=np.int16)
        end_ixs = np.full(sum(window_ixs), fill_value=times[1], dtype=np.int16)
        squares = np.zeros_like(traces[:, 0])
        nums = np.zeros_like(traces[:, 0])
        window_squares, window_nums = compute_stats_by_ixs(traces[window_ixs], start_ixs, end_ixs)
        squares[window_ixs] = window_squares
        nums[window_ixs] = window_nums
        return squares, nums

    @staticmethod
    @njit(nogil=True)
    def _get_time_ixs(gather_samples, times):
        times = np.asarray([max(gather_samples[0], times[0]), min(gather_samples[-1], times[1])])
        return times_to_indices(times, gather_samples).astype(np.int16)

    def _plot(self, ax, gather, color="lime", legend=None):
        times = self._get_time_ixs(gather.samples, self.times)

        offs_ind = np.nonzero((gather.offsets >= self.offsets[0]) & (gather.offsets <= self.offsets[1]))[0]
        if len(offs_ind) > 0:
            n_rec = (offs_ind[0], times[0]), len(offs_ind), (times[1] - times[0])
            ax.add_patch(patches.Rectangle(*n_rec, linewidth=2, edgecolor=color, facecolor='none', label=legend))


class AdaptiveWindowRMS(BaseWindowMetric):
    """Signal to Noise RMS ratio computed in sliding windows along provided refractor velocity.
    RMS will be computed in two windows for every gather:
    1. Window shifted up from refractor velocity by `shift_up` ms. RMS in this window represents the noise value.
    2. WIndow shifted down from refractor velocity by `shift_down` ms`. RMS in this window represents the signal value.

    Only traces that contain noise and signal windows of the provided `window_size` are considered,
    the metric is 0 for other traces.


    Parameters
    ----------
    window_size : int
        Length of the windows for computing signam and noise RMS amplitudes measured in ms.
    shift_up : int
        The delta between noise window end and first breaks, measured in ms.
    shift_down : int
        The delta between signal window beginning and first breaks, measured in ms.
    refractor_velocity: RefractorVelocity
        Refractor velocity object to find times along witch
    name : str, optional, defaults to "adaptive_rms"
        Metrics name.
    """
    name = "adaptive_rms"
    is_lower_better = False
    threshold = None

    def __init__(self, window_size, shift, refractor_velocity, name=None):
        super().__init__(name=name)
        self.window_size = window_size
        self.shift = shift
        self.refractor_velocity = refractor_velocity

    def __repr__(self):
        """String representation of the metric."""
        repr_str = f"(name='{self.name}', window_size='{self.window_size}', shift='{self.shift}', "\
                   f"refractor_velocity='{self.refractor_velocity}')"
        return f"{type(self).__name__}" + repr_str

    def get_mask(self, gather):
        """QC indicator implementation. See `plot` docstring for parameters descriptions."""
        fbp_times = self.refractor_velocity(gather.offsets)
        return self.numba_get_mask(gather.data, self._get_indices, self.compute_stats_by_ixs,
                                   window_size=self.window_size, shift=self.shift, samples=gather.samples,
                                   fbp_times=fbp_times, times_to_indices=times_to_indices)

    @staticmethod
    @njit(nogil=True)
    def numba_get_mask(traces, _get_indices, compute_stats_by_ixs, window_size, shift, samples, fbp_times,
                       times_to_indices):
        start_ixs, end_ixs = _get_indices(window_size, shift, samples, fbp_times, times_to_indices)
        return compute_stats_by_ixs(traces, start_ixs, end_ixs)

    @staticmethod
    @njit(nogil=True)
    def _get_indices(window_size, shift, samples, fbp_times, times_to_indices):
        """Convert times to use for noise and signal windows into indices"""
        mid_samples = times_to_indices(fbp_times + shift, samples, round=True).astype(np.int16)
        window_size = int(times_to_indices(np.array([window_size]), samples, round=True)[0])

        start_ixs = np.clip(mid_samples - (window_size - window_size // 2), 0, len(samples))
        end_ixs = np.clip(mid_samples + (window_size // 2), 0, len(samples))
        return start_ixs, end_ixs

    def _plot(self, ax, gather, color="lime", legend=None):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows."""
        fbp_times = self.refractor_velocity(gather.offsets)
        indices = self._get_indices(self.window_size, self.shift, gather.samples, fbp_times, times_to_indices)
        indices = np.where(np.asarray(indices) == 0, np.nan, indices)
        indices = np.where(np.asarray(indices) == np.nanmax(indices), np.nan, indices)

        ax.plot(np.arange(gather.n_traces), indices[0], color=color, label=legend)
        ax.plot(np.arange(gather.n_traces), indices[1], color=color)

DEFAULT_TRACEWISE_METRICS = [TraceAbsMean, TraceMaxAbs, MaxClipsLen, MaxConstLen, DeadTrace]
