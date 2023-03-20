# pylint: disable=not-an-iterable
"""Implements survey metrics"""

from functools import partial

import numpy as np
from numba import njit, prange
from matplotlib import patches

from ..metrics import Metric
from ..utils import times_to_indices, to_list


class SurveyAttribute(Metric):
    """A utility metric class that reindexes given survey by `index_cols` and allows for plotting gathers by their
    indices. Does not implement any calculation logic."""
    def __init__(self, name=None):
        super().__init__(name=name)

        # Attributes set after context binding
        self.survey = None

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

    min_value = None
    max_value = None
    is_lower_better = None
    threshold = None
    top_ax_y_scale = "linear"

    def __init__(self, name=None):
        super().__init__(name=name)

    def __call__(self, gather):
        """Compute metric by applying `self.preprocess`, `self.get_mask` and `self.aggregate` to provided gather."""
        gather = self.preprocess(gather)
        mask = self.get_mask(gather)
        return self.aggregate(mask)

    @property
    def header_cols(self):
        """Column names in survey.headers to srote metrics results."""
        return self.name

    def preprocess(self, gather):
        """Preprocess gather before calculating metric. Identity by default."""
        _ = self
        return gather

    def get_mask(self, gather):
        """QC indicator implementation. Takes a gather as an argument and returns either a samplewise qc indicator with
        shape equal to `gather.shape` or a tracewize indicator with shape (`gather.n_traces`,)."""
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

        return (mask < threshold[0]) | (mask > threshold[1])

    def construct_map(self, headers, index_cols, coords_cols, **kwargs):
        """Aggregate headers before constructing metric map. No aggregation performed by default."""
        index = headers[index_cols] if index_cols is not None else None
        return super().construct_map(headers[coords_cols], headers[self.name], index=index, **kwargs)

    def plot(self, ax, coords, index, sort_by=None, threshold=None, top_ax_y_scale=None,  bad_only=False, **kwargs):
        """Gather plot where samples with indicator above/below `.threshold` are highlited."""
        threshold = self.threshold if threshold is None else threshold
        top_ax_y_scale = self.top_ax_y_scale if top_ax_y_scale is None else top_ax_y_scale
        _ = coords

        gather = self.survey.get_gather(index)
        if sort_by is not None:
            gather = gather.sort(sort_by)
        gather = self.preprocess(gather)

        # TODO: Can we do only single copy here? (first copy sometimes done in self.preprocess)
        # We need to copy gather since some metrics changes gather in get_mask, but we want to plot gather unchanged
        mask = self.get_mask(gather.copy())
        metric_vals = self.aggregate(mask)
        bin_mask = self.binarize(mask, threshold)
        if bad_only:
            gather.data[self.aggregate(bin_mask) == 0] = np.nan

        mode = kwargs.pop("mode", "wiggle")
        masks_dict = {"masks": bin_mask, "alpha": 0.8, "label": self.name or "metric", **kwargs.pop("masks", {})}
        gather.plot(ax=ax, mode=mode, top_header=metric_vals, masks=masks_dict, **kwargs)
        ax.figure.axes[1].axhline(threshold, alpha=0.5)
        ax.figure.axes[1].set_yscale(top_ax_y_scale)

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

    def get_mask(self, gather):
        """QC indicator implementation.

        The resulted 2d mask shows the deviation of the ampluteds of an input gather.
        """
        traces = gather.data
        self.fill_leading_nulls(traces)

        res = np.abs(traces[:, 2:] + traces[:, :-2] - 2*traces[:, 1:-1]) / 3
        return np.pad(res, ((0, 0), (1, 1)))

    @staticmethod
    @njit(parallel=True, nogil=True)
    def fill_leading_nulls(arr):
        """"Fill leading null values of array's row with the first non null value in a row."""
        for i in prange(arr.shape[0]):
            nan_indices = np.nonzero(np.isnan(arr[i]))[0]
            if len(nan_indices) > 0:
                j = nan_indices[-1] + 1
                if j < arr.shape[1]:
                    arr[i, :j] = arr[i, j]
                else:
                    arr[i, :] = 0


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

    def get_mask(self, gather):
        """QC indicator implementation."""
        # TODO: descide what to do with almost nan traces (in 98% in trace are nan, it almost always will have -1 val)
        return np.nanmean(gather.data[:, 1:] * gather.data[:, :-1], axis=1)


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

    def get_mask(self, gather):
        """QC indicator implementation."""
        return np.abs(gather.data.mean(axis=1) / (gather.data.std(axis=1) + 1e-10))


