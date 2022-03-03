import warnings
from inspect import signature
from functools import partial

import numpy as np
import pandas as pd

from .metrics import PlottableMetric
from ..utils import to_list
from ...batchflow import Pipeline


def pass_coords(method):
    method.args_unpacking_mode = "coords"
    return staticmethod(method)


def pass_batch(method):
    method.args_unpacking_mode = "batch"
    return staticmethod(method)


def pass_calc_args(method):
    method.args_unpacking_mode = "calc_args"
    return staticmethod(method)


class PipelineMetric(PlottableMetric):
    args_to_unpack = "all"
    views = tuple()

    def __init__(self, pipeline, calculate_metric_index, coords_cols, coords_to_indices, **kwargs):
        super().__init__(**kwargs)
        self.dataset = pipeline.dataset
        self.coords_dataset = self.dataset.copy().reindex(coords_cols)

        self.coords_cols = coords_cols
        self.coords_to_indices = coords_to_indices

        # Slice the pipeline in which the metric was calculated up to its calculate_metric call
        calculate_metric_indices = [i for i, action in enumerate(pipeline._actions)
                                      if action["name"] == "calculate_metric"]
        calculate_metric_action_index = calculate_metric_indices[calculate_metric_index]
        actions = pipeline._actions[:calculate_metric_action_index]
        self.plot_pipeline = Pipeline(pipeline=pipeline, actions=actions)

        # Get args and kwargs of the calculate_metric call with possible named expressions in them
        self.calculate_metric_args = pipeline._actions[calculate_metric_action_index]["args"]
        self.calculate_metric_kwargs = pipeline._actions[calculate_metric_action_index]["kwargs"]

    @staticmethod
    def calc(metric):
        return metric

    def gen_batch(self, coords, batch_src, pipeline):
        if batch_src not in {"index", "coords"}:
            raise ValueError("Unknown source to get the batch from. Available options are 'index' and 'coords'.")
        if batch_src == "index":
            if self.coords_to_indices is None:
                raise ValueError("Unable to use indices to get the batch by coordinates since they were not passed "
                                 "during metric instantiation. Please specify batch_src='coords'.")
            subset = self.dataset.create_subset(self.coords_to_indices[coords])
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
        sign = signature(cls.calc)
        bound_args = sign.bind(*args, **kwargs)

        # Determine Metric.calc arguments to unpack
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
        # Get params passed to Metric.calc with possible named expressions and evaluate them
        sign = signature(batch.calculate_metric)
        bound_args = sign.bind(*self.calculate_metric_args, **self.calculate_metric_kwargs)
        bound_args.apply_defaults()
        calc_args = self.plot_pipeline._eval_expr(bound_args.arguments["args"], batch=batch)
        calc_kwargs = self.plot_pipeline._eval_expr(bound_args.arguments["kwargs"], batch=batch)
        args, _ = self.unpack_calc_args(batch, *calc_args, **calc_kwargs)
        return args[0]

    def plot_component(self, coords, ax, batch_src, pipeline, plot_component, **kwargs):
        default_pipelines = {
            "index": self.plot_pipeline,
            "coords": Pipeline().load(src=plot_component)
        }
        if pipeline is None:
            pipeline = default_pipelines[batch_src]
        batch = self.gen_batch(coords, batch_src, pipeline)
        item = getattr(batch, plot_component)[0]
        item.plot(ax=ax, **kwargs)

    def plot_view(self, coords, ax, batch_src, pipeline, view_fn, **kwargs):
        if view_fn.args_unpacking_mode == "coords":
            return view_fn(coords, ax=ax, **kwargs)

        if pipeline is None:
            if batch_src == "coords":
                raise ValueError("A pipeline must be passed to plot a view if a batch is generated from coordinates")
            pipeline = self.plot_pipeline
        batch = self.gen_batch(coords, batch_src, pipeline)

        if view_fn.args_unpacking_mode == "batch":
            return view_fn(batch, ax=ax, **kwargs)

        coords_args, coords_kwargs = self.eval_calc_args(batch)
        return view_fn(*coords_args, ax=ax, **coords_kwargs, **kwargs)

    def get_views(self, batch_src, pipeline, plot_component, *args, **kwargs):
        _ = args, kwargs
        if plot_component is not None:
            return [partial(self.plot_component, batch_src=batch_src, pipeline=pipeline, plot_component=component)
                    for component in to_list(plot_component)]

        view_fns = [getattr(self, view) for view in to_list(self.views)]
        if not all(hasattr(view_fn, "args_unpacking_mode") for view_fn in view_fns):
            raise ValueError("Each metric view must be decorated with @coords, @batch or @calc_args decorator")
        return [partial(self.plot_view, batch_src=batch_src, pipeline=pipeline, view_fn=view_fn)
                for view_fn in view_fns]
