"""Implements a metric that tracks a pipeline in which it was calculated and allows for automatic plotting of batch
components on its interactive maps"""

import warnings
from inspect import signature
from functools import partial
from collections import defaultdict

import numpy as np
import pandas as pd

from .metrics import define_metric, Metric, PartialMetric
from ..utils import to_list, get_first_defined
from ...batchflow import Pipeline


def pass_coords(method):
    """Indicate that the decorated view plotter should be provided with click coordinates besides `ax`."""
    method.args_unpacking_mode = "coords"
    return classmethod(method)


def pass_batch(method):
    """Indicate that the decorated view plotter should be provided with a batch for which `calculate_metric` method was
    called besides `ax`."""
    method.args_unpacking_mode = "batch"
    return classmethod(method)


def pass_calc_args(method):
    """Indicate that the decorated view plotter should be provided with all arguments passed to the metric `calc`
    method besides `ax`."""
    method.args_unpacking_mode = "calc_args"
    return classmethod(method)


class PipelineMetric(Metric):
    """Define a metric that tracks a pipeline in which it was calculated and allows for automatic plotting of batch
    components on its interactive maps.

    Examples
    --------
    Define a metric, that calculates standard deviation of gather amplitudes:
    >>> class StdMetric(PipelineMetric):
    ...     name = "std"
    ...     min_value = 0
    ...     max_value = None
    ...     is_lower_better = None
    ...     args_to_unpack = "gather"
    ...
    ...     @classmethod
    ...     def calc(cls, gather):
    ...         return gather.data.std()
    Note that the defined `calc` method operates on each batch item independently.

    Calculate the metric for a given dataset:
    >>> survey = Survey(path, header_index="FieldRecord", header_cols=["SourceY", "SourceX", "offset"], name="raw")
    >>> dataset = SeismicDataset(survey)
    >>> pipeline = (dataset
    ...     .pipeline()
    ...     .load(src="raw")
    ...     .calculate_metric(StdMetric, gather="raw", save_to=V("accumulator", mode="a"))
    ... )
    >>> pipeline.run(batch_size=16, n_epochs=1)

    `PipelineMetric` tracks a pipeline in which it was calculated. This allows reconstructing the batch used to compute
    the metric and plot its components on click on the interactive metric map:
    >>> std_map = pipeline.v("accumulator").construct_map()
    >>> std_map.plot(interactive=True, plot_component="raw")

    If a `pipeline` argument is passed, it will be used instead of the one used to calculate the metric:
    >>> plot_pipeline = Pipeline().load(src="raw").sort(src="raw", dst="sorted", by="offset")
    >>> std_map.plot(interactive=True, pipeline=plot_pipeline, plot_component="sorted")

    If several components are passed, a multiview plot is created:
    >>> std_map.plot(interactive=True, pipeline=plot_pipeline, plot_component=["raw", "sorted"])

    By default, the batch to execute the pipeline for is generated by the dataset index, corresponding to click
    coordinates which allows running the same pipeline used for metric computation. Click coordinates can be used
    directly to generate the batch, but in this case the default pipeline will simply load the requested component by
    the survey name:
    >>> std_map.plot(interactive=True, batch_src="coords", plot_component="raw")

    However, a `pipeline` can be specified as well as in the index case:
    >>> std_map.plot(interactive=True, batch_src="coords", pipeline=plot_pipeline, plot_component=["raw", "sorted"])

    A `PipelineMetric` allows for defining default views. Each of them must be a `classmethod` decorated with one of
    @pass_coords, @pass_batch or @pass_calc_args decorators that specify which additional arguments will be passed to
    the view along with the axes to plot on. Each of the views must be listed in the `views` class attribute. The
    following class extends the `StdMetric` class with two views: one plotting the gather used to calculate the metric
    itself and the other plotting the same gather sorted by offset.
    >>> class PlotStdMetric(StdMetric):
    ...     views = ("plot", "plot_sorted")
    ...
    ...     @pass_calc_args
    ...     def plot(cls, gather, ax, **kwargs):
    ...         return gather.plot(ax=ax, **kwargs)
    ...
    ...     @pass_calc_args
    ...     def plot_sorted(cls, gather, ax, **kwargs):
    ...         return gather.sort(by="offset").plot(ax=ax, **kwargs)

    In this case an interactive plot of the map may be constructed without any extra arguments:
    >>> pipeline = (dataset
    ...     .pipeline()
    ...     .load(src="raw")
    ...     .calculate_metric(PlotStdMetric, gather="raw", save_to=V("accumulator", mode="a"))
    ... )
    >>> pipeline.run(batch_size=16, n_epochs=1)
    >>> std_map = pipeline.v("accumulator").construct_map()
    >>> std_map.plot(interactive=True)

    Parameters
    ----------
    pipeline : Pipeline
        A pipeline used to calculate the metric.
    calculate_metric_index : int
        An ordinal number of the `calculate_metric` action produced the current metric.
    coords_cols : array-like with 2 elements, optional
        Names of the dataset headers used to extract X and Y coordinates from.
    coords_to_indices : dict
        A mapping from spatial coordinates to the corresponding indices of the dataset for which the pipeline was
        executed.
    kwargs : misc, optional
        Additional keyword arguments to :func:`~Metric.__init__`.

    Attributes
    ----------
    args_to_unpack : str or list of str or "all"
        `calc` method arguments to unpack. Unpacking is performed in the following way:
        * If argument value is `str`, it is treated as a batch component name to get the actual argument from,
        * If argument value is an array-like whose length matches the length of the batch, its elements are passed to
          `calc` methods for the corresponding batch items.
        * Otherwise the argument value is passed to `calc` methods for all batch items.
        If "all", tries to unpack all the arguments.
    views : str or iterable of str
        Default views of the metric to display on click on a metric map in interactive mode.
    dataset : SeismicDataset
        The dataset for which the metric was calculated.
    coords_dataset : SeismicDataset
        The `dataset` reindexed by `coords_cols`.
    plot_pipeline : Pipeline
        The `pipeline` up to the `calculate_metric` method.
    coords_cols : array-like with 2 elements, optional
        Names of the dataset headers used to extract X and Y coordinates from.
    coords_to_indices : dict
        A mapping from spatial coordinates to the corresponding indices of the dataset for which the pipeline was
        executed.
    """
    args_to_unpack = "all"

    def __init__(self, pipeline, calculate_metric_index, coords_cols, coords_to_pos=None, **kwargs):
        # coords_to_indices = None
        # if self.stores_indices:
        #     # Rename metrics coordinates columns to avoid possible collision with index names which breaks groupby
        #     renamed_metrics = self.metrics.rename(columns=dict(zip(self.coords_cols, ["X", "Y"])))
        #     coords_to_indices = renamed_metrics.groupby(by=["X", "Y"]).groups
        #     coords_to_indices = {coords: indices.unique() for coords, indices in coords_to_indices.items()}

        super().__init__(**kwargs)
        self.dataset = pipeline.dataset
        self.coords_dataset = self.dataset.reindex(coords_cols, recursive=False)

        self.coords_cols = coords_cols
        self.coords_to_pos = coords_to_pos

        # Slice the pipeline in which the metric was calculated up to its calculate_metric call
        calculate_metric_indices = [i for i, action in enumerate(pipeline._actions)
                                      if action["name"] == "calculate_metric"]
        calculate_metric_action_index = calculate_metric_indices[calculate_metric_index]
        actions = pipeline._actions[:calculate_metric_action_index]
        self.plot_pipeline = Pipeline(pipeline=pipeline, actions=actions)

        # Get args and kwargs of the calculate_metric call with possible named expressions in them
        self.calculate_metric_args = pipeline._actions[calculate_metric_action_index]["args"]
        self.calculate_metric_kwargs = pipeline._actions[calculate_metric_action_index]["kwargs"]

    @classmethod
    def calc(cls, metric):
        """Return an already calculated metric. May be overridden in child classes."""
        return metric

    @staticmethod
    def combine_init_params(*params):
        # TODO: validate all other attributes for is-equality
        params_coords_to_pos = [param["coords_to_pos"] for param in params if "coords_to_pos" in param]
        if not params_coords_to_pos:
            return params[-1]
        merged_coords_to_pos = defaultdict(list)
        for coords_to_pos in params_coords_to_pos:
            for key, val in coords_to_pos.items():
                merged_coords_to_pos[key].extend(val)
        return {**params[-1], "coords_to_pos": merged_coords_to_pos}

    def make_batch(self, coords, batch_src, pipeline):
        """Construct a batch for given spatial `coords` and execute the `pipeline` for it. The batch can be generated
        either directly from coords if `batch_src` is "coords" or from the corresponding index if `batch_src` is
        "index"."""
        if batch_src not in {"index", "coords"}:
            raise ValueError("Unknown source to get the batch from. Available options are 'index' and 'coords'.")
        if batch_src == "index":
            if self.coords_to_pos is None:
                raise ValueError("Unable to use indices to get the batch by coordinates since they were not passed "
                                 "during metric instantiation. Please specify batch_src='coords'.")
            subset = self.dataset.create_subset(self.dataset.subset_by_pos(self.coords_to_pos[coords]))
        else:
            indices = []
            for concat_id in range(self.coords_dataset.index.next_concat_id):
                index_candidate = (concat_id,) + coords
                if index_candidate in self.coords_dataset.index.index_to_headers_pos:
                    indices.append(index_candidate)
            subset = self.coords_dataset.create_subset(pd.MultiIndex.from_tuples(indices))

        if len(subset) > 1:
            # TODO: try moving to MapBinPlot in this case
            warnings.warn("Multiple gathers exist for given coordinates, only the first one is shown", RuntimeWarning)
        batch = subset.next_batch(1, shuffle=False)
        batch = pipeline.execute_for(batch)
        return batch

    @classmethod
    def unpack_calc_args(cls, batch, *args, **kwargs):
        """Unpack arguments for metric calculation depending on the `args_to_unpack` class attribute and return them
        with the first unpacked `calc` argument. If `args_to_unpack` equals "all", tries to unpack all the passed
        arguments.

        Unpacking is performed in the following way:
        * If argument value is `str`, it is treated as a batch component name to get the actual argument from,
        * If argument value is an array-like whose length matches the length of the batch, its elements are passed to
          `calc` methods for the corresponding batch items.
        * Otherwise the argument value is passed to `calc` methods for all batch items.
        """
        sign = signature(cls.calc)
        bound_args = sign.bind(*args, **kwargs)

        # Determine PipelineMetric.calc arguments to unpack
        if cls.args_to_unpack is None:
            args_to_unpack = set()
        elif cls.args_to_unpack == "all":
            args_to_unpack = {name for name, param in sign.parameters.items()
                                   if param.kind not in {param.VAR_POSITIONAL, param.VAR_KEYWORD}}
        else:
            args_to_unpack = set(to_list(cls.args_to_unpack))

        # Convert the value of each argument to an array-like matching the length of the batch
        packed_args = {}
        for arg, val in bound_args.arguments.items():
            if arg in args_to_unpack:
                if isinstance(val, str):
                    packed_args[arg] = getattr(batch, val)
                elif isinstance(val, (tuple, list, np.ndarray)) and len(val) == len(batch):
                    packed_args[arg] = val
                else:
                    packed_args[arg] = [val] * len(batch)
            else:
                packed_args[arg] = [val] * len(batch)

        # Extract the values of the first calc argument to use them as a default source for coordinates calculation
        first_arg = packed_args[list(sign.parameters.keys())[0]]

        # Convert packed args dict to a list of calc args and kwargs for each of the batch items
        unpacked_args = []
        for values in zip(*packed_args.values()):
            bound_args.arguments = dict(zip(packed_args.keys(), values))
            unpacked_args.append((bound_args.args, bound_args.kwargs))
        return unpacked_args, first_arg

    def eval_calc_args(self, batch):
        """Evaluate named expressions in arguments passed to `calc` method and unpack arguments for the first batch
        item."""
        sign = signature(batch.calculate_metric)
        bound_args = sign.bind(*self.calculate_metric_args, **self.calculate_metric_kwargs)
        bound_args.apply_defaults()
        # pylint: disable=protected-access
        calc_args = self.plot_pipeline._eval_expr(bound_args.arguments["args"], batch=batch)
        calc_kwargs = self.plot_pipeline._eval_expr(bound_args.arguments["kwargs"], batch=batch)
        # pylint: enable=protected-access
        args, _ = self.unpack_calc_args(batch, *calc_args, **calc_kwargs)
        return args[0]

    def plot_component(self, coords, ax, batch_src, pipeline, plot_component, **kwargs):
        """Construct a batch by click coordinates and plot its component."""
        default_pipelines = {
            "index": self.plot_pipeline,
            "coords": Pipeline().load(src=plot_component)
        }
        if pipeline is None:
            pipeline = default_pipelines[batch_src]
        batch = self.make_batch(coords, batch_src, pipeline)
        item = getattr(batch, plot_component)[0]
        item.plot(ax=ax, **kwargs)

    def plot_view(self, coords, ax, batch_src, pipeline, view_fn, **kwargs):
        """Plot a given metric view. Pass extra arguments depending on `@pass_*` decorator."""
        if view_fn.args_unpacking_mode == "coords":
            return view_fn(coords, ax=ax, **kwargs)

        if pipeline is None:
            if batch_src == "coords":
                raise ValueError("A pipeline must be passed to plot a view if a batch is generated from coordinates")
            pipeline = self.plot_pipeline
        batch = self.make_batch(coords, batch_src, pipeline)

        if view_fn.args_unpacking_mode == "batch":
            return view_fn(batch, ax=ax, **kwargs)

        coords_args, coords_kwargs = self.eval_calc_args(batch)
        return view_fn(*coords_args, ax=ax, **coords_kwargs, **kwargs)

    def get_views(self, batch_src="index", pipeline=None, plot_component=None, **kwargs):
        """Get metric views by parameters passed to interactive metric map plotter. If `plot_component` is given,
        batch components are displayed. Otherwise defined metric views are shown."""
        if plot_component is not None:
            return [partial(self.plot_component, batch_src=batch_src, pipeline=pipeline, plot_component=component)
                    for component in to_list(plot_component)], kwargs

        view_fns = [getattr(self, view) for view in to_list(self.views)]
        if not all(hasattr(view_fn, "args_unpacking_mode") for view_fn in view_fns):
            raise ValueError("Each metric view must be decorated with @pass_coords, @pass_batch or @pass_calc_args")
        return [partial(self.plot_view, batch_src=batch_src, pipeline=pipeline, view_fn=view_fn)
                for view_fn in view_fns], kwargs


def define_pipeline_metric(metric, metric_name):
    """Define a new `PipelineMetric` from a `callable` or another `PipelineMetric`. In the first case, the `callable`
    defines `calc` method of the metric. In the latter case only the new metric name is being set."""
    is_metric_type = isinstance(metric, type) and issubclass(metric, PipelineMetric)
    is_callable = not isinstance(metric, type) and callable(metric)
    if not (is_metric_type or is_callable):
        raise ValueError(f"metric must be either a subclass of PipelineMetric or a callable but {type(metric)} given")

    if is_callable:
        metric_name = get_first_defined(metric_name, metric.__name__)
        if metric_name == "<lambda>":
            raise ValueError("metric_name must be passed for lambda metrics")
        return define_metric(base_cls=PipelineMetric, name=metric_name, calc=staticmethod(metric))

    metric_name = get_first_defined(metric_name, metric.name)
    if metric_name is None:
        raise ValueError("metric_name must be passed if not defined in metric class")
    return PartialMetric(metric, name=metric_name)