class TraceMaxAbs(TracewiseMetric):
    """Maximun absolute amplitude value scaled by trace's std.

    Parameters
    ----------
    name : str, optional, defaults to "trace_maxabs"
        Metrics name.
    """
    name = "trace_maxabs"
    is_lower_better = True
    threshold = 15

    def get_mask(self, gather):
        """QC indicator implementation."""
        return np.max(np.abs(gather.data), axis=1) / (gather.data.std(axis=1) + 1e-10)


class MaxLenMetric(TracewiseMetric):  # pylint: disable=abstract-method
    """Base class for metrics that calculates length of continuous sequence of 1."""

    @staticmethod
    @njit(nogil=True, parallel=True)
    def compute_indicators_length(indicators, counter_init):
        """Compute length of continuous sequence of 1 in provided `indicators` and fill the squence with its length."""
        for i in prange(len(indicators)):
            counter = counter_init
            indicator = indicators[i]
            for j in range(len(indicator)):  # pylint: disable=consider-using-enumerate
                if indicator[j] == 1:
                    counter += 1
                else:
                    if counter > 1:
                        indicators[i, j - counter: j] = counter
                    counter = counter_init

            if counter > 1:
                indicators[i, -counter:] = counter
        return indicators


class MaxClipsLen(MaxLenMetric):
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

    def get_mask(self, gather):
        """QC indicator implementation."""
        traces = gather.data

        maxes = traces.max(axis=-1, keepdims=True)
        maxes_indicators = (np.atleast_2d(traces) == maxes).astype(np.int16)
        res_maxes = self.compute_indicators_length(maxes_indicators, 0)

        mins = traces.min(axis=-1, keepdims=True)
        mins_indicators = (np.atleast_2d(traces) == mins).astype(np.int16)
        res_mins = self.compute_indicators_length(mins_indicators, 0)

        return (res_maxes + res_mins).astype(np.float32).reshape(traces.shape)


class MaxConstLen(MaxLenMetric):
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

    def get_mask(self, gather):
        """QC indicator implementation."""
        traces = np.atleast_2d(gather.data)
        indicators = np.zeros_like(traces, dtype=np.int16)
        indicators[:, 1:] = (traces[:, 1:] == traces[:, :-1]).astype(np.int16)
        return self.compute_indicators_length(indicators, 1).astype(np.float32).reshape(gather.data.shape)


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

    def get_mask(self, gather):
        """Return QC indicator."""
        return (np.max(gather.data, axis=1) - np.min(gather.data, axis=1) < 1e-10).astype(np.float32)


