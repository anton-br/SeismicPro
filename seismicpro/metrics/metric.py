"""Implements base class for metric calculation"""

from copy import deepcopy
from textwrap import dedent

from .metric_map import MetricMap
from ..utils import to_list, get_first_defined


class Metric:
    """Define a base metric class.

    Each concrete metric subclass describes a metric as a function and must implement an obligatory `__call__` method
    and optionally redefine various class attributes that store metric metadata (e.g metric name and its minimum and
    maximum values). `__init__` method may also be overridden to accept any additional parameters and use them for
    further calculations. By default it simply sets a new name for a metric instance.

    If metric values are estimated for items with known spatial coordinates (e.g. a metric evaluates the quality of
    noise attenuation for shot gathers whose coordinates are defined by `SourceX` and `SourceY` trace headers), an
    instance of `MetricMap` may be constructed which allows for metric visualization over a field map. Such plot may
    optionally be interactive: clicking on it may display some data representation for items at click locations (e.g.
    the gather before and after noise attenuation). This behavior is defined by views: metric methods that accept click
    coordinates, item index and axes to plot on. These methods should be listed in the `views` attribute to be
    automatically detected by a metric map.

    Each view should somehow be able to transform click information into data for visualization, which usually requires
    knowledge of the metric calculation context (in the described case one needs a `Survey` instance to obtain a gather
    by its coordinates). This context should be stored in the corresponding `MetricMap` instance and is provided to the
    metric by calling its `bind_context` upon the first interactive plot of the metric map.

    The simplest way to calculate a metric and construct its map is to manually iterate over a dataset and calculate
    the metric for each item. However, convenient interfaces for the most common use cases exist e.g. `PipelineMetric`
    may be computed using `SeismicBatch.calculate_metric` method which simplifies metric accumulation over batches of
    data in a global storage.

    Thus, in order to define a new metric, one needs to:
    * Create a class inherited from `Metric`,
    * Define metric calculation logic in its `__call__` method,
    * Optionally redefine `__init__` to store some additional metric parameters,
    * Optionally implement one or more views and list them in the `views` attribute,
    * Optionally set other metric attributes for future convenience.

    Parameters
    ----------
    name : str, optional
        Metric name, overrides default name if given.

    Attributes
    ----------
    name : str
        Metric name. Used in internal data structures (e.g. `MetricMap`) to identify the metric and is displayed in a
        metric map title.
    is_lower_better : bool or None
        Specifies whether lower metric value is better. Affects the default aggregation of the metric and the map
        colormap.
    min_value : float or None
        Minimum feasible metric value. If defined, limits the colorbar of the metric map.
    max_value : float or None
        Maximum feasible metric value. If defined, limits the colorbar of the metric map.
    map_class : type
        A type of the metric map generated by `Metric.construct_map`. Defaults to `MetricMap`.
    views : str or iterable of str
        Views of the metric to display on click on a metric map in interactive mode. No default views are defined.
    vmin : float or None
        Minimum colorbar value. Unlike `min_value` which describes minimum feasible value of the metric in mathematical
        sense, `vmin` defines colorbar limit to compare several maps or highlight outliers. Takes precedence over
        `min_value`.
    vmax : float or None
        Maximum colorbar value. Unlike `max_value` which describes maximum feasible value of the metric in mathematical
        sense, `vmax` defines colorbar limit to compare several maps or highlight outliers. Takes precedence over
        `max_value`.
    has_bound_context : bool
        Whether the metric has bound execution context and can be used for interactive metric map plotting.
    """
    name = "metric"
    is_lower_better = None
    min_value = None
    max_value = None

    map_class = MetricMap
    views = tuple()
    vmin = None
    vmax = None

    def __init__(self, name=None):
        if name is not None:
            if not isinstance(name, str):
                raise TypeError("Metric name name must be a string")
            self.name = name
        self.has_bound_context = False

    def __call__(self, *args, **kwargs):
        """Calculate the metric. Must be overridden in child classes."""
        _ = self, args, kwargs
        raise NotImplementedError

    def __repr__(self):
        """String representation of the metric."""
        return f"{type(self).__name__}(name='{self.name}')"

    def _get_general_info(self):
        msg = f"""
        Metric type:               {type(self).__name__}
        Metric name:               {self.name}
        Is lower value better:     {get_first_defined(self.is_lower_better, "Undefined")}
        Minimum feasible value:    {get_first_defined(self.min_value, "Undefined")}
        Maximum feasible value:    {get_first_defined(self.max_value, "Undefined")}
        """
        return dedent(msg).strip()

    def _get_plot_info(self):
        msg = f"""
        Metric map visualization parameters:
        Has bound context:         {self.has_bound_context}
        Metric map type:           {self.map_class.__name__}
        Number of metric views:    {len(self.views)}
        Minimum colorbar value:    {get_first_defined(self.vmin, "Undefined")}
        Maximum colorbar value:    {get_first_defined(self.vmax, "Undefined")}
        """
        return dedent(msg).strip()

    def __str__(self):
        """Print information about metric attributes."""
        return self._get_general_info() + "\n\n" + self._get_plot_info()

    def info(self):
        """Print information about metric attributes."""
        print(self)

    def copy(self):
        """Copy the metric."""
        return deepcopy(self)

    def bind_context(self, metric_map):
        """Process metric evaluation context."""
        _ = metric_map

    def bind_metric_map(self, metric_map):
        """Return a copy of the metric with bound `metric_map` context and `has_bound_context` flag set to `True`."""
        # Copy the metric to handle the case when it is simultaneously used in multiple maps
        self_bound = self.copy()
        self_bound.bind_context(metric_map=metric_map, **metric_map.context)
        self_bound.has_bound_context = True
        return self_bound

    def set_name(self, name=None):
        """Return a copy of the metric with updated `name`."""
        self_renamed = self.copy()
        if name is not None:
            self_renamed.name = name
        return self_renamed

    def get_views(self, **kwargs):
        """Return plotters of the metric views and those `kwargs` that should be passed further to an interactive map
        plotter."""
        return [getattr(self, view) for view in to_list(self.views)], kwargs

    def construct_map(self, coords, values, *, coords_cols=None, index=None, index_cols=None, agg=None, bin_size=None,
                      calculate_immediately=True, **context):
        """Construct a metric map.

        Parameters
        ----------
        coords : 2d array-like with 2 columns
            Metric coordinates for X and Y axes.
        values : 1d array-like or array of 1d arrays
            One or more metric values for each pair of coordinates in `coords`. Must match `coords` in length.
        coords_cols : array-like of str with 2 elements, optional
            Names of X and Y coordinates. Usually names of survey headers used to extract coordinates from. Defaults to
            ("X", "Y") if not given and cannot be inferred from `coords`.
        index : array-like, optional
            Unique identifiers of items in the map. Equals to `coords` if not given. Must match `coords` in length.
        index_cols : str or array-like of str, optional
            Names of `index` columns, usually names of survey headers used to extract `index` from. Equals to
            `coords_cols` if `index` is not given.
        agg : str or callable, optional
            A function used for aggregating the map. If not given, will be determined by the value of `is_lower_better`
            attribute of the metric class in order to highlight outliers. Passed directly to
            `pandas.core.groupby.DataFrameGroupBy.agg`.
        bin_size : int, float or array-like with length 2, optional
            Bin size for X and Y axes. If single `int` or `float`, the same bin size will be used for both axes.
        calculate_immediately : bool, optional, defaults to True
            Whether to calculate map data immediately or postpone it until the first access.
        context : misc, optional
            Any additional keyword arguments defining metric calculation context. Will be later passed to
            `metric.bind_context` method together with the metric map upon the first interactive map plot.

        Returns
        -------
        metric_map : map_class
            Constructed metric map.
        """
        return self.map_class(coords, values, coords_cols=coords_cols, index=index, index_cols=index_cols, metric=self,
                              agg=agg, bin_size=bin_size, calculate_immediately=calculate_immediately, **context)


def is_metric(metric, metric_class=Metric):
    """Return whether `metric` is an instance or a subclass of `metric_class`."""
    return isinstance(metric, metric_class) or isinstance(metric, type) and issubclass(metric, metric_class)


def initialize_metrics(metrics, metric_class=Metric):
    """Check if all passed `metrics` are instances or subclasses of `metric_class` and have different names. Return a
    list of instantiated metrics and a boolean flag, defining whether a single metric was passed."""
    is_single_metric = is_metric(metrics, metric_class=metric_class)
    metrics = to_list(metrics)
    if not metrics:
        raise ValueError("At least one metric should be passed")
    if not all(is_metric(metric, metric_class=metric_class) for metric in metrics):
        raise TypeError(f"All passed metrics must be either instances or subclasses of {metric_class.__name__}")
    if len({metric.name for metric in metrics}) != len(metrics):
        raise ValueError("Passed metrics must have different names")
    metrics = [metric() if isinstance(metric, type) else metric for metric in metrics]
    return metrics, is_single_metric
