"""Implements survey metrics"""

from functools import partial

from numba import njit, prange
import numpy as np
from scipy import signal
from matplotlib import patches

from ..metrics import Metric
from ..const import EPS
from ..gather.utils import times_to_indices

class SurveyAttribute(Metric):
    """A utility metric class that reindexes given survey by `coords_cols` and allows for plotting gathers by their
    coordinates. Does not implement any calculation logic."""
    def __init__(self, survey, coords_cols, **kwargs):
        super().__init__(**kwargs)
        self.survey = survey.reindex(coords_cols)

    def plot(self, coords, ax, sort_by=None, **kwargs):
        """Plot a gather by given `coords`. Optionally sort it."""
        gather = self.survey.get_gather(coords)
        if sort_by is not None:
            gather = gather.sort(by=sort_by)
        gather.plot(ax=ax, **kwargs)

    def get_views(self, sort_by=None, **kwargs):
        """Return a single view, that plots a gather sorted by `sort_by` by click coordinates."""
        return [partial(self.plot, sort_by=sort_by)], kwargs


class TracewiseMetric(Metric):
    """Base class for tracewise metrics with addidional plotters and aggregations
    Child classes should redefine `_get_res` method, and optionnaly `preprocess`.
    """

    min_value = None
    max_value = None
    is_lower_better = None

    views = ("plot_image", "plot_wiggle")

    threshold = None
    top_ax_y_scale = 'linear'

    params = []

    def __init__(self, survey=None, coords_cols=None, **kwargs):
        super().__init__(**kwargs)
        if survey is not None and coords_cols is not None:
            self.survey = survey.reindex(coords_cols)

    @property
    def kwargs(self):
        """returns metric kwargs"""
        return {'threshold': self.threshold, 'top_ax_y_scale': self.top_ax_y_scale,
                **{name: getattr(self, name) for name in self.params}}


    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation.
        Takes a gather as an argument and returns either a samplewise qc indicator with shape
        (`gather.n_traces`, `gather.n_samples - d`), where `d >= 0`,
        or a tracewize indicator with shape (`gather.n_traces`,).
        For a 2d output with 2-d axis smaller than `gather.n_samples`,
        it will be padded with zeros at the beggining in `get_res`.
        """
        raise NotImplementedError

    @classmethod
    def preprocess(cls, gather, **kwargs):
        """Preprocess gather for calculating metric. Identity by default."""
        _ = kwargs
        return gather

    @classmethod
    def get_res(cls, gather, **kwargs):
        """Return QC indicator with zero traces masked with `np.nan`
        and output shape either `gater.data.shape`, or (`gather.n_traces`,)."""

        gather = cls.preprocess(gather, **kwargs)

        res = cls._get_res(gather, **kwargs)

        if res.ndim == 2 and res.shape[1] != 1:
            res = np.pad(res, pad_width=((0, 0), (gather.n_samples - res.shape[1], 0)))

        return res

    @staticmethod
    def get_extremum(res, axis=None):
        """Get the value that deviates the most from the mean"""
        return np.nanmax((res - np.nanmean(res, axis=axis)).abs(), axis=axis)

    @classmethod
    def aggregate(cls, res, tracewise=False):
        """Aggregate input depending on `cls.is_lower_better`

        Parameters
        ----------
        res : np.array
            input 1d or 2d array
        tracewise : bool, optional, defaults to False
            whether to return tracewise values, or to aggregate result for the whole gather.

        Returns
        -------
        np.array or float
            aggregated result for the whole gather, or an array of values for each trace
        """
        if cls.is_lower_better is None:
            fn = cls.get_extremum
        else:
            fn = np.nanmax if cls.is_lower_better else np.nanmin

        if tracewise:
            return res if res.ndim == 1 else fn(res, axis=1)

        return fn(res)

    @classmethod
    def calc(cls, gather, tracewise=False, **kwargs):  # pylint: disable=arguments-renamed
        """Return an already calculated metric."""
        res = cls.get_res(gather, **kwargs)
        return cls.aggregate(res, tracewise=tracewise)

    def plot_image(self, coords, ax, **kwargs):
        """Gather plot where samples with indicator above/below `cls.threshold` are highlited."""
        self._plot('seismogram', coords, ax, **kwargs)

    def plot_wiggle(self, coords, ax, **kwargs):
        """"Gather wiggle plot where samples with indicator above/below `cls.threshold` are highlited."""
        self._plot('wiggle', coords, ax, **kwargs)

    def _plot(self, mode, coords, ax, **kwargs):
        """Gather plot, mode-dependent"""
        gather = self.survey.get_gather(coords)

        self._plot_gather_metric(mode, gather, ax, **kwargs)

        if self.threshold is None or self.is_lower_better is None:
            return

        top_ax, bottom_ax = ax.figure.axes[1], ax.figure.axes[0]

        top_ax.axhline(self.threshold, alpha=0.5)

        mask = self.get_res(gather, **self.kwargs)
        fn = np.greater_equal if self.is_lower_better else np.less_equal
        mask = fn(mask, self.threshold)

        if np.any(mask):
            self._plot_mask(mode, gather, mask, bottom_ax, **kwargs)

    def _plot_gather_metric(self, mode, gather, ax, **kwargs):
        """Plot gather and metric values on a top plot"""

        metric_vals = self.calc(gather, tracewise=True, **self.kwargs)

        gather = self.preprocess(gather, **self.kwargs)
        gather.plot(ax=ax, mode=mode, top_header=metric_vals, **kwargs)

        ax.figure.axes[1].set_yscale(self.top_ax_y_scale)

    def _plot_mask(self, mode, gather, mask, ax, eps=EPS, **kwargs):
        """Highlight metric values above/below `cls.threshold` """

        # tracewise metric
        if mask.ndim == 1 or mask.ndim == 2 and mask.shape[1] == 1:
            mask.squeeze()

            if mode == 'wiggle':
                yrange = np.arange(gather.n_samples)

                beg = 0 if mask[0] else None

                for i, val in enumerate(mask):
                    if val:
                        if beg is None:
                            beg = i
                    else:
                        if beg is not None:
                            ax.fill_betweenx(yrange, beg - 0.5, i - 1 + 0.5, color='red', alpha=0.1)
                            beg = None

                if beg is not None:
                    ax.fill_betweenx(yrange, beg - 0.5, len(mask) - 1 + 0.5, color='red', alpha=0.1)
            else:
                blurred = signal.fftconvolve(mask.astype(np.int16), np.ones(5), mode='same')
                gather.data[blurred <= eps] = np.nan
                gather.plot(ax=ax, mode='seismogram', cmap='Reds', **kwargs)

            return

        # samplewise metric
        if mode == 'wiggle':
            mask[:, 1:-1] = (mask[:, 1:-1] | mask[:, 2:] | mask[:, :-2])
            gather.data = mask.astype(np.float32)
            gather.data[~(mask.any(axis=1))] = np.nan
            gather.plot(ax=ax, mode='wiggle', alpha=0.1, color='red', **kwargs)
        else:
            blurred = self._blur_mask(mask.astype(np.int16), eps=eps)
            gather.data = blurred
            gather.plot(ax=ax, mode='seismogram', alpha=0.2, cmap='Reds', **kwargs)

    @staticmethod
    def _blur_mask(flt, eps=EPS):
        """Blure filter values"""
        if np.any(flt == 1):
            win_size = np.floor(min((np.prod(flt.shape) / np.sum(flt == 1)), np.min(flt.shape) / 10)).astype(np.int16)

            if win_size > 4:
                kernel_1d = signal.windows.gaussian(win_size, win_size//2)
                kernel = np.outer(kernel_1d, kernel_1d)
                flt = signal.fftconvolve(flt, kernel, mode='same')
                flt[flt < eps] = 0

        return flt


class Spikes(TracewiseMetric):
    """Spikes detection."""
    name = "spikes"
    min_value = 0
    max_value = None
    is_lower_better = True
    threshold = 2
    top_ax_y_scale = 'log'

    params = ['muter']

    @classmethod
    def preprocess(cls, gather, muter, **kwargs):
        _ = kwargs
        return gather.copy().mute(muter=muter, fill_value=np.nan).scale_standard()

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        traces = gather.data
        cls.fill_leading_nulls(traces)

        running_mean = (traces[:, 1:-1] + traces[:, 2:] + traces[:, :-2])/3
        res = np.abs(traces[:, 1:-1] - running_mean)

        return np.pad(res, ((0, 0), (1, 1)))

    @staticmethod
    @njit
    def fill_leading_nulls(arr):
        """"Fill leading null values of array's row with the first non null value in a row."""

        n_samples = arr.shape[1]

        for i in range(arr.shape[0]):
            nan_indices = np.nonzero(np.isnan(arr[i]))[0]
            if len(nan_indices) > 0:
                j = nan_indices[-1] + 1
                if j < n_samples:
                    arr[i, :j] = arr[i, j]


class Autocorr(TracewiseMetric):
    """Autocorrelation with shift 1"""
    name = "autocorr"
    is_lower_better = False
    threshold = 0.9
    top_ax_y_scale = 'log'

    @classmethod
    def preprocess(cls, gather, muter, **kwargs):
        _ = kwargs
        return gather.copy().mute(muter=muter, fill_value=np.nan).scale_standard()

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        return np.nanmean(gather.data[..., 1:] * gather.data[..., :-1], axis=1)


class TraceAbsMean(TracewiseMetric):
    """Absolute value of the trace's mean scaled by trace's std."""
    name = "trace_absmean"
    is_lower_better = True
    threshold = 0.1
    top_ax_y_scale = 'log'

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        return np.abs(gather.data.mean(axis=1) / (gather.data.std(axis=1) + EPS))


class TraceMaxAbs(TracewiseMetric):
    """Maximun absolute amplitude value scaled by trace's std."""
    name = "trace_maxabs"
    is_lower_better = True
    threshold = None
    top_ax_y_scale = 'log'

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        return np.max(np.abs(gather.data), axis=1) / (gather.data.std(axis=1) + EPS)


class MaxClipsLen(TracewiseMetric):
    """Detecting minimum/maximun clips"""
    name = "max_clips_len"
    min_value = 1
    max_value = None
    is_lower_better = True
    threshold = 3

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        traces = gather.data

        maxes = traces.max(axis=-1, keepdims=True)
        mins = traces.min(axis=-1, keepdims=True)

        res_plus = cls.get_val_subseq(traces, maxes)
        res_minus = cls.get_val_subseq(traces, mins)

        return (res_plus + res_minus).astype(np.float32)

    @staticmethod
    @njit(nogil=True)
    def get_val_subseq(traces, cmpval):
        """Indicator of constant subsequences equal to given value."""

        old_shape = traces.shape
        traces = np.atleast_2d(traces)

        indicators = (traces == cmpval).astype(np.int16)

        for t, indicator in enumerate(indicators):
            counter = 0
            for i, sample in enumerate(indicator):
                if sample == 1:
                    counter += 1
                else:
                    if counter > 1:
                        indicators[t, i - counter: i] = counter
                    counter = 0

            if counter > 1:
                indicators[t, -counter:] = counter

        return indicators.reshape(*old_shape)


class MaxConstLen(TracewiseMetric):
    """Detecting constant subsequences"""
    name = "const_len"
    is_lower_better = True
    threshold = 4

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        res = cls.get_const_subseq(gather.data)
        return res.astype(np.float32)

    @staticmethod
    @njit(nogil=True)
    def get_const_subseq(traces):
        """Indicator of constant subsequences."""

        old_shape = traces.shape
        traces = np.atleast_2d(traces)

        indicators = np.zeros_like(traces, dtype=np.int16)
        indicators[:, 1:] = (traces[:, 1:] == traces[:, :-1]).astype(np.int16)
        for t, indicator in enumerate(indicators):
            counter = 0
            for i, sample in enumerate(indicator):
                if sample == 1:
                    counter += 1
                else:
                    if counter > 0:
                        indicators[t, i - counter - 1:i] = counter + 1
                    counter = 0

            if counter > 0:
                indicators[t, -counter - 1:] = counter + 1

        return indicators.reshape(*old_shape)


class Std(TracewiseMetric):
    """Tracewise std"""
    name = "std"
    top_ax_y_scale = 'log'

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """QC indicator implementation."""
        _ = kwargs
        return gather.data.std(axis=1)


class DeadTrace(TracewiseMetric):  # pylint: disable=abstract-method
    """Detects constant traces."""
    name = "DeadTrace"
    min_value = 0
    max_value = 1
    is_lower_better = True
    threshold = 0.5

    @classmethod
    def _get_res(cls, gather, **kwargs):
        """Return QC indicator."""
        _ = kwargs
        return (np.max(gather.data, axis=1) - np.min(gather.data, axis=1) < EPS).astype(np.float32)


class WindowRMS(TracewiseMetric):
    """ RMS computed for provided windows

    Parameters
    ----------
    offsets : tuple of 2 ints
        offset range to use for calcualtion.
    times : tuple of 2 ints
        time range to use for calcualtion, measured in ms.
    """
    name = "RMS"
    is_lower_better = False
    threshold = None
    top_ax_y_scale = 'log'

    params = ['offsets', 'times']

    def __init__(self, survey=None, coords_cols=None, offsets=None, times=None, **kwargs):
        super().__init__(survey, coords_cols, offsets=offsets, times=times, **kwargs)

    @classmethod
    def _get_res(cls, gather, offsets, times, **kwargs):
        """QC indicator implementation."""
        _ = kwargs

        times = cls._get_times(gather, times)
        offsets = cls._get_offsets(gather, offsets)

        w_beg, w_end = cls._get_indices(gather, times)
        w_begs = np.full(gather.n_traces, fill_value=w_beg, dtype=np.int16)
        w_ends = np.full(gather.n_traces, fill_value=w_end, dtype=np.int16)

        mask = ((gather.offsets >= offsets[0]) & (gather.offsets <= offsets[1]))
        w_begs[~mask] = -1
        w_ends[~mask] = -1

        return cls.rms_win(gather.data, w_begs, w_ends)

    @staticmethod
    def _get_times(gather, times):
        return times if times is not None else (min(gather.samples), max(gather.samples))

    @staticmethod
    def _get_offsets(gather, offsets):
        return offsets if offsets is not None else (min(gather.offsets), max(gather.offsets))

    @staticmethod
    @njit(nogil=True)
    def rms_win(data, w_begs, w_ends):
        """Compute RMS for a window defined by its starting and end sample indices."""
        res = np.full(data.shape[0], fill_value=np.nan)

        for i, (trace, w_beg, w_end) in enumerate(zip(data, w_begs, w_ends)):
            if w_beg >= 0 and w_end > 0:
                res[i] = np.sqrt(np.mean(trace[w_beg:w_end]**2))
        return res

    @staticmethod
    def _get_indices(gather, times):
        return times_to_indices(np.asarray([max(gather.samples[0], times[0]), min(times[-1], gather.samples[-1])]),
                                gather.samples).astype(np.int16)

    def _plot(self, mode, coords, ax, **kwargs):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows"""
        gather = self.survey.get_gather(coords).sort(by='offset')

        times = self._get_times(gather, self.times)
        offsets = self._get_offsets(gather, self.offsets)

        self._plot_gather_metric(mode, gather, ax, **kwargs)

        mask = (gather.offsets >= offsets[0]) & (gather.offsets <= offsets[1])
        offs_ind = np.nonzero(mask)[0]
        w_beg, w_end = self._get_indices(gather, times)

        n_rec = (offs_ind[0], w_beg), len(offs_ind), (w_end - w_beg)
        ax.add_patch(patches.Rectangle(*n_rec, linewidth=1, edgecolor='magenta', facecolor='none'))


class SinalToNoiseRMSAdaptive(TracewiseMetric):
    """Signal to Noise RMS ratio computed in sliding windows along first breaks.
    The Metric parameters are: window size that is used for both signal and noise windows,
    and the shifts of the windows from from the first breaks picking.
    Noise window beginnings are computed as fbp_time - shift_up - window_size,
    Signal windows beginnings are computed as fbp_time + shift_down.
    Only traces that contain noise and signal windows of the provided `window_size` are considered,
    the metric is Null for other traces.

    TODO use refractor velocity

    Parameters
    ----------
    win_size : int
        length of the windows for computing signam and noise RMS amplitudes measured in ms.
    shift_up : int
        the delta between noise window end and first breaks, measured in ms.
    shift_down : int
        the delta between signal window beginning and first breaks, measured in ms.
    first_breaks_col : str, optional
        header with first breaks, by default HDR_FIRST_BREAK
    """

    name = "RMS_Ratio_Adaptive"
    is_lower_better = False
    threshold = None
    top_ax_y_scale = 'log'

    params = ['win_size', 'shift_up', 'shift_down', 'refractor_velocity']

    @staticmethod
    def _get_indices(gather, win_size, shift_up, shift_down, refractor_velocity, **kwargs):
        """Convert times to use for noise and signal windows into indices"""
        _ = kwargs

        fbp = refractor_velocity(gather.offsets)

        noise_beg = fbp - shift_up - win_size
        noise_beg[noise_beg < 0] = np.nan

        signal_beg = fbp + shift_down
        signal_beg[signal_beg > gather.samples[-1] - win_size] = np.nan

        s_begs = times_to_indices(signal_beg, gather.samples)
        n_begs = times_to_indices(noise_beg, gather.samples)

        return n_begs, s_begs

    @classmethod
    def _get_res(cls, gather, win_size, shift_up, shift_down, refractor_velocity, **kwargs):
        """QC indicator implementation. See `plot` docstring for parameters descriptions."""
        _ = kwargs

        n_begs, s_begs = cls._get_indices(gather, win_size, shift_up, shift_down, refractor_velocity)

        s_begs[np.isnan(s_begs)] = -1
        n_begs[np.isnan(n_begs)] = -1

        win_size = np.rint(win_size/gather.sample_rate).astype(np.int16)

        return cls.rms_2_windows_ratio(gather.data, n_begs.astype(np.int16), s_begs.astype(np.int16), win_size)

    @staticmethod
    @njit(nogil=True)
    def rms_2_windows_ratio(data, n_begs, s_begs, win_size):
        """Compute RMS ratio for 2 windows defined by their starting samples and window size."""
        res = np.full(data.shape[0], fill_value=np.nan, dtype=np.float32)

        for i in prange(len(res)):  # pylint: disable=not-an-iterable
            trace, n_beg, s_beg = data[i], n_begs[i], s_begs[i]
            if n_beg >= 0 and s_beg >= 0:
                sig = trace[s_beg:s_beg + win_size]
                noise = trace[n_beg:n_beg + win_size]
                res[i] = np.sqrt(np.mean(sig**2)) / (np.sqrt(np.mean(noise**2)) + EPS)
        return res

    def _plot(self, mode, coords, ax, **kwargs):
        """Gather plot sorted by offset with tracewise indicator on a separate axis and signal and noise windows."""
        gather = self.survey.get_gather(coords).sort(by='offset')

        self._plot_gather_metric(mode, gather, ax, **kwargs)

        n_begs, s_begs = self._get_indices(gather, **self.kwargs)
        n_begs[np.isnan(s_begs)] = np.nan
        s_begs[np.isnan(n_begs)] = np.nan

        win_size = np.rint(self.win_size/gather.sample_rate)

        ax.plot(np.arange(gather.n_traces), n_begs, color='magenta')
        ax.plot(np.arange(gather.n_traces), n_begs + win_size, color='magenta')
        ax.plot(np.arange(gather.n_traces), s_begs, color='lime')
        ax.plot(np.arange(gather.n_traces), s_begs + win_size, color='lime')
