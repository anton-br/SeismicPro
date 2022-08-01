"""Implements a StackingVelocityField class which stores stacking velocities calculated at different field locations
and allows for spatial velocity interpolation"""

import os

import numpy as np
from tqdm.contrib.concurrent import thread_map
from sklearn.neighbors import NearestNeighbors

from .stacking_velocity import StackingVelocity
from .metrics import VELOCITY_QC_METRICS, StackingVelocityMetric
from ..field import ValuesAgnosticField, VFUNCFieldMixin
from ..utils import to_list, IDWInterpolator


class StackingVelocityField(ValuesAgnosticField, VFUNCFieldMixin):
    """A class for storing and interpolating stacking velocity data over a field.

    Velocities used for seismic cube stacking are usually picked on a sparse grid of inlines and crosslines and then
    interpolated over the whole field in order to reduce computational costs. Such interpolation can be performed by
    `StackingVelocityField` class which provides an interface to obtain stacking velocity at given spatial coordinates
    via its `__call__` method.

    A field can be populated with stacking velocities in 3 main ways:
    - by passing precalculated velocities in the `__init__`,
    - by creating an empty field and then iteratively updating it with calculated stacking velocities using `update`,
    - by loading a field from a file of vertical functions via its `from_file` `classmethod`.
    After all velocities are added, an interpolator must be created by running `create_interpolator` method to make the
    field callable.

    The field provides an interface to its quality control via `qc` method, which returns maps for several
    spatial-window-based metrics calculated for its stacking velocities. These maps may be interactively visualized to
    assess field quality in detail.

    Examples
    --------
    A field can be created empty and updated with instances of `StackingVelocity` class:
    >>> field = StackingVelocityField()
    >>> velocity = StackingVelocity(times=[0, 1000, 2000, 3000], velocities=[1500, 2000, 2800, 3400],
    ...                             coords=Coordinates((20, 40), names=("INLINE_3D", "CROSSLINE_3D")))
    >>> field.update(velocity)

    Or created from precalculated instances:
    >>> field = StackingVelocityField(list_of_stacking_velocities)

    Or simply loaded from a file of vertical functions:
    >>> field = StackingVelocityField.from_file(path)

    Field construction must be finalized with `create_interpolator` method call:
    >>> field.create_interpolator("idw")

    Now the field allows for velocity interpolation at given coordinates:
    >>> velocity = field((10, 10))

    Quality control can be performed by calling `qc` method and visualizing the resulting maps:
    >>> metrics_maps = cube.qc(radius=40, times=np.arange(0, 3000, 2))
    >>> for metric_map in metrics_maps:
    >>>     metric_map.plot(interactive=True)

    Parameters
    ----------
    items : StackingVelocity or list of StackingVelocity, optional
        Stacking velocities to be added to the field on instantiation. If not given, an empty field is created.
    survey : Survey, optional
        A survey the field is describing.
    is_geographic : bool, optional
        Coordinate system of the field: either geographic (e.g. (CDP_X, CDP_Y)) or line-based (e.g. (INLINE_3D,
        CROSSLINE_3D)). Inferred automatically on the first update if not given.

    Attributes
    ----------
    survey : Survey or None
        A survey the field is describing. `None` if not specified during instantiation.
    item_container : dict
        A mapping from coordinates of field items as 2-element tuples to the items themselves.
    is_geographic : bool
        Whether coordinate system of the field is geographic. `None` for an empty field if was not specified during
        instantiation.
    coords_cols : tuple with 2 elements or None
        Names of SEG-Y trace headers representing coordinates of items in the field. `None` if names of coordinates are
        mixed or the field is empty.
    interpolator : SpatialInterpolator or None
        Field data interpolator.
    is_dirty_interpolator : bool
        Whether the field was updated after the interpolator was created.
    """
    item_class = StackingVelocity

    def construct_item(self, items, weights, coords):
        """Construct a new stacking velocity by averaging other stacking velocities with corresponding weights.

        Parameters
        ----------
        items : list of StackingVelocity
            Stacking velocities to be aggregated.
        weights : list of float
            Weight of each item in `items`.
        coords : Coordinates
            Spatial coordinates of a stacking velocity being constructed.

        Returns
        -------
        item : StackingVelocity
            Constructed stacking velocity instance.
        """
        return self.item_class.from_stacking_velocities(items, weights, coords=coords)

    @property
    def mean_velocity(self):
        """StackingVelocity: Mean stacking velocity over the field."""
        return self.item_class.from_stacking_velocities(list(self.item_container.values()))

    def smooth(self, radius):
        """Smooth the field by averaging its stacking velocities within given radius.

        Parameters
        ----------
        radius : positive float
            Spatial window radius (Euclidean distance).

        Returns
        -------
        field : StackingVelocityField
            Smoothed field.
        """
        smoothing_interpolator = IDWInterpolator(self.coords, radius=radius, dist_transform=0)
        weights = smoothing_interpolator.get_weights(self.coords)
        items_coords = [item.coords for item in self.item_container.values()]
        smoothed_items = self.weights_to_items(weights, items_coords)
        return type(self)(survey=self.survey, is_geographic=self.is_geographic).update(smoothed_items)

    def interpolate(self, coords, times, is_geographic=None):
        """Interpolate stacking velocities at given `coords` and `times`.

        Interpolation over a regular grid of times allows implementing a much more efficient computation strategy than
        simply iteratively calling the interpolator for each of `coords` and than evaluating the obtained stacking
        velocities at `times`.

        Parameters
        ----------
        coords : 2d np.array or list of Coordinates
            Coordinates to interpolate stacking velocities at.
        times : 1d array-like
            Times to interpolate stacking velocities at.

        Returns
        -------
        velocities : 2d np.ndarray
            Interpolated stacking velocities at given `coords` and `times`. Has shape (n_coords, n_times).
        """
        self.validate_interpolator()
        field_coords, _, _ = self.transform_coords(coords, is_geographic=is_geographic)
        times = np.atleast_1d(times)
        weights = self.interpolator.get_weights(field_coords)
        base_velocities_coords = set.union(*[set(weights_dict.keys()) for weights_dict in weights])
        base_velocities = {coords: self.item_container[coords](times) for coords in base_velocities_coords}

        res = np.zeros((len(field_coords), len(times)))
        for i, weights_dict in enumerate(weights):
            for coord, weight in weights_dict.items():
                res[i] += base_velocities[coord] * weight
        return res

    #pylint: disable-next=invalid-name
    def qc(self, radius, metrics=None, coords=None, times=None, n_workers=None, bar=True):
        """Perform quality control of the velocity field by calculating spatial-window-based metrics for its stacking
        velocities evaluated at given `coords` and `times`.

        If `coords` are not given, coordinates of items in the field are used, but interpolation is performed as well.
        If `times` are not given, samples of the underlying `Survey` are used if it is defined.

        By default, the following metrics are calculated:
        * Presence of segments with velocity decrease in time,
        * Maximal deviation of instantaneous acceleration from the mean acceleration over all times,
        * Maximal spatial velocity standard deviation in a window over all times,
        * Maximal absolute relative difference between central stacking velocity and the average of all remaining
          velocities in the window over all times.

        Parameters
        ----------
        radius : positive float
            Spatial window radius (Euclidean distance).
        metrics : StackingVelocityMetric or list of StackingVelocityMetric, optional
            Metrics to calculate. Defaults to those defined in `~metrics.VELOCITY_QC_METRICS`.
        coords : 2d np.array or list of Coordinates or None, optional
            Spatial coordinates of stacking velocities to calculate metrics for. If not given, coordinates of items in
            the field are used.
        times : 1d array-like
            Times to calculate metrics for. Measured in milliseconds.
        n_workers : int, optional
            The number of threads to be spawned to calculate metrics. Defaults to the number of cpu cores.
        bar : bool, optional, defaults to True
            Whether to show a progress bar.

        Returns
        -------
        metrics_maps : StackingVelocityMetricMap or list of StackingVelocityMetricMap
            Calculated metrics maps. Has the same shape as `metrics`.
        """
        is_single_metric = isinstance(metrics, type) and issubclass(metrics, StackingVelocityMetric)
        if metrics is None:
            metrics = VELOCITY_QC_METRICS
        metrics = to_list(metrics)
        if not metrics:
            raise ValueError("At least one metric should be passed")
        if not all(isinstance(metric, type) and issubclass(metric, StackingVelocityMetric) for metric in metrics):
            raise ValueError("All passed metrics must be subclasses of StackingVelocityMetric")

        # Set default coords and times
        if coords is None:
            coords = self.coords
        if times is None:
            if not self.has_survey:
                raise ValueError("times must be passed if the field is not linked with a survey")
            times = self.survey.times

        # Calculate stacking velocities at given times for each of coords
        velocities = self.interpolate(coords, times)

        # Select all neighboring stacking velocities for each of coords
        if n_workers is None:
            n_workers = os.cpu_count()
        coords_neighbors = NearestNeighbors(radius=radius, n_jobs=n_workers).fit(coords)
        # Sort results to guarantee that central stacking velocity of each window will have index 0
        _, windows_indices = coords_neighbors.radius_neighbors(coords, return_distance=True, sort_results=True)

        # Initialize metrics and calculate them
        def calc_metrics(window_indices):
            window = velocities[window_indices]
            return [metric.calc(window if metric.is_window_metric else window[0], times) for metric in metrics]

        metrics = [metric(times, velocities, coords_neighbors) for metric in metrics]
        results = thread_map(calc_metrics, windows_indices, max_workers=n_workers,
                             desc="Coordinates processed", disable=not bar)
        metrics_maps = [metric.map_class(coords, metric_values, coords_cols=self.coords_cols, metric=metric)
                        for metric, metric_values in zip(metrics, zip(*results))]
        if is_single_metric:
            return metrics_maps[0]
        return metrics_maps