class BaseWindowMetric(TracewiseMetric):
    """Base class for all window based metric that provide method for computing sum of squares of traces amplitudes in
    provided windows defined by start and end indices, and length of windows for every trace. Also, provide a method
    `self.aggregate_headers` that is aggregating the results by passed `index_cols` or `coords_cols`."""

    def __call__(self, gather):
        """Compute metric by applying `self.preprocess` and `self.get_mask` to provided gather."""
        gather = self.preprocess(gather)
        return self.get_mask(gather)

    @staticmethod
    @njit(nogil=True, parallel=True)
    def compute_stats_by_ixs(data, start_ixs_list, end_ixs_list):
        """TODO"""
        stats = np.full((data.shape[0], 2 * len(start_ixs_list)), fill_value=0, dtype=np.float32)

        for i in prange(data.shape[0]):
            trace = data[i]
            for ix in prange(len(start_ixs_list)):
                start_ix = start_ixs_list[ix][i]
                end_ix = end_ixs_list[ix][i]
                if start_ix >= 0 and end_ix >= 0:
                    stats[i, 2*ix] = sum(trace[start_ix: end_ix] ** 2)
                    stats[i, 2*ix+1] = len(trace[start_ix: end_ix])
        return stats

    def construct_map(self, headers, index_cols, coords_cols, **kwargs):
        groupby_cols = self.header_cols + (coords_cols if index_cols != coords_cols else [])
        groupby = headers.groupby(index_cols)[groupby_cols]
        sums_func = {sum_name: lambda x: np.sqrt(np.sum(x)) for sum_name in self.header_cols[::2]}
        nums_func = {num_name: "sum" for num_name in self.header_cols[1::2]}
        coords_func = {coord_name: "mean" for coord_name in groupby_cols[len(self.header_cols):]}

        aggregated_gb = groupby.agg({**sums_func, **nums_func, **coords_func})
        aggregated_gb.reset_index(inplace=True)
        coords = aggregated_gb[coords_cols]
        value = self._calculate_metric_from_stats(aggregated_gb[self.header_cols].to_numpy())
        index = aggregated_gb[index_cols]
        return SurveyAttribute.construct_map(self, coords, value, index=index, **kwargs)

    def plot(self, ax, coords, index, sort_by=None, threshold=None, top_ax_y_scale=None, bad_only=False, **kwargs):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows"""
        # TODO: add mask to gather plot
        _ = coords
        gather = self.survey.get_gather(index)
        sort_by = "offset" if sort_by is None else sort_by
        gather = gather.sort(sort_by)
        stats = self.get_mask(gather)
        tracewise_metric = self._calculate_metric_from_stats(stats)
        tracewise_metric[tracewise_metric==0] = np.nan
        if bad_only:
            bin_mask = self.binarize(tracewise_metric, threshold)
            gather.data[self.aggregate(bin_mask) == 0] = np.nan

        gather.plot(ax=ax, top_header=tracewise_metric, **kwargs)
        if threshold is not None:
            if isinstance(threshold, (int, float, np.number)):
                ax.figure.axes[1].axhline(threshold, alpha=0.5, color="blue")
            else:
                ax.figure.axes[1].fill_between(np.arange(gather.n_traces), *threshold, alpha=0.3, color="blue")
        ax.figure.axes[1].set_yscale(self.top_ax_y_scale if top_ax_y_scale is None else top_ax_y_scale)
        self._plot(ax=ax, gather=gather)

    @staticmethod
    def _calculate_metric_from_stats(stats):
        raise NotImplementedError

    def _plot(self, ax, gather):
        """Add any additional metric related graphs on plot"""
        pass


class WindowRMS(BaseWindowMetric):
    """Computes traces RMS for provided window by offsets and times.

    Parameters
    ----------
    offsets : tuple of 2 ints, optional, defaults to gather length
        Offset range to use for calcualtion.
    times : tuple of 2 ints, optional, defaults to gather length
        Time range to use for calcualtion, measured in ms.
    name : str, optional, defaults to "rms"
        Metrics name.
    """
    name = "rms"
    is_lower_better = False # TODO: think what should it be?
    # What treshold to use? Leave it none?
    threshold = None

    def __init__(self, offsets=None, times=None, name=None):
        super().__init__(name=name)
        self.offsets = offsets
        self.times = times

    def __repr__(self):
        """String representation of the metric."""
        return f"{type(self).__name__}(name='{self.name}', offsets='{self.offsets}', times='{self.times}')"

    @property
    def header_cols(self):
        """Column names in survey.headers to srote metrics results."""
        return [self.name+"_sum", self.name+"_n"]

    @staticmethod
    def _get_time_ixs(gather, times):
        if times is None:
            return (min(gather.samples), max(gather.samples))
        times = np.asarray([max(gather.samples[0], times[0]), min(gather.samples[-1], times[1])])
        return times_to_indices(times, gather.samples).astype(np.int16)

    @staticmethod
    def _get_offsets(gather, offsets):
        return (min(gather.offsets), max(gather.offsets)) if offsets is None else offsets

    def get_mask(self, gather):
        """QC indicator implementation."""
        times = self._get_time_ixs(gather, self.times)
        offsets = self._get_offsets(gather, self.offsets)

        window_ixs = np.nonzero((gather.offsets >= offsets[0]) & (gather.offsets <= offsets[1]))[0]
        start_ixs = np.full(len(window_ixs), fill_value=times[0])
        end_ixs = np.full(len(window_ixs), fill_value=times[1])
        result = np.full((gather.data.shape[0], 2), fill_value=np.nan)
        result[window_ixs] = self.compute_stats_by_ixs(gather.data[window_ixs], (start_ixs, ), (end_ixs, ))
        return result

    @staticmethod
    def _calculate_metric_from_stats(stats):
        return stats[:, 0] / stats[:, 1]

    def _plot(self, ax, gather):
        # TODO: do we want to plot this metric with sort_by != 'offset'?
        times = self._get_time_ixs(gather, self.times)
        offsets = self._get_offsets(gather, self.offsets)

        offs_ind = np.nonzero((gather.offsets >= offsets[0]) & (gather.offsets <= offsets[1]))[0]
        if len(offs_ind) > 0:
            n_rec = (offs_ind[0], times[0]), len(offs_ind), (times[1] - times[0])
            ax.add_patch(patches.Rectangle(*n_rec, linewidth=2, edgecolor='magenta', facecolor='none'))


class SinalToNoiseRMSAdaptive(BaseWindowMetric):
    """Signal to Noise RMS ratio computed in sliding windows along provided refractor velocity.
    RMS will be computed in two windows for every gather:
    1. Window shifted up from refractor velocity by `shift_up` ms. RMS in this window represents the noise value.
    2. WIndow shifted down from refractor velocity by `shift_down` ms`. RMS in this window represents the signal value.

    Only traces that contain noise and signal windows of the provided `window_size` are considered,
    the metric is 0 for other traces.


    Parameters
    ----------
    win_size : int
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

    def __init__(self, win_size, shift_up, shift_down, refractor_velocity, name=None):
        super().__init__(name=name)
        self.win_size = win_size
        self.shift_up = shift_up
        self.shift_down = shift_down
        self.refractor_velocity = refractor_velocity

    def __repr__(self):
        """String representation of the metric."""
        repr_str = f"(name='{self.name}', win_size='{self.win_size}', shift_up='{self.shift_up}', "\
                   f"shift_down='{self.shift_down}', refractor_velocity='{self.refractor_velocity}')"
        return f"{type(self).__name__}" + repr_str

    @property
    def header_cols(self):
        """Column names in survey.headers to srote metrics results."""
        return [self.name + postfix for postfix in ["_signal_sum", "_signal_n", "_noise_sum", "_noise_n"]]

    def _get_indices(self, gather):
        """Convert times to use for noise and signal windows into indices"""
        fbp = self.refractor_velocity(gather.offsets)

        signal_start_times = fbp + self.shift_down
        signal_end_times = np.clip(signal_start_times + self.win_size, None, gather.samples[-1])

        noise_end_times = fbp - self.shift_up
        noise_start_times = np.clip(noise_end_times - self.win_size, 0, None)

        signal_mask = signal_start_times > gather.samples[-1]
        noise_mask = noise_end_times < 0
        mask = signal_mask | noise_mask

        signal_start_ixs = times_to_indices(signal_start_times, gather.samples).astype(np.int16)
        signal_end_ixs = times_to_indices(signal_end_times, gather.samples).astype(np.int16)
        noise_start_ixs = times_to_indices(noise_start_times, gather.samples).astype(np.int16)
        noise_end_ixs = times_to_indices(noise_end_times, gather.samples).astype(np.int16)

        # Avoiding dividing signal rms by zero and optimize computations a little
        signal_start_ixs[mask] = -1
        signal_end_ixs[mask] = -1

        noise_start_ixs[mask] = -1
        noise_end_ixs[mask] = -1

        return signal_start_ixs, signal_end_ixs, noise_start_ixs, noise_end_ixs

    def get_mask(self, gather):
        """QC indicator implementation. See `plot` docstring for parameters descriptions."""
        ssi, sei, nsi, nei = self._get_indices(gather)
        return self.compute_stats_by_ixs(gather.data, (ssi, nsi), (sei, nei))

    @staticmethod
    def _calculate_metric_from_stats(stats):
        return (stats[:, 0] / stats[:, 1] + 1e-10) / (stats[:, 2] / stats[:, 3] + 1e-10)

    def _plot(self, ax, gather):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows."""
        indices = self._get_indices(gather)
        indices = np.where(np.asarray(indices) == -1, np.nan, indices)

        ax.plot(np.arange(gather.n_traces), indices[0], color='lime')
        ax.plot(np.arange(gather.n_traces), indices[1], color='lime')
        ax.plot(np.arange(gather.n_traces), indices[2], color='magenta')
        ax.plot(np.arange(gather.n_traces), indices[3], color='magenta')

DEFAULT_TRACEWISE_METRICS = [TraceAbsMean, TraceMaxAbs, MaxClipsLen, MaxConstLen, DeadTrace, WindowRMS]
