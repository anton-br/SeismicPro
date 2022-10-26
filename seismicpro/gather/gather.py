"""Implements Gather class that represents a group of seismic traces that share some common acquisition parameter"""

import os
import warnings
from textwrap import dedent

import cv2
import scipy
import segyio
import numpy as np
from scipy.signal import firwin
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from mpl_toolkits.axes_grid1 import make_axes_locatable

from .cropped_gather import CroppedGather
from .plot_corrections import NMOCorrectionPlot, LMOCorrectionPlot
from .utils import correction, normalization, gain
from .utils import convert_times_to_mask, convert_mask_to_pick, times_to_indices, mute_gather, make_origins
from ..utils import (to_list, get_coords_cols, set_ticks, format_subplot_yticklabels, set_text_formatting,
                     add_colorbar, piecewise_polynomial, Coordinates)
from ..containers import TraceContainer, SamplesContainer
from ..semblance import Semblance, ResidualSemblance
from ..muter import Muter, MuterField
from ..stacking_velocity import StackingVelocity, StackingVelocityField
from ..refractor_velocity import RefractorVelocity, RefractorVelocityField
from ..decorators import batch_method, plotter
from ..const import HDR_FIRST_BREAK, DEFAULT_SDC_VELOCITY


class Gather(TraceContainer, SamplesContainer):
    """A class representing a single seismic gather.

    A gather is a collection of seismic traces that share some common acquisition parameter (same index value of the
    generating survey header in our case). Unlike `Survey`, `Gather` instance stores loaded seismic traces along with
    a corresponding subset of its parent survey header.

    `Gather` instance is generally created by calling one of the following methods of a `Survey`, `SeismicIndex` or
    `SeismicDataset`:
    1. `sample_gather` - to get a randomly selected gather,
    2. `get_gather` - to get a particular gather by its index value.

    Most of the methods change gather data inplace, thus `Gather.copy` may come in handy to keep the original gather
    available.

    Examples
    --------
    Let's load a randomly selected common source gather, sort it by offset and plot:
    >>> survey = Survey(path, header_index="FieldRecord", header_cols=["TraceNumber", "offset"], name="survey")
    >>> gather = survey.sample_gather().sort(by="offset")
    >>> gather.plot()

    Parameters
    ----------
    headers : pd.DataFrame
        A subset of parent survey header with common index value defining the gather.
    data : 2d np.ndarray
        Trace data of the gather with (num_traces, trace_length) layout.
    samples : 1d np.ndarray of floats
        Recording time for each trace value. Measured in milliseconds.
    survey : Survey
        A survey that generated the gather.

    Attributes
    ----------
    headers : pd.DataFrame
        A subset of parent survey header with common index value defining the gather.
    data : 2d np.ndarray
        Trace data of the gather with (num_traces, trace_length) layout.
    samples : 1d np.ndarray of floats
        Recording time for each trace value. Measured in milliseconds.
    sample_rate : float
        Sample rate of seismic traces. Measured in milliseconds.
    survey : Survey
        A survey that generated the gather.
    sort_by : None or str
        Headers column that was used for gather sorting. If `None`, no sorting was performed.
    """
    def __init__(self, headers, data, samples, survey):
        self.headers = headers
        self.data = data
        self.samples = samples
        self.survey = survey
        self.sort_by = None

    @property
    def index(self):
        """int or tuple of int or None: Common value of `Survey`'s `header_index` that define traces of the gather.
        `None` if the gather is combined.
        """
        indices = self.headers.index.drop_duplicates()
        if len(indices) != 1:
            return None
        return indices[0]

    @property
    def sample_rate(self):
        """"float: Sample rate of seismic traces. Measured in milliseconds."""
        sample_rate = np.unique(np.diff(self.samples))
        if len(sample_rate) == 1:
            return sample_rate.item()
        raise ValueError("`sample_rate` is not defined, since `samples` are not regular.")

    @property
    def offsets(self):
        """1d np.ndarray of floats: The distance between source and receiver for each trace. Measured in meters."""
        return self["offset"].ravel()

    @property
    def shape(self):
        """tuple with 2 elements: The number of traces in the gather and trace length in samples."""
        return self.data.shape

    @property
    def coords(self):
        """Coordinates or None: Spatial coordinates of the gather. Headers to extract coordinates from are determined
        automatically by the `indexed_by` attribute of the gather. `None` if the gather is indexed by unsupported
        headers or required coords headers were not loaded or coordinates are non-unique for traces of the gather."""
        try:
            coords_cols = get_coords_cols(self.indexed_by)  # Possibly unknown coordinates for indexed_by
            coords = self[coords_cols]  # Required coords headers may not be loaded
        except KeyError:
            return None
        if (coords != coords[0]).any():  # Non-unique coordinates
            return None
        return Coordinates(coords[0], names=coords_cols)

    def __getitem__(self, key):
        """Either select gather headers values by their names or create a new `Gather` with specified traces and
        samples depending on the key type.

        Notes
        -----
        1. If the data after `__getitem__` is no longer sorted, `sort_by` attribute in the resulting `Gather` will be
        set to `None`.
        2. If headers selection is performed, a 2d array is always returned even for a single header.

        Parameters
        ----------
        key : str, list of str, int, list, tuple, slice
            If str or list of str, gather headers to get as a 2d np.ndarray.
            Otherwise, indices of traces and samples to get. In this case, __getitem__ behavior almost coincides with
            np.ndarray indexing and slicing except for cases, when resulting ndim is not preserved or joint indexation
            of gather attributes becomes ambiguous (e.g. gather[[0, 1], [0, 1]]).

        Returns
        -------
        result : 2d np.ndarray or Gather
            Headers values or Gather with a specified subset of traces and samples.

        Raises
        ------
        ValueError
            If the resulting gather is empty, or data ndim has changed, or joint attribute indexation is ambiguous.
        """
        # If key is str or array of str, treat it as names of headers columns
        keys_array = np.array(to_list(key))
        if keys_array.dtype.type == np.str_:
            return super().__getitem__(key)

        # Perform traces and samples selection
        key = (key, ) if not isinstance(key, tuple) else key
        key = key + (slice(None), ) if len(key) == 1 else key
        indices = ()
        for axis_indexer, axis_shape in zip(key, self.shape):
            if isinstance(axis_indexer, (int, np.integer)):
                # Convert negative array index to a corresponding positive one
                axis_indexer %= axis_shape
                # Switch from simple indexing to a slice to keep array dims
                axis_indexer = slice(axis_indexer, axis_indexer+1)
            elif isinstance(axis_indexer, tuple):
                # Force advanced indexing for `samples`
                axis_indexer = list(axis_indexer)
            indices = indices + (axis_indexer, )

        data = self.data[indices]
        if data.ndim != 2:
            raise ValueError("Data ndim is not preserved or joint indexation of gather attributes becomes ambiguous "
                             "after indexation")
        if data.size == 0:
            raise ValueError("Empty gather after indexation")

        # Set indexed data attribute. Make it C-contiguous since otherwise some numba functions may fail
        new_self = self.copy(ignore=['data', 'headers', 'samples'])
        new_self.data = np.ascontiguousarray(data, dtype=self.data.dtype)

        # The two-element `indices` tuple describes indices of traces and samples to be obtained respectively
        new_self.headers = self.headers.iloc[indices[0]]
        new_self.samples = self.samples[indices[1]]

        # Check that `sort_by` still represents the actual trace sorting as it might be changed during getitem.
        if new_self.sort_by is not None and not new_self.headers[new_self.sort_by].is_monotonic_increasing:
            new_self.sort_by = None
        return new_self

    def __str__(self):
        """Print gather metadata including information about its survey, headers and traces."""
        # Calculate offset range
        offsets = self.headers.get('offset')
        offset_range = f'[{np.min(offsets)} m, {np.max(offsets)} m]' if offsets is not None else None

        # Format gather coordinates
        coords = self.coords
        coords_str = "Unknown" if coords is None else str(coords)

        # Count the number of zero/constant traces
        n_dead_traces = np.isclose(np.max(self.data, axis=1), np.min(self.data, axis=1)).sum()

        msg = f"""
        Parent survey path:          {self.survey.path}
        Parent survey name:          {self.survey.name}

        Indexed by:                  {', '.join(to_list(self.indexed_by))}
        Index value:                 {'Combined' if self.index is None else self.index}
        Gather coordinates:          {coords_str}
        Gather sorting:              {self.sort_by}

        Number of traces:            {self.n_traces}
        Trace length:                {self.n_samples} samples
        Sample rate:                 {self.sample_rate} ms
        Times range:                 [{min(self.samples)} ms, {max(self.samples)} ms]
        Offsets range:               {offset_range}

        Gather statistics:
        Number of dead traces:       {n_dead_traces}
        mean | std:                  {np.mean(self.data):>10.2f} | {np.std(self.data):<10.2f}
         min | max:                  {np.min(self.data):>10.2f} | {np.max(self.data):<10.2f}
         q01 | q99:                  {self.get_quantile(0.01):>10.2f} | {self.get_quantile(0.99):<10.2f}
        """
        return dedent(msg).strip()

    def info(self):
        """Print gather metadata including information about its survey, headers and traces."""
        print(self)

    @batch_method(target='threads', copy_src=False)
    def copy(self, ignore=None):
        """Perform a deepcopy of all gather attributes except for `survey` and those specified in ignore, which are
        kept unchanged.

        Parameters
        ----------
        ignore : str or array of str, defaults to None
            Attributes that won't be copied.

        Returns
        -------
        copy : Gather
            Copy of the gather.
        """
        ignore = set() if ignore is None else set(to_list(ignore))
        return super().copy(ignore | {"survey"})

    @batch_method(target='for')
    def get_item(self, *args):
        """An interface for `self.__getitem__` method."""
        return self[args if len(args) > 1 else args[0]]

    def _post_filter(self, mask):
        """Remove traces from gather data that correspond to filtered headers after `Gather.filter`."""
        self.data = self.data[mask]

    #------------------------------------------------------------------------#
    #                              Dump methods                              #
    #------------------------------------------------------------------------#

    @batch_method(target='for', force=True)
    def dump(self, path, name=None, retain_parent_segy_headers=True):
        """Save the gather to a `.sgy` file.

        Notes
        -----
        1. All textual and almost all binary headers are copied from the parent SEG-Y file unchanged except for the
           following binary header fields that are inferred by the current gather:
           1) Sample rate, bytes 3217-3218, called `Interval` in `segyio`,
           2) Number of samples per data trace, bytes 3221-3222, called `Samples` in `segyio`,
           3) Extended number of samples per data trace, bytes 3269-3272, called `ExtSamples` in `segyio`.
        2. Bytes 117-118 of trace header (called `TRACE_SAMPLE_INTERVAL` in `segyio`) for each trace is filled with
           sample rate of the current gather.

        Parameters
        ----------
        path : str
            The directory to dump the gather in.
        name : str, optional, defaults to None
            The name of the file. If `None`, the concatenation of the survey name and the value of gather index will
            be used.
        retain_parent_segy_headers : bool, optional, defaults to True
            Whether to copy the headers that weren't loaded during `Survey` creation from the parent SEG-Y file.

        Returns
        -------
        self : Gather
            Gather unchanged.

        Raises
        ------
        ValueError
            If empty `name` was specified.
        """
        parent_handler = self.survey.segy_handler

        if name is None:
            # Use the first value of gather index to handle combined case
            name = "_".join(map(str, [self.survey.name] + to_list(self.headers.index.values[0])))
        if name == "":
            raise ValueError("Argument `name` can not be empty.")
        if not os.path.splitext(name)[1]:
            name += ".sgy"
        full_path = os.path.join(path, name)

        os.makedirs(path, exist_ok=True)
        # Create segyio spec. We choose only specs that relate to unstructured data.
        spec = segyio.spec()
        spec.samples = self.samples
        spec.ext_headers = parent_handler.ext_headers
        spec.format = parent_handler.format
        spec.tracecount = self.n_traces

        sample_rate = np.int32(self.sample_rate * 1000) # Convert to microseconds
        # Remember ordinal numbers of traces in the parent SEG-Y file to further copy their headers
        trace_ids = self["TRACE_SEQUENCE_FILE"].ravel() - 1

        # Keep only headers, defined by SEG-Y standard.
        used_header_names = (set(to_list(self.indexed_by)) | set(self.headers.columns)) & set(segyio.tracefield.keys)
        used_header_names = to_list(used_header_names)

        # Transform header's names into byte number based on the SEG-Y standard.
        used_header_bytes = [segyio.tracefield.keys[header_name] for header_name in used_header_names]

        with segyio.create(full_path, spec) as dump_handler:
            # Copy the binary header from the parent SEG-Y file and update it with samples data of the gather.
            # TODO: Check if other bin headers matter
            dump_handler.bin = parent_handler.bin
            dump_handler.bin[segyio.BinField.Interval] = sample_rate
            dump_handler.bin[segyio.BinField.Samples] = self.n_samples
            dump_handler.bin[segyio.BinField.ExtSamples] = self.n_samples

            # Copy textual headers from the parent SEG-Y file.
            for i in range(spec.ext_headers + 1):
                dump_handler.text[i] = parent_handler.text[i]

            # Dump traces and their headers. Optionally copy headers from the parent SEG-Y file.
            dump_handler.trace = self.data
            for i, trace_headers in enumerate(self[used_header_names]):
                if retain_parent_segy_headers:
                    dump_handler.header[i] = parent_handler.header[trace_ids[i]]
                dump_handler.header[i].update({**dict(zip(used_header_bytes, trace_headers)),
                                               segyio.TraceField.TRACE_SAMPLE_INTERVAL: sample_rate,
                                               segyio.TraceField.TRACE_SEQUENCE_FILE: i + 1})
        return self

    #------------------------------------------------------------------------#
    #                         Normalization methods                          #
    #------------------------------------------------------------------------#

    def _apply_agg_func(self, func, tracewise, **kwargs):
        """Apply a `func` either to entire gather's data or to each trace independently.

        Notes
        -----
        `func` must accept an `axis` argument.

        Parameters
        ----------
        func : callable
            Function to be applied to the gather's data.
        tracewise : bool
            If `True`, the `func` is applied to each trace independently, otherwise to the entire gather's data.
        kwargs : misc, optional
            Additional keyword arguments to `func`.

        Returns
        -------
        result : misc
            The result of the application of the `func` to the gather's data.
        """
        axis = 1 if tracewise else None
        return func(self.data, axis=axis, **kwargs)

    def get_quantile(self, q, tracewise=False, use_global=False):
        """Calculate the `q`-th quantile of the gather or fetch the global quantile from the parent survey.

        Notes
        -----
        The `tracewise` mode is only available when `use_global` is `False`.

        Parameters
        ----------
        q : float or array-like of floats
            Quantile or a sequence of quantiles to compute, which must be between 0 and 1 inclusive.
        tracewise : bool, optional, default False
            If `True`, the quantiles are computed for each trace independently, otherwise for the entire gather.
        use_global : bool, optional, default False
            If `True`, the survey's quantiles are used, otherwise the quantiles are computed for the gather data.

        Returns
        -------
        q : float or array-like of floats
            The `q`-th quantile values.

        Raises
        ------
        ValueError
            If `use_global` is `True` but global statistics were not calculated.
        """
        if use_global:
            return self.survey.get_quantile(q)
        quantiles = self._apply_agg_func(func=np.nanquantile, tracewise=tracewise, q=q).astype(np.float32)
        # return the same type as q in case of global calculation: either single float or array-like
        return quantiles.item() if not tracewise and quantiles.ndim == 0 else quantiles

    @batch_method(target='threads')
    def scale_standard(self, tracewise=True, use_global=False, eps=1e-10):
        r"""Standardize the gather by removing the mean and scaling to unit variance.

        The standard score of a gather `g` is calculated as:
        :math:`G = \frac{g - m}{s + eps}`,
        where:
        `m` - the mean of the gather or global average if `use_global=True`,
        `s` - the standard deviation of the gather or global standard deviation if `use_global=True`,
        `eps` - a constant that is added to the denominator to avoid division by zero.

        Notes
        -----
        1. The presence of NaN values in the gather will lead to incorrect behavior of the scaler.
        2. Standardization is performed inplace.

        Parameters
        ----------
        tracewise : bool, optional, defaults to True
            If `True`, mean and standard deviation are calculated for each trace independently. Otherwise they are
            calculated for the entire gather.
        use_global : bool, optional, defaults to False
            If `True`, parent survey's mean and std are used, otherwise gather statistics are computed.
        eps : float, optional, defaults to 1e-10
            A constant to be added to the denominator to avoid division by zero.

        Returns
        -------
        self : Gather
            Standardized gather.

        Raises
        ------
        ValueError
            If `use_global` is `True` but global statistics were not calculated.
        """
        if use_global:
            if not self.survey.has_stats:
                raise ValueError('Global statistics were not calculated, call `Survey.collect_stats` first.')
            mean = self.survey.mean
            std = self.survey.std
        else:
            mean = self._apply_agg_func(func=np.mean, tracewise=tracewise, keepdims=True)
            std = self._apply_agg_func(func=np.std, tracewise=tracewise, keepdims=True)
        self.data = normalization.scale_standard(self.data, mean, std, np.float32(eps))
        return self

    @batch_method(target='for')
    def scale_maxabs(self, q_min=0, q_max=1, tracewise=True, use_global=False, clip=False, eps=1e-10):
        r"""Scale the gather by its maximum absolute value.

        Maxabs scale of the gather `g` is calculated as:
        :math: `G = \frac{g}{m + eps}`,
        where:
        `m` - the maximum of absolute values of `q_min`-th and `q_max`-th quantiles,
        `eps` - a constant that is added to the denominator to avoid division by zero.

        Quantiles are used to minimize the effect of amplitude outliers on the scaling result. Default 0 and 1
        quantiles represent the minimum and maximum values of the gather respectively and result in usual max-abs
        scaler behavior.

        Notes
        -----
        1. The presence of NaN values in the gather will lead to incorrect behavior of the scaler.
        2. Maxabs scaling is performed inplace.

        Parameters
        ----------
        q_min : float, optional, defaults to 0
            A quantile to be used as a gather minimum during scaling.
        q_max : float, optional, defaults to 1
            A quantile to be used as a gather maximum during scaling.
        tracewise : bool, optional, defaults to True
            If `True`, quantiles are calculated for each trace independently. Otherwise they are calculated for the
            entire gather.
        use_global : bool, optional, defaults to False
            If `True`, parent survey's quantiles are used, otherwise gather quantiles are computed.
        clip : bool, optional, defaults to False
            Whether to clip the scaled gather to the [-1, 1] range.
        eps : float, optional, defaults to 1e-10
            A constant to be added to the denominator to avoid division by zero.

        Returns
        -------
        self : Gather
            Scaled gather.

        Raises
        ------
        ValueError
            If `use_global` is `True` but global statistics were not calculated.
        """
        min_value, max_value = self.get_quantile([q_min, q_max], tracewise=tracewise, use_global=use_global)
        self.data = normalization.scale_maxabs(self.data, min_value, max_value, clip, np.float32(eps))
        return self

    @batch_method(target='for')
    def scale_minmax(self, q_min=0, q_max=1, tracewise=True, use_global=False, clip=False, eps=1e-10):
        r"""Linearly scale the gather to a [0, 1] range.

        The transformation of the gather `g` is given by:
        :math:`G=\frac{g - min}{max - min + eps}`
        where:
        `min` and `max` - `q_min`-th and `q_max`-th quantiles respectively,
        `eps` - a constant that is added to the denominator to avoid division by zero.

        Notes
        -----
        1. The presence of NaN values in the gather will lead to incorrect behavior of the scaler.
        2. Minmax scaling is performed inplace.

        Parameters
        ----------
        q_min : float, optional, defaults to 0
            A quantile to be used as a gather minimum during scaling.
        q_max : float, optional, defaults to 1
            A quantile to be used as a gather maximum during scaling.
        tracewise : bool, optional, defaults to True
            If `True`, quantiles are calculated for each trace independently. Otherwise they are calculated for the
            entire gather.
        use_global : bool, optional, defaults to False
            If `True`, parent survey's quantiles are used, otherwise gather quantiles are computed.
        clip : bool, optional, defaults to False
            Whether to clip the scaled gather to the [0, 1] range.
        eps : float, optional, defaults to 1e-10
            A constant to be added to the denominator to avoid division by zero.

        Returns
        -------
        self : Gather
            Scaled gather.

        Raises
        ------
        ValueError
            If `use_global` is `True` but global statistics were not calculated.
        """
        min_value, max_value = self.get_quantile([q_min, q_max], tracewise=tracewise, use_global=use_global)
        self.data = normalization.scale_minmax(self.data, min_value, max_value, clip, np.float32(eps))
        return self

    #------------------------------------------------------------------------#
    #                    First-breaks processing methods                     #
    #------------------------------------------------------------------------#

    @batch_method(target="threads", copy_src=False)
    def pick_to_mask(self, first_breaks_col=HDR_FIRST_BREAK):
        """Convert first break times to a binary mask with the same shape as `gather.data` containing zeros before the
        first arrivals and ones after for each trace.

        Parameters
        ----------
        first_breaks_col : str, optional, defaults to :const:`~const.HDR_FIRST_BREAK`
            A column of `self.headers` that contains first arrival times, measured in milliseconds.

        Returns
        -------
        gather : Gather
            A new `Gather` with calculated first breaks mask in its `data` attribute.
        """
        mask = convert_times_to_mask(times=self[first_breaks_col].ravel(), samples=self.samples).astype(np.int32)
        gather = self.copy(ignore='data')
        gather.data = mask
        return gather

    @batch_method(target='for', args_to_unpack='save_to')
    def mask_to_pick(self, threshold=0.5, first_breaks_col=HDR_FIRST_BREAK, save_to=None):
        """Convert a first break mask saved in `data` into times of first arrivals.

        For a given trace each value of the mask represents the probability that the corresponding index is greater
        than the index of the first break.

        Notes
        -----
        A detailed description of conversion heuristic used can be found in :func:`~general_utils.convert_mask_to_pick`
        docs.

        Parameters
        ----------
        threshold : float, optional, defaults to 0.5
            A threshold for trace mask value to refer its index to be either pre- or post-first break.
        first_breaks_col : str, optional, defaults to :const:`~const.HDR_FIRST_BREAK`
            Headers column to save first break times to.
        save_to : Gather or str, optional
            An extra `Gather` to save first break times to. Generally used to conveniently pass first break times from
            a `Gather` instance with a first break mask to an original `Gather`.
            May be `str` if called in a pipeline: in this case it defines a component with gathers to save first break
            times to.

        Returns
        -------
        self : Gather
            A gather with first break times in headers column defined by `first_breaks_col`.
        """
        picking_times = convert_mask_to_pick(mask=self.data, samples=self.samples, threshold=threshold)
        self[first_breaks_col] = picking_times
        if save_to is not None:
            save_to[first_breaks_col] = picking_times
        return self

    @batch_method(target='for', use_lock=True)
    def dump_first_breaks(self, path, trace_id_cols=('FieldRecord', 'TraceNumber'), first_breaks_col=HDR_FIRST_BREAK,
                          col_space=8, encoding="UTF-8"):
        """ Save first break picking times to a file.

        Each line in the resulting file corresponds to one trace, where all columns but
        the last one store values from `trace_id_cols` headers and identify the trace
        while the last column stores first break time from `first_breaks_col` header.

        Parameters
        ----------
        path : str
            Path to the file.
        trace_id_cols : tuple of str, defaults to ('FieldRecord', 'TraceNumber')
            Columns names from `self.headers` that act as trace id. These would be present in the file.
        first_breaks_col : str, defaults to :const:`~const.HDR_FIRST_BREAK`
            Column name from `self.headers` where first break times are stored.
        col_space : int, defaults to 8
            The minimum width of each column.
        encoding : str, optional, defaults to "UTF-8"
            File encoding.

        Returns
        -------
        self : Gather
            Gather unchanged
        """
        rows = self[to_list(trace_id_cols) + [first_breaks_col]]

        # SEG-Y specification states that all headers values are integers, but first break values can be float
        row_fmt = '{:{col_space}.0f}' * (rows.shape[1] - 1) + '{:{col_space}.2f}\n'
        fmt = row_fmt * len(rows)
        rows_as_str = fmt.format(*rows.ravel(), col_space=col_space)

        with open(path, 'a', encoding=encoding) as f:
            f.write(rows_as_str)
        return self

    @batch_method(target="for", copy_src=False)  # pylint: disable-next=too-many-arguments
    def calculate_refractor_velocity(self, init=None, bounds=None, n_refractors=None, max_offset=None,
                                     min_velocity_step=1, min_refractor_size=1, loss="L1", huber_coef=20, tol=1e-5,
                                     first_breaks_col=HDR_FIRST_BREAK, **kwargs):
        """Fit a near-surface velocity model by offsets of traces and times of their first breaks.

        Notes
        -----
        Please refer to the :class:`~refractor_velocity.RefractorVelocity` docs for more details about the velocity
        model, its computation algorithm and available parameters. At least one of `init`, `bounds` or `n_refractors`
        should be passed.

        Examples
        --------
        >>> refractor_velocity = gather.calculate_refractor_velocity(n_refractors=2)

        Parameters
        ----------
        init : dict, optional
            Initial values of model parameters.
        bounds : dict, optional
            Lower and upper bounds of model parameters.
        n_refractors : int, optional
            The number of refractors described by the model.
        max_offset : float, optional
            Maximum offset reliably described by the model. Inferred automatically by `offsets`, `init` and `bounds`
            provided but should be preferably explicitly passed.
        min_velocity_step : int, or 1d array-like with shape (n_refractors - 1,), optional, defaults to 1
            Minimum difference between velocities of two adjacent refractors. Default value ensures that velocities are
            strictly increasing.
        min_refractor_size : int, or 1d array-like with shape (n_refractors,), optional, defaults to 1
            Minimum offset range covered by each refractor. Default value ensures that refractors do not degenerate
            into single points.
        loss : str, optional, defaults to "L1"
            Loss function to be minimized. Should be one of "MSE", "huber", "L1", "soft_L1", or "cauchy".
        huber_coef : float, optional, default to 20
            Coefficient for Huber loss function.
        tol : float, optional, defaults to 1e-5
            Precision goal for the value of loss in the stopping criterion.
        first_breaks_col : str, optional, defaults to :const:`~const.HDR_FIRST_BREAK`
            Column name from `self.headers` where times of first break are stored.
        kwargs : misc, optional
            Additional `SLSQP` options, see https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html for
            more details.

        Returns
        -------
        rv : RefractorVelocity
            Constructed near-surface velocity model.
        """
        return RefractorVelocity.from_first_breaks(self.offsets, self[first_breaks_col].ravel(), init, bounds,
                                                   n_refractors, max_offset, min_velocity_step, min_refractor_size,
                                                   loss, huber_coef, tol, coords=self.coords, **kwargs)

    #------------------------------------------------------------------------#
    #                         Gather muting methods                          #
    #------------------------------------------------------------------------#

    @batch_method(target="threads", args_to_unpack="muter")
    def mute(self, muter, fill_value=0):
        """Mute the gather using given `muter` which defines an offset-time boundary above which gather amplitudes will
        be set to `fill_value`.

        Parameters
        ----------
        muter : Muter, MuterField or str
            A muter to use. `Muter` instance is used directly. If `MuterField` instance is passed, a `Muter`
            corresponding to gather coordinates is fetched from it.
            May be `str` if called in a pipeline: in this case it defines a component with muters to apply.
        fill_value : float, optional, defaults to 0
            A value to fill the muted part of the gather with.

        Returns
        -------
        self : Gather
            Muted gather.
        """
        if isinstance(muter, MuterField):
            muter = muter(self.coords)
        if not isinstance(muter, Muter):
            raise ValueError("muter must be of Muter or MuterField type")
        self.data = mute_gather(gather_data=self.data, muting_times=muter(self.offsets), samples=self.samples,
                                fill_value=fill_value)
        return self

    #------------------------------------------------------------------------#
    #                     Semblance calculation methods                      #
    #------------------------------------------------------------------------#

    @batch_method(target="threads", copy_src=False)
    def calculate_semblance(self, velocities, win_size=25):
        """Calculate vertical velocity semblance for the gather.

        Notes
        -----
        A detailed description of vertical velocity semblance and its computation algorithm can be found in
        :func:`~semblance.Semblance` docs.

        Examples
        --------
        Calculate semblance for 200 velocities from 2000 to 6000 m/s and a temporal window size of 8 samples:
        >>> semblance = gather.calculate_semblance(velocities=np.linspace(2000, 6000, 200), win_size=8)

        Parameters
        ----------
        velocities : 1d np.ndarray
            Range of velocity values for which semblance is calculated. Measured in meters/seconds.
        win_size : int, optional, defaults to 25
            Temporal window size used for semblance calculation. The higher the `win_size` is, the smoother the
            resulting semblance will be but to the detriment of small details. Measured in samples.

        Returns
        -------
        semblance : Semblance
            Calculated vertical velocity semblance.
        """
        gather = self.copy().sort(by="offset")
        return Semblance(gather=gather, velocities=velocities, win_size=win_size)

    @batch_method(target="threads", args_to_unpack="stacking_velocity", copy_src=False)
    def calculate_residual_semblance(self, stacking_velocity, n_velocities=140, win_size=25, relative_margin=0.2):
        """Calculate residual vertical velocity semblance for the gather and a chosen stacking velocity.

        Notes
        -----
        A detailed description of residual vertical velocity semblance and its computation algorithm can be found in
        :func:`~semblance.ResidualSemblance` docs.

        Examples
        --------
        Calculate residual semblance for a gather and a stacking velocity, loaded from a file:
        >>> velocity = StackingVelocity.from_file(velocity_path)
        >>> residual = gather.calculate_residual_semblance(velocity, n_velocities=100, win_size=8)

        Parameters
        ----------
        stacking_velocity : StackingVelocity or StackingVelocityField or str
            Stacking velocity around which residual semblance is calculated. `StackingVelocity` instance is used
            directly. If `StackingVelocityField` instance is passed, a `StackingVelocity` corresponding to gather
            coordinates is fetched from it.
            May be `str` if called in a pipeline: in this case it defines a component with stacking velocities to use.
        n_velocities : int, optional, defaults to 140
            The number of velocities to compute residual semblance for.
        win_size : int, optional, defaults to 25
            Temporal window size used for semblance calculation. The higher the `win_size` is, the smoother the
            resulting semblance will be but to the detriment of small details. Measured in samples.
        relative_margin : float, optional, defaults to 0.2
            Relative velocity margin, that determines the velocity range for semblance calculation for each time `t` as
            `stacking_velocity(t)` * (1 +- `relative_margin`).

        Returns
        -------
        semblance : ResidualSemblance
            Calculated residual vertical velocity semblance.
        """
        if isinstance(stacking_velocity, StackingVelocityField):
            stacking_velocity = stacking_velocity(self.coords)
        gather = self.copy().sort(by="offset")
        return ResidualSemblance(gather=gather, stacking_velocity=stacking_velocity, n_velocities=n_velocities,
                                 win_size=win_size, relative_margin=relative_margin)

    #------------------------------------------------------------------------#
    #                           Gather corrections                           #
    #------------------------------------------------------------------------#

    @batch_method(target="threads", args_to_unpack="refractor_velocity")
    def apply_lmo(self, refractor_velocity, delay=100, fill_value=np.nan, event_headers=None):
        """Perform gather linear moveout correction using the given near-surface velocity model.

        Parameters
        ----------
        refractor_velocity : int, float, RefractorVelocity, RefractorVelocityField or str
            Near-surface velocity model to perform LMO correction with. `RefractorVelocity` instance is used directly.
            If `RefractorVelocityField` instance is passed, a `RefractorVelocity` corresponding to gather coordinates
            is fetched from it. If `int` or `float` then constant-velocity correction is performed.
            May be `str` if called in a pipeline: in this case it defines a component with refractor velocities to use.
        delay : float, optional, defaults to 100
            An extra delay in milliseconds introduced in each trace, positive values result in shifting gather traces
            down. Used to center the first breaks hodograph around the delay value instead of 0.
        fill_value : float, optional, defaults to 0
            Value used to fill the amplitudes outside the gather bounds after moveout.
        event_headers : str, list, or None, optional, defaults to None
            Headers columns which will be LMO-corrected inplace.

        Returns
        -------
        self : Gather
            LMO-corrected gather.

        Raises
        ------
        ValueError
            If wrong type of `refractor_velocity` is passed.
        """
        if isinstance(refractor_velocity, (int, float)):
            refractor_velocity = RefractorVelocity.from_constant_velocity(refractor_velocity)
        if isinstance(refractor_velocity, RefractorVelocityField):
            refractor_velocity = refractor_velocity(self.coords)
        if not isinstance(refractor_velocity, RefractorVelocity):
            raise ValueError("refractor_velocity must be of int, float, RefractorVelocity or RefractorVelocityField "
                             "type")

        trace_delays = delay - refractor_velocity(self.offsets)
        trace_delays_samples = times_to_indices(trace_delays, self.samples, round=True).astype(int)
        self.data = correction.apply_lmo(self.data, trace_delays_samples, fill_value)
        event_headers = [] if event_headers is None else to_list(event_headers)
        for header in event_headers:
            self[header] += trace_delays.reshape(-1, 1)
        return self

    @batch_method(target="threads", args_to_unpack="stacking_velocity")
    def apply_nmo(self, stacking_velocity):
        """Perform gather normal moveout correction using the given stacking velocity.

        Notes
        -----
        A detailed description of NMO correction can be found in :func:`~utils.correction.apply_nmo` docs.

        Parameters
        ----------
        stacking_velocity : int, float, StackingVelocity, StackingVelocityField or str
            Stacking velocities to perform NMO correction with. `StackingVelocity` instance is used directly. If
            `StackingVelocityField` instance is passed, a `StackingVelocity` corresponding to gather coordinates is
            fetched from it. If `int` or `float` then constant-velocity correction is performed.
            May be `str` if called in a pipeline: in this case it defines a component with stacking velocities to use.

        Returns
        -------
        self : Gather
            NMO-corrected gather.

        Raises
        ------
        ValueError
            If wrong type of `stacking_velocity` is passed.
        """
        if isinstance(stacking_velocity, (int, float)):
            stacking_velocity = StackingVelocity.from_constant_velocity(stacking_velocity)
        if isinstance(stacking_velocity, StackingVelocityField):
            stacking_velocity = stacking_velocity(self.coords)
        if not isinstance(stacking_velocity, StackingVelocity):
            raise ValueError("stacking_velocity must be of int, float, StackingVelocity or StackingVelocityField type")

        velocities_ms = stacking_velocity(self.times) / 1000  # from m/s to m/ms
        self.data = correction.apply_nmo(self.data, self.times, self.offsets, velocities_ms, self.sample_rate)
        return self

    #------------------------------------------------------------------------#
    #                       General processing methods                       #
    #------------------------------------------------------------------------#

    @batch_method(target="for")
    def sort(self, by):
        """Sort gather `headers` and traces by specified header column.

        Parameters
        ----------
        by : str
            `headers` column name to sort the gather by.

        Returns
        -------
        self : Gather
            Gather sorted by `by` column. Sets `sort_by` attribute to `by`.

        Raises
        ------
        TypeError
            If `by` is not str.
        ValueError
            If `by` column was not loaded in `headers`.
        """
        if not isinstance(by, str):
            raise TypeError(f'`by` should be str, not {type(by)}')
        if self.sort_by == by:
            return self
        order = np.argsort(self[by].ravel(), kind='stable')
        self.sort_by = by
        self.data = self.data[order]
        self.headers = self.headers.iloc[order]
        return self

    @batch_method(target="for")
    def get_central_gather(self):
        """Get a central CDP gather from a supergather.

        A supergather has `SUPERGATHER_INLINE_3D` and `SUPERGATHER_CROSSLINE_3D` headers columns, whose values equal to
        values of `INLINE_3D` and `CROSSLINE_3D` only for traces from the central CDP gather. Read more about
        supergather generation in :func:`~Survey.generate_supergathers` docs.

        Returns
        -------
        self : Gather
            `self` with only traces from the central CDP gather kept. Updates `self.headers` and `self.data` inplace.
        """
        mask = np.all(self["INLINE_3D", "CROSSLINE_3D"] == self["SUPERGATHER_INLINE_3D", "SUPERGATHER_CROSSLINE_3D"],
                      axis=1)
        self.headers = self.headers.loc[mask]
        self.data = self.data[mask]
        return self

    @batch_method(target="threads")
    def stack(self):
        """Stack a gather by calculating mean value of all non-nan amplitudes for each time over the offset axis.

        The gather being stacked must contain traces from a single bin. The resulting gather will contain a single
        trace with `headers` matching those of the first input trace.

        Returns
        -------
        gather : Gather
            Stacked gather.
        """
        lines = self[["INLINE_3D", "CROSSLINE_3D"]]
        if (lines != lines[0]).any():
            raise ValueError("Only a single CDP gather can be stacked")

        # Preserve headers of the first trace of the gather being stacked
        self.headers = self.headers.iloc[[0]]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            self.data = np.nanmean(self.data, axis=0, keepdims=True)
        self.data = np.nan_to_num(self.data)
        return self

    def crop(self, origins, crop_shape, n_crops=1, stride=None, pad_mode='constant', **kwargs):
        """Crop gather data.

        Parameters
        ----------
        origins : list, tuple, np.ndarray or str
            Origins define top-left corners for each crop (the first trace and the first time sample respectively)
            or a rule used to calculate them. All array-like values are cast to an `np.ndarray` and treated as origins
            directly, except for a 2-element tuple of `int`, which will be treated as a single individual origin.
            If `str`, represents a mode to calculate origins. Two options are supported:
            - "random": calculate `n_crops` crops selected randomly using a uniform distribution over the gather data,
              so that no crop crosses gather boundaries,
            - "grid": calculate a deterministic uniform grid of origins, whose density is determined by `stride`.
        crop_shape : tuple with 2 elements
            Shape of the resulting crops.
        n_crops : int, optional, defaults to 1
            The number of generated crops if `origins` is "random".
        stride : tuple with 2 elements, optional, defaults to crop_shape
            Steps between two adjacent crops along both axes if `origins` is "grid". The lower the value is, the more
            dense the grid of crops will be. An extra origin will always be placed so that the corresponding crop will
            fit in the very end of an axis to guarantee complete data coverage with crops regardless of passed
            `crop_shape` and `stride`.
        pad_mode : str or callable, optional, defaults to 'constant'
            Padding mode used when a crop with given origin and shape crossed boundaries of gather data. Passed
            directly to `np.pad`, see https://numpy.org/doc/stable/reference/generated/numpy.pad.html for more
            details.
        kwargs : dict, optional
            Additional keyword arguments to `np.pad`.

        Returns
        -------
        crops : CroppedGather
            Calculated gather crops.
        """
        origins = make_origins(origins, self.shape, crop_shape, n_crops, stride)
        return CroppedGather(self, origins, crop_shape, pad_mode, **kwargs)

    @batch_method(target="threads")
    def bandpass_filter(self, low=None, high=None, filter_size=81, **kwargs):
        """Filter frequency spectrum of the gather.

        Can act as a lowpass, bandpass or highpass filter. `low` and `high` serve as the range for the remaining
        frequencies and can be passed either solely or together.

        Examples
        --------
        Apply highpass filter: remove all the frequencies bellow 30 Hz.
        >>> gather.bandpass_filter(low=30)

        Apply bandpass filter: keep frequencies within [30, 100] Hz range.
        >>> gather.bandpass_filter(low=30, high=100)

        Apply lowpass filter, remove all the frequencies above 100 Hz.
        >>> gather.bandpass_filter(high=100)

        Notes
        -----
        Default `filter_size` is set to 81 to guarantee that transition bandwidth of the filter does not exceed 10% of
        the Nyquist frequency for the default Hamming window.

        Parameters
        ----------
        low : int, optional
            Lower bound for the remaining frequencies
        high : int, optional
            Upper bound for the remaining frequencies
        filter_size : int, defaults to 81
            The length of the filter
        kwargs : misc, optional
            Additional keyword arguments to the `scipy.firwin`

        Returns
        -------
        self : Gather
            `self` with filtered frequency spectrum.
        """
        filter_size |= 1  # Guarantee that filter size is odd
        pass_zero = low is None
        cutoffs = [cutoff for cutoff in [low, high] if cutoff is not None]

        # Construct the filter and flip it since opencv computes crosscorrelation instead of convolution
        kernel = firwin(filter_size, cutoffs, pass_zero=pass_zero, fs=1000 / self.sample_rate, **kwargs)[::-1]
        cv2.filter2D(self.data, dst=self.data, ddepth=-1, kernel=kernel.reshape(1, -1))
        return self

    @batch_method(target="threads")
    def resample(self, new_sample_rate, kind=3, anti_aliasing=True):
        """Change sample rate of traces in the gather.

        This implies increasing or decreasing the number of samples in each trace. In case new sample rate is greater
        than the current one, anti-aliasing filter is optionally applied to avoid frequency aliasing.

        Parameters
        ----------
        new_sample_rate : float
            New sample rate
        kind : int or str, defaults to 3
            The interpolation method to use.
            If `int`, use piecewise polynomial interpolation with degree `kind`;
            if `str`, delegate interpolation to scipy.interp1d with mode `kind`.
        anti_aliasing : bool, defaults to True
            Whether to apply anti-aliasing filter or not. Ignored in case of upsampling.

        Returns
        -------
        self : Gather
            `self` with new sample rate
        """
        current_sample_rate = self.sample_rate

        # Anti-aliasing filter is optionally applied during downsampling to avoid frequency aliasing
        if new_sample_rate > current_sample_rate and anti_aliasing:
            # Smoothly attenuate frequencies starting from 0.8 of the new Nyquist frequency so that all frequencies
            # above are zeroed out
            nyquist_frequency = 1000 / (2 * new_sample_rate)
            filter_size = int(40 * new_sample_rate / current_sample_rate)
            self.bandpass_filter(high=0.9 * nyquist_frequency, filter_size=filter_size, window="hann")

        new_samples = np.arange(self.samples[0], self.samples[-1] + 1e-6, new_sample_rate, self.samples.dtype)

        if isinstance(kind, int):
            data_resampled = piecewise_polynomial(new_samples, self.samples, self.data, kind)
        elif isinstance(kind, str):
            data_resampled = scipy.interpolate.interp1d(self.samples, self.data, kind=kind)(new_samples)

        self.data = data_resampled
        self.samples = new_samples
        return self

    @batch_method(target="for")
    def apply_agc(self, window_size=250, mode='rms'):
        """Calculate instantaneous or RMS amplitude AGC coefficients and apply them to gather data.

        Parameters
        ----------
        window_size : int, optional, defaults to 250
            Window size to calculate AGC scaling coefficient in, measured in milliseconds.
        mode : str, optional, defaults to 'rms'
            Mode for AGC: if 'rms', root mean squared value of non-zero amplitudes in the given window
            is used as scaling coefficient (RMS amplitude AGC), if 'abs' - mean of absolute non-zero
            amplitudes (instantaneous AGC).

        Raises
        ------
        ValueError
            If window_size is less than (3 * sample_rate) milliseconds or larger than trace length.
            If mode is neither 'rms' nor 'abs'.

        Returns
        -------
        self : Gather
            Gather with AGC applied to its data.
        """
        # Cast window from ms to samples
        window_size_samples = int(window_size // self.sample_rate) + 1

        if mode not in ['abs', 'rms']:
            raise ValueError(f"mode should be either 'abs' or 'rms', but {mode} was given")
        if (window_size_samples < 3) or (window_size_samples > self.n_samples):
            raise ValueError(f'window should be at least {3*self.sample_rate} milliseconds and'
                             f' {(self.n_samples-1)*self.sample_rate} at most, but {window_size} was given')
        self.data = gain.apply_agc(data=self.data, window_size=window_size_samples, mode=mode)
        return self

    @batch_method(target="for")
    def apply_sdc(self, velocity=None, v_pow=2, t_pow=1):
        """Calculate spherical divergence correction coefficients and apply them to gather data.

        Parameters
        ----------
        velocities: StackingVelocity or None, optional, defaults to None.
            StackingVelocity that is used to obtain velocities at self.times, measured in meters / second.
            If None, default StackingVelocity object is used.
        v_pow : float, optional, defaults to 2
            Velocity power value.
        t_pow: float, optional, defaults to 1
            Time power value.

        Returns
        -------
        self : Gather
            Gather with applied SDC.
        """
        if velocity is None:
            velocity = DEFAULT_SDC_VELOCITY
        if not isinstance(velocity, StackingVelocity):
            raise ValueError("Only StackingVelocity instance or None can be passed as velocity")
        self.data = gain.apply_sdc(self.data, v_pow, velocity(self.times), t_pow, self.times)
        return self

    @batch_method(target="for")
    def undo_sdc(self, velocity=None, v_pow=2, t_pow=1):
        """Calculate spherical divergence correction coefficients and use them to undo previously applied SDC.

        Parameters
        ----------
        velocities: StackingVelocity or None, optional, defaults to None.
            StackingVelocity that is used to obtain velocities at self.times, measured in meters / second.
            If None, default StackingVelocity object is used.
        v_pow : float, optional, defaults to 2
            Velocity power value.
        t_pow: float, optional, defaults to 1
            Time power value.

        Returns
        -------
        self : Gather
            Gather without SDC.
        """
        if velocity is None:
            velocity = DEFAULT_SDC_VELOCITY
        if not isinstance(velocity, StackingVelocity):
            raise ValueError("Only StackingVelocity instance or None can be passed as velocity")
        self.data = gain.undo_sdc(self.data, v_pow, velocity(self.times), t_pow, self.times)
        return self

    #------------------------------------------------------------------------#
    #                         Visualization methods                          #
    #------------------------------------------------------------------------#

    @plotter(figsize=(10, 7))
    def plot(self, mode="seismogram", *, title=None, x_ticker=None, y_ticker=None, ax=None, **kwargs):
        """Plot gather traces.

        The traces can be displayed in a number of representations, depending on the `mode` provided. Currently, the
        following options are supported:
        - `seismogram`: a 2d grayscale image of seismic traces. This mode supports the following `kwargs`:
            * `colorbar`: whether to add a colorbar to the right of the gather plot (defaults to `False`). If `dict`,
              defines extra keyword arguments for `matplotlib.figure.Figure.colorbar`,
            * `q_vmin`, `q_vmax`: quantile range of amplitude values covered by the colormap (defaults to 0.1 and 0.9),
            * Any additional arguments for `matplotlib.pyplot.imshow`. Note, that `vmin` and `vmax` arguments take
              priority over `q_vmin` and `q_vmax` respectively.
        - `wiggle`: an amplitude vs time plot for each trace of the gather as an oscillating line around its mean
          amplitude. This mode supports the following `kwargs`:
            * `norm_tracewise`: specifies whether to standardize each trace independently or use gather mean amplitude
              and standard deviation (defaults to `True`),
            * `std`: amplitude scaling factor. Higher values result in higher plot oscillations (defaults to 0.5),
            * `lw` and `alpha`: width of the lines and transparency of polygons, by default estimated
              based on the number of traces in the gather and figure size.
            * `color`: defines a color for traces,
            * Any additional arguments for `matplotlib.pyplot.plot`.
        - `hist`: a histogram of the trace data amplitudes or header values. This mode supports the following `kwargs`:
            * `bins`: if `int`, the number of equal-width bins; if sequence, bin edges that include the left edge of
              the first bin and the right edge of the last bin,
            * `grid`: whether to show the grid lines,
            * `log`: set y-axis to log scale. If `True`, formatting defined in `y_ticker` is discarded,
            * Any additional arguments for `matplotlib.pyplot.hist`.

        Trace headers, whose values are measured in milliseconds (e.g. first break times) may be displayed over a
        seismogram or wiggle plot if passed as `event_headers`. If `top_header` is passed, an auxiliary scatter plot of
        values of this header will be shown on top of the gather plot.

        While the source of label ticks for both `x` and `y` is defined by `x_tick_src` and `y_tick_src`, ticker
        appearance can be controlled via `x_ticker` and `y_ticker` parameters respectively. In the most general form,
        each of them is a `dict` with the following most commonly used keys:
        - `label`: axis label. Can be any string.
        - `round_to`: the number of decimal places to round tick labels to (defaults to 0).
        - `rotation`: the rotation angle of tick labels in degrees (defaults to 0).
        - One of the following keys, defining the way to place ticks:
            * `num`: place a given number of evenly-spaced ticks,
            * `step_ticks`: place ticks with a given step between two adjacent ones,
            * `step_labels`: place ticks with a given step between two adjacent ones in the units of the corresponding
              labels (e.g. place a tick every 200ms for `y` axis or every 300m offset for `x` axis). This option is
              valid only for "seismogram" and "wiggle" modes.
        A short argument form allows defining both tickers labels as a single `str`, which will be treated as the value
        for the `label` key. See :func:`~plot_utils.set_ticks` for more details on the ticker parameters.

        Parameters
        ----------
        mode : "seismogram", "wiggle" or "hist", optional, defaults to "seismogram"
            A type of the gather representation to display:
            - "seismogram": a 2d grayscale image of seismic traces;
            - "wiggle": an amplitude vs time plot for each trace of the gather;
            - "hist": histogram of the data amplitudes or some header values.
        title : str or dict, optional, defaults to None
            If `str`, a title of the plot.
            If `dict`, should contain keyword arguments to pass to `matplotlib.axes.Axes.set_title`. In this case, the
            title string is stored under the `label` key.
        x_ticker : str or dict, optional, defaults to None
            Parameters to control `x` axis label and ticker formatting and layout.
            If `str`, it will be displayed as axis label.
            If `dict`, the axis label is specified under the "label" key and the rest of keys define labels formatting
            and layout, see :func:`~plot_utils.set_ticks` for more details.
            If not given, axis label is defined by `x_tick_src`.
        y_ticker : str or dict, optional, defaults to None
            Parameters to control `y` axis label and ticker formatting and layout.
            If `str`, it will be displayed as axis label.
            If `dict`, the axis label is specified under the "label" key and the rest of keys define labels formatting
            and layout, see :func:`~plot_utils.set_ticks` for more details.
            If not given, axis label is defined by `y_tick_src`.
        ax : matplotlib.axes.Axes, optional, defaults to None
            An axis of the figure to plot on. If not given, it will be created automatically.
        x_tick_src : str, optional
            Source of the tick labels to be plotted on x axis. For "seismogram" and "wiggle" can be either "index"
            (default if gather is not sorted) or any header; for "hist" it also defines the data source and can be
            either "amplitude" (default) or any header.
            Also serves as a default for axis label.
        y_tick_src : str, optional
            Source of the tick labels to be plotted on y axis. For "seismogram" and "wiggle" can be either "time"
            (default) or "samples"; has no effect in "hist" mode. Also serves as a default for axis label.
        event_headers : str, array-like or dict, optional, defaults to None
            Valid only for "seismogram" and "wiggle" modes.
            Headers, whose values will be displayed over the gather plot. Must be measured in milliseconds.
            If `dict`, allows controlling scatter plot options and handling outliers (header values falling out the `y`
            axis range). The following keys are supported:
            - `headers`: header names, can be either `str` or an array-like.
            - `process_outliers`: an approach for outliers processing. Available options are:
                * `clip`: clip outliers to fit the range of `y` axis,
                * `discard`: do not display outliers,
                * `none`: plot all the header values (default behavior).
            - Any additional arguments for `matplotlib.axes.Axes.scatter`.
            If some dictionary value is array-like, each its element will be associated with the corresponding header.
            Otherwise, the single value will be used for all the scatter plots.
        top_header : str, optional, defaults to None
            Valid only for "seismogram" and "wiggle" modes.
            The name of a header whose values will be plotted on top of the gather plot.
        figsize : tuple, optional, defaults to (10, 7)
            Size of the figure to create if `ax` is not given. Measured in inches.
        save_to : str or dict, optional, defaults to None
            If `str`, a path to save the figure to.
            If `dict`, should contain keyword arguments to pass to `matplotlib.pyplot.savefig`. In this case, the path
            is stored under the `fname` key.
            If `None`, the figure is not saved.
        kwargs : misc, optional
            Additional keyword arguments to the plotter depending on the `mode`.

        Returns
        -------
        self : Gather
            Gather unchanged.

        Raises
        ------
        ValueError
            If given `mode` is unknown.
            If `colorbar` is not `bool` or `dict`.
            If length of `color` doesn't match the number of traces in gather.
            If `event_headers` argument has the wrong format or given outlier processing mode is unknown.
            If `x_ticker` or `y_ticker` has the wrong format.
        """
        # Cast text-related parameters to dicts and add text formatting parameters from kwargs to each of them
        (title, x_ticker, y_ticker), kwargs = set_text_formatting(title, x_ticker, y_ticker, **kwargs)

        # Plot the gather depending on the mode passed
        plotters_dict = {
            "seismogram": self._plot_seismogram,
            "wiggle": self._plot_wiggle,
            "hist": self._plot_histogram,
        }
        if mode not in plotters_dict:
            raise ValueError(f"Unknown mode {mode}")
        plotters_dict[mode](ax, title=title, x_ticker=x_ticker, y_ticker=y_ticker, **kwargs)
        return self

    def _plot_histogram(self, ax, title, x_ticker, y_ticker, x_tick_src="amplitude", bins=None,
                        log=False, grid=True, **kwargs):
        """Plot histogram of the data specified by x_tick_src."""
        data = self.data if x_tick_src == "amplitude" else self[x_tick_src]
        _ = ax.hist(data.ravel(), bins=bins, **kwargs)
        set_ticks(ax, "x", tick_labels=None, **{"label": x_tick_src, 'round_to': None, **x_ticker})
        set_ticks(ax, "y", tick_labels=None, **{"label": "counts", **y_ticker})

        ax.grid(grid)
        if log:
            ax.set_yscale("log")
        ax.set_title(**{'label': None, **title})

    # pylint: disable=too-many-arguments
    def _plot_seismogram(self, ax, title, x_ticker, y_ticker, x_tick_src=None, y_tick_src='time', colorbar=False,
                         q_vmin=0.1, q_vmax=0.9, event_headers=None, top_header=None, **kwargs):
        """Plot the gather as a 2d grayscale image of seismic traces."""
        # Make the axis divisible to further plot colorbar and header subplot
        divider = make_axes_locatable(ax)
        vmin, vmax = self.get_quantile([q_vmin, q_vmax])
        kwargs = {"cmap": "gray", "aspect": "auto", "vmin": vmin, "vmax": vmax, **kwargs}
        img = ax.imshow(self.data.T, **kwargs)
        add_colorbar(ax, img, colorbar, divider, y_ticker)
        self._finalize_plot(ax, title, divider, event_headers, top_header, x_ticker, y_ticker, x_tick_src, y_tick_src)

    #pylint: disable=invalid-name
    def _plot_wiggle(self, ax, title, x_ticker, y_ticker, x_tick_src=None, y_tick_src="time", norm_tracewise=True,
                     std=0.5, event_headers=None, top_header=None, lw=None, alpha=None, color="black", **kwargs):
        """Plot the gather as an amplitude vs time plot for each trace."""
        # Make the axis divisible to further plot colorbar and header subplot
        divider = make_axes_locatable(ax)

        # The default parameters lw = 1 and alpha = 1 are fine for 150 traces gather being plotted on 7.75 inches width
        # axes(by default created by gather.plot()). Scale this parameters linearly for bigger gathers or smaller axes.
        axes_width = ax.get_window_extent().transformed(ax.figure.dpi_scale_trans.inverted()).width

        MAX_TRACE_DENSITY = 150 / 7.75
        BOUNDS = [[0.25, 1], [0, 1.5]] # The clip limits for parameters after linear scale.

        alpha, lw = [np.clip(MAX_TRACE_DENSITY * (axes_width / self.n_traces), *val_bounds) if val is None else val
                     for val, val_bounds in zip([alpha, lw], BOUNDS)]

        std_axis = 1 if norm_tracewise else None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            traces = std * ((self.data - np.nanmean(self.data, axis=1, keepdims=True)) /
                            (np.nanstd(self.data, axis=std_axis, keepdims=True) + 1e-10))

        # Shift trace amplitudes according to the trace index in the gather
        amps = traces + np.arange(traces.shape[0]).reshape(-1, 1)
        # Plot all the traces as one Line, then hide transitions between adjacent traces
        amps = np.concatenate([amps, np.full((len(amps), 1), np.nan)], axis=1)
        ax.plot(amps.ravel(), np.broadcast_to(np.arange(amps.shape[1]), amps.shape).ravel(),
                color=color, lw=lw, **kwargs)

        # Find polygons bodies: indices of target amplitudes, start and end
        poly_amp_ix = np.argwhere(traces > 0)
        start_ix = np.argwhere((np.diff(poly_amp_ix[:, 0], prepend=poly_amp_ix[0, 0]) != 0) |
                               (np.diff(poly_amp_ix[:, 1], prepend=poly_amp_ix[0, 1]) != 1)).ravel()
        end_ix = start_ix + np.diff(start_ix, append=len(poly_amp_ix)) - 1

        shift = np.arange(len(start_ix)) * 3
        # For each polygon we need to:
        # 1. insert 0 amplitude at the start.
        # 2. append 0 amplitude to the end.
        # 3. append the start point to the end to close polygon.
        # Fill the array storing resulted polygons
        verts = np.empty((len(poly_amp_ix) + 3 * len(start_ix), 2))
        verts[start_ix + shift] = poly_amp_ix[start_ix]
        verts[end_ix + shift + 2] = poly_amp_ix[end_ix]
        verts[end_ix + shift + 3] = poly_amp_ix[start_ix]

        body_ix = np.setdiff1d(np.arange(len(verts)),
                               np.concatenate([start_ix + shift, end_ix + shift + 2, end_ix + shift + 3]),
                               assume_unique=True)
        verts[body_ix] = np.column_stack([amps[tuple(poly_amp_ix.T)], poly_amp_ix[:, 1]])

        # Fill the array representing the nodes codes: either start, intermediate or end code.
        codes = np.full(len(verts), Path.LINETO)
        codes[start_ix + shift] = Path.MOVETO
        codes[end_ix + shift + 3] = Path.CLOSEPOLY

        patch = PathPatch(Path(verts, codes), color=color, alpha=alpha)
        ax.add_patch(patch)
        ax.update_datalim([(0, 0), traces.shape])
        if not ax.yaxis_inverted():
            ax.invert_yaxis()
        self._finalize_plot(ax, title, divider, event_headers, top_header, x_ticker, y_ticker, x_tick_src, y_tick_src)

    def _finalize_plot(self, ax, title, divider, event_headers, top_header,
                       x_ticker, y_ticker, x_tick_src, y_tick_src):
        """Plot optional artists and set ticks on the `ax`. Utility method for 'seismogram' and 'wiggle' modes."""
        # Add headers scatter plot if needed
        if event_headers is not None:
            self._plot_headers(ax, event_headers)

        # Add a top subplot for given header if needed and set plot title
        top_ax = ax
        if top_header is not None:
            top_ax = self._plot_top_subplot(ax=ax, divider=divider, header_values=self[top_header].ravel(),
                                            y_ticker=y_ticker)

        # Set axis ticks
        x_tick_src = x_tick_src or self.sort_by or "index"
        self._set_ticks(ax, axis="x", tick_src=x_tick_src, ticker=x_ticker)
        self._set_ticks(ax, axis="y", tick_src=y_tick_src, ticker=y_ticker)

        top_ax.set_title(**{'label': None, **title})

    @staticmethod
    def _parse_headers_kwargs(headers_kwargs, headers_key):
        """Construct a `dict` of kwargs for each header defined in `headers_kwargs` under `headers_key` key so that it
        contains all other keys from `headers_kwargs` with the values defined as follows:
        1. If the value in `headers_kwargs` is an array-like, it is indexed with the index of the currently processed
           header,
        2. Otherwise, it is kept unchanged.

        Examples
        --------
        >>> headers_kwargs = {
        ...     "headers": ["FirstBreakTrue", "FirstBreakPred"],
        ...     "s": 5,
        ...     "c": ["blue", "red"]
        ... }
        >>> Gather._parse_headers_kwargs(headers_kwargs, headers_key="headers")
        [{'headers': 'FirstBreakTrue', 's': 5, 'c': 'blue'},
         {'headers': 'FirstBreakPred', 's': 5, 'c': 'red'}]
        """
        if not isinstance(headers_kwargs, dict):
            return [{headers_key: header} for header in to_list(headers_kwargs)]

        if headers_key not in headers_kwargs:
            raise KeyError(f'{headers_key} key is not defined in event_headers')

        n_headers = len(to_list(headers_kwargs[headers_key]))
        kwargs_list = [{} for _ in range(n_headers)]
        for key, values in headers_kwargs.items():
            values = to_list(values)
            if len(values) == 1:
                values = values * n_headers
            elif len(values) != n_headers:
                raise ValueError(f"Incompatible length of {key} array: {n_headers} expected but {len(values)} given.")
            for ix, value in enumerate(values):
                kwargs_list[ix][key] = value
        return kwargs_list

    def _plot_headers(self, ax, headers_kwargs):
        """Add scatter plots of values of one or more headers over the main gather plot."""
        x_coords = np.arange(self.n_traces)
        kwargs_list = self._parse_headers_kwargs(headers_kwargs, "headers")
        for kwargs in kwargs_list:
            kwargs = {"zorder": 10, **kwargs}  # Increase zorder to plot headers on top of gather
            header = kwargs.pop("headers")
            label = kwargs.pop("label", header)
            process_outliers = kwargs.pop("process_outliers", "none")
            y_coords = times_to_indices(self[header].ravel(), self.samples, round=False)
            if process_outliers == "clip":
                y_coords = np.clip(y_coords, 0, self.n_samples - 1)
            elif process_outliers == "discard":
                y_coords = np.where((y_coords >= 0) & (y_coords <= self.n_samples - 1), y_coords, np.nan)
            elif process_outliers != "none":
                raise ValueError(f"Unknown outlier processing mode {process_outliers}")
            ax.scatter(x_coords, y_coords, label=label, **kwargs)

        if headers_kwargs:
            ax.legend()

    def _plot_top_subplot(self, ax, divider, header_values, y_ticker, **kwargs):
        """Add a scatter plot of given header values on top of the main gather plot."""
        top_ax = divider.append_axes("top", sharex=ax, size="12%", pad=0.05)
        top_ax.scatter(np.arange(self.n_traces), header_values, **{"s": 5, "color": "black", **kwargs})
        top_ax.xaxis.set_visible(False)
        top_ax.yaxis.tick_right()
        top_ax.invert_yaxis()
        format_subplot_yticklabels(top_ax, **y_ticker)
        return top_ax

    def _get_x_ticks(self, axis_label):
        """Get tick labels for x-axis: either any gather header or ordinal numbers of traces in the gather."""
        if axis_label in self.headers.columns:
            return self[axis_label].reshape(-1)
        if axis_label == "index":
            return np.arange(self.n_traces)
        raise ValueError(f"Unknown label for x axis {axis_label}")

    def _get_y_ticks(self, axis_label):
        """Get tick labels for y-axis: either time samples or ordinal numbers of samples in the gather."""
        if axis_label == "time":
            return self.samples
        if axis_label == "samples":
            return np.arange(self.n_samples)
        raise ValueError(f"y axis label must be either `time` or `samples`, not {axis_label}")

    def _set_ticks(self, ax, axis, tick_src, ticker):
        """Set ticks, their labels and an axis label for a given axis."""
        # Get tick_labels depending on axis and its label
        if axis == "x":
            tick_labels = self._get_x_ticks(tick_src)
        elif axis == "y":
            tick_labels = self._get_y_ticks(tick_src)
        else:
            raise ValueError(f"Unknown axis {axis}")
        set_ticks(ax, axis, tick_labels=tick_labels, **{"label": tick_src, **ticker})

    def plot_nmo_correction(self, min_vel=1500, max_vel=6000, figsize=(6, 4.5), show_grid=True, **kwargs):
        """Perform interactive NMO correction of the gather with selected constant velocity.

        The plot provides 2 views:
        * Corrected gather (default). NMO correction is performed on the fly with the velocity controlled by a slider
          on top of the plot.
        * Source gather. This view disables the velocity slider.

        Plotting must be performed in a JupyterLab environment with the the `%matplotlib widget` magic executed and
        `ipympl` and `ipywidgets` libraries installed.

        Parameters
        ----------
        min_vel : float, optional, defaults to 1500
            Minimum seismic velocity value for NMO correction. Measured in meters/seconds.
        max_vel : float, optional, defaults to 6000
            Maximum seismic velocity value for NMO correction. Measured in meters/seconds.
        figsize : tuple with 2 elements, optional, defaults to (6, 4.5)
            Size of the created figure. Measured in inches.
        show_grid : bool, defaults to True
            If `True` shows the horizontal grid with a step based on `y_ticker`.
        kwargs : misc, optional
            Additional keyword arguments to `Gather.plot`.
        """
        NMOCorrectionPlot(self, min_vel=min_vel, max_vel=max_vel, figsize=figsize, show_grid=show_grid,
                          **kwargs).plot()

    def plot_lmo_correction(self, min_vel=500, max_vel=3000, figsize=(6, 4.5), show_grid=True, **kwargs):
        """Perform interactive LMO correction of the gather with the selected velocity.

        The plot provides 2 views:
        * Corrected gather (default). LMO correction is performed on the fly with the velocity controlled by a slider
        on top of the plot.
        * Source gather. This view disables the velocity slider.

        Plotting must be performed in a JupyterLab environment with the the `%matplotlib widget` magic executed and
        `ipympl` and `ipywidgets` libraries installed.

        Parameters
        ----------
        min_vel : float, optional, defaults to 500
            Minimum velocity value for LMO correction. Measured in meters/seconds.
        max_vel : float, optional, defaults to 3000
            Maximum velocity value for LMO correction. Measured in meters/seconds.
        figsize : tuple with 2 elements, optional, defaults to (6, 4.5)
            Size of the created figure. Measured in inches.
        show_grid : bool, defaults to True
            If `True` shows the horizontal grid with a step based on `y_ticker`.
        kwargs : misc, optional
            Additional keyword arguments to `Gather.plot`.
        """
        LMOCorrectionPlot(self, min_vel=min_vel, max_vel=max_vel, figsize=figsize, show_grid=show_grid,
                          **kwargs).plot()