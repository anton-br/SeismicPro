"""Implements Field class - a container of objects of a particular type at different field locations which allows for
their spatial interpolation at given coordinates.

Usually a field is created empty and then iteratively populated with items by calling its `update` method. Each item
being added must have type, defined in the `item_class` attribute of the field class. The only requirement for the
`item_class` is that its instances must have `coords` attribute, containing their spatial coordinates as `Coordinates`
objects. After all items are added, field construction must be finalized by calling `create_interpolator` method which
makes the field callable: now it's able to perform interpolation of items at unknown locations.

The following child classes of `Field` are implemented to cover main types of interpolators being used:
- `SpatialField` - constructs `SpatialInterpolator` and thus requires each item to be convertible to a numeric vector,
- `ValuesAgnosticField` - constructs `ValuesAgnosticInterpolator` which utilizes only information about coordinates. In
  this case the field should be provided with a way to create an instance of `item_class` by averaging other instances
  with given weights.
You can read more about these types of interpolators and cases when one of them is preferable in
:mod:`~utils.interpolation.spatial` docs.

In order to implement a new field one needs to select the appropriate field type, inherit a new class from it and
redefine the following attributes and methods:
- If the base class is `SpatialField`:
    - Set a type of field items to the `item_class` attribute of the field class,
    - Define `item_to_values` `staticmethod` which converts an item to a 1d `np.ndarray` of values that will be passed
      to the field interpolator,
    - Optionally redefine `_interpolate` method if some post-processing of interpolated values is required, by default
      it simply evaluates the field interpolator at the requested coordinates,
    - Define `construct_item` method which creates a new instance of `item_class` from its values.
- If the base class is `ValuesAgnosticField`:
    - Set a type of field items to the `item_class` attribute of the field class,
    - Define `construct_item` method which creates a new instance of `item_class` by averaging a list of objects of the
      same type with given weights.
"""

import warnings

import numpy as np

from .utils import to_list, read_vfunc, dump_vfunc, Coordinates
from .utils.interpolation import IDWInterpolator, DelaunayInterpolator, CloughTocherInterpolator, RBFInterpolator


class Field:
    """Base field class.

    Each concrete subclass must redefine the following attributes and methods:
    - `item_class` class attribute containing the type of items in the field,
    - `available_interpolators` with a mapping from names of available interpolators to the corresponding classes,
    - `values` property returning values to be passed to field interpolator,
    - `construct_items` method that constructs new items at given field coordinates.

    Parameters
    ----------
    items : item_class or list of item_class, optional
        Items to be added to the field on instantiation. If not given, an empty field is created.
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
        mixed.
    interpolator : SpatialInterpolator or None
        Field data interpolator.
    is_dirty_interpolator : bool
        Whether the field was updated after the interpolator was created.
    """
    item_class = None

    def __init__(self, items=None, survey=None, is_geographic=None):
        self.survey = survey
        self.item_container = {}
        self.is_geographic = is_geographic
        self.coords_cols = None
        self.interpolator = None
        self.is_dirty_interpolator = True
        if items is not None:
            self.update(items)

    @property
    def is_empty(self):
        """bool: Whether the field is empty."""
        return len(self.item_container) == 0

    @property
    def has_survey(self):
        """bool: Whether a survey is defined for the filed."""
        return self.survey is not None

    @property
    def has_interpolator(self):
        """bool: Whether the field interpolator was created."""
        return self.interpolator is not None

    @property
    def available_interpolators(self):
        """dict: A mapping from names of available interpolators to the corresponding classes. Must be redefined in
        concrete child classes."""
        return {}

    @property
    def coords(self):
        """2d np.ndarray with shape (n_items, 2): Stacked spatial coordinates of field items."""
        return np.stack(list(self.item_container.keys()))

    @property
    def values(self):
        """np.ndarray or None: Values to be passed to construct an interpolator. Must be redefined in concrete child
        classes."""
        raise NotImplementedError

    def create_interpolator(self, interpolator, **kwargs):
        """Create a field interpolator. Chooses appropriate interpolator type by its name defined by `interpolator` and
        a mapping returned by `self.available_interpolators`."""
        if self.is_empty:
            raise ValueError("Interpolator cannot be created for an empty field")
        interpolator_class = self.available_interpolators.get(interpolator)
        if interpolator_class is None:
            raise ValueError(f"Unknown interpolator {interpolator}. Available options are: "
                             f"{', '.join(self.available_interpolators.keys())}")
        self.interpolator = interpolator_class(self.coords, self.values, **kwargs)
        self.is_dirty_interpolator = False
        return self

    def transform_coords(self, coords, to_geographic=None, is_geographic=None):
        """Cast input `coords` either to geographic or line coordinates depending on the `to_geographic` flag. If the
        flag is not given, `coords` are transformed to coordinate system of the field."""
        if to_geographic is None:
            to_geographic = self.is_geographic
        if is_geographic is None:
            is_geographic = self.is_geographic

        coords_arr = np.array(coords, dtype=np.float32)  # Use float32 since coords won't remain integer after cast
        is_1d_coords = coords_arr.ndim == 1
        if is_1d_coords:
            coords = [coords]
        coords_arr = np.atleast_2d(coords_arr)
        if coords_arr.ndim != 2 or coords_arr.shape[1] != 2:
            raise ValueError("Wrong shape of passed coordinates")

        need_cast_mask = np.full(len(coords), fill_value=(is_geographic is not to_geographic), dtype=bool)
        for i, coord in enumerate(coords):
            if isinstance(coord, Coordinates):
                need_cast_mask[i] = coord.is_geographic is not to_geographic

        if need_cast_mask.any():
            if not self.has_survey or not self.survey.has_inferred_geometry:
                raise ValueError("A survey with inferred geometry must be defined for a field if coords and field "
                                 "are defined in different coordinate systems")
            transformer = self.survey.bins_to_coords if to_geographic else self.survey.coords_to_bins
            coords_arr[need_cast_mask] = transformer(coords_arr[need_cast_mask])

        return coords_arr, coords, is_1d_coords

    def validate_items(self, items):
        #pylint: disable-next=isinstance-second-argument-not-valid-type
        if not all(isinstance(item, self.item_class) for item in items):
            raise TypeError(f"The field can be updated only with instances of {self.item_class} class")
        if not all(hasattr(item, "coords") for item in items):
            raise ValueError("Each item must have coords attribute")
        if not all(isinstance(item.coords, Coordinates) for item in items):
            raise ValueError("The field can be updated only with instances with well-defined coordinates")

    def update(self, items):
        """Add new items to the field. All passed `items` must have not-None coordinates.

        Parameters
        ----------
        items : self.item_class or list of self.item_class
            Items to add to the field.

        Returns
        -------
        self : Field
            `self` with new items added. Changes `item_container` inplace and sets the `is_dirty_interpolator` flag to
            `True` if the `items` list is not empty. Sets `is_geographic` flag during the first update if it was not
            defined during field creation. Resets `coords_cols` attribute if headers, defining coordinates of any item
            being added, differ from those of the field.

        Raises
        ------
        TypeError
            If wrong type of items were found.
        ValueError
            If any of the passed items have `None` coordinates.
        """
        items = to_list(items)
        if not items:
            return self
        self.validate_items(items)

        # Infer is_geographic and coords_cols during the first update
        is_geographic = self.is_geographic
        if self.is_geographic is None:
            is_geographic = items[0].coords.is_geographic

        coords_cols_set = {item.coords.names for item in items}
        coords_cols = coords_cols_set.pop() if len(coords_cols_set) == 1 else None
        if not self.is_empty and coords_cols != self.coords_cols:
            coords_cols = None

        # Update the field
        field_coords, _, _ = self.transform_coords([item.coords for item in items], to_geographic=is_geographic)
        for coords, item in zip(field_coords, items):
            self.item_container[tuple(coords)] = item
        self.is_dirty_interpolator = True
        self.is_geographic = is_geographic
        self.coords_cols = coords_cols
        return self

    def validate_interpolator(self):
        """Verify that field interpolator is created and warn if it's dirty."""
        if not self.has_interpolator:
            raise ValueError("Field interpolator was not created, call create_interpolator method first")
        if self.is_dirty_interpolator:
            warnings.warn("The field was updated after its interpolator was created", RuntimeWarning)

    def construct_items(self, field_coords, items_coords):
        """Evaluate the field at given `field_coords`. `field_coords` are guaranteed to be a 2d `np.ndarray` with
        shape (n_coords, 2), converted to the coordinate system of the field. Each constructed item must have
        coordinates defined by the corresponding values from `items_coords`: unlike `field_coords` they may be defined
        in another coordinate system. Must be redefined in concrete child classes."""
        _ = field_coords, items_coords
        raise NotImplementedError

    def __call__(self, coords, is_geographic=None):
        """Interpolate field items at given locations.

        Parameters
        ----------
        coords : 2-element array-like or 2d np.array with shape (n_coords, 2) or Coordinates or list of Coordinates
            Coordinates to interpolate field items at.

        Returns
        -------
        items : item_class or list of item_class
            Interpolated items.
        """
        self.validate_interpolator()
        field_coords, items_coords, is_1d_coords = self.transform_coords(coords, is_geographic=is_geographic)
        if self.coords_cols is None and not all(isinstance(coords, Coordinates) for coords in items_coords):
            raise ValueError("Names of field coordinates are undefined, so only Coordinates instances are allowed")
        items_coords = [coords if isinstance(coords, Coordinates) else Coordinates(coords, names=self.coords_cols)
                        for coords in items_coords]
        items = self.construct_items(field_coords, items_coords)
        if is_1d_coords:
            return items[0]
        return items


class SpatialField(Field):
    """A field that constructs interpolators of type `SpatialInterpolator`.

    Each concrete subclass must redefine the following attributes and methods:
    - `item_class` class attribute containing the type of items in the field,
    - `item_to_values` method that converts a field item to an `np.ndarray` of its values,
    - `construct_item` method that restores an item from its values,
    - `_interpolate` method to post-process interpolation results (optional).
    """

    @property
    def available_interpolators(self):
        """dict: A mapping from names of available interpolators to the corresponding classes."""
        interpolators = {
            "idw": IDWInterpolator,
            "delaunay": DelaunayInterpolator,
            "ct": CloughTocherInterpolator,
            "rbf": RBFInterpolator,
        }
        return interpolators

    def create_interpolator(self, interpolator, **kwargs):  #pylint: disable=useless-super-delegation
        """Create a field interpolator whose name is defined by `interpolator`.

        Available options are:
        - "idw" - to create `IDWInterpolator`,
        - "delaunay" - to create `DelaunayInterpolator`,
        - "ct" - to create `CloughTocherInterpolator`,
        - "rbf" - to create `RBFInterpolator`.

        Parameters
        ----------
        interpolator : str
            Name of the interpolator to create.
        kwargs : misc, optional
            Additional keyword arguments to be passed to the constructor of interpolator class.

        Returns
        -------
        field : Field
            A field with created interpolator. Sets `is_dirty_interpolator` flag to `False`.
        """
        return super().create_interpolator(interpolator, **kwargs)

    @property
    def values(self):
        """2d np.ndarray with shape (n_items, n_values): Stacked values of items in the field to construct an
        interpolator."""
        return np.stack([self.item_to_values(item) for item in self.item_container.values()])

    @staticmethod
    def item_to_values(item):
        """Convert a field item to a 1d `np.ndarray` of its values being interpolated. Must be redefined in concrete
        child classes."""
        _ = item
        raise NotImplementedError

    def _interpolate(self, coords):
        """Interpolate field values at given `coords`. `coords` are guaranteed to be a 2d `np.ndarray` with
        shape (n_coords, 2), converted to the coordinate system of the field. By default simply evaluates the field
        interpolator at the requested coordinates, may be optionally redefined in child classes if some post-processing
        of interpolated values is required."""
        return self.interpolator(coords)

    def interpolate(self, coords, is_geographic=None):
        """Interpolate values of field items at given locations.

        Parameters
        ----------
        coords : 2-element array-like or 2d np.array with shape (n_coords, 2) or Coordinates or list of Coordinates
            Coordinates to interpolate field values at.

        Returns
        -------
        values : 1d np.array with shape (n_values,) or 2d np.array with shape (n_coords, n_values)
            Interpolated values.
        """
        self.validate_interpolator()
        field_coords, _, is_1d_coords = self.transform_coords(coords, is_geographic=is_geographic)
        values = self._interpolate(field_coords)
        if is_1d_coords:
            return values[0]
        return values

    def construct_item(self, values, coords):
        """Construct an instance of `item_class` from its `values`. Must be redefined in concrete child classes.

        Parameters
        ----------
        values : 1d np.ndarray
            Values to construct an item.
        coords : Coordinates
            Spatial coordinates of an item being constructed.

        Returns
        -------
        item : item_class
            Constructed item.
        """
        _ = values, coords
        raise NotImplementedError

    def construct_items(self, field_coords, items_coords):
        """Evaluate the field at given `field_coords`. `field_coords` are guaranteed to be a 2d `np.ndarray` with
        shape (n_coords, 2), converted to the coordinate system of the field. Each constructed item must have
        coordinates defined by the corresponding values from `items_coords`: unlike `field_coords` they may be defined
        in another coordinate system."""
        values = self._interpolate(field_coords)
        return [self.construct_item(vals, coords) for vals, coords in zip(values, items_coords)]


class ValuesAgnosticField(Field):
    """A field that constructs interpolators of type `ValuesAgnosticInterpolator`.

    Each concrete subclass must redefine the following attributes and methods:
    - `item_class` class attribute containing the type of items in the field,
    - `construct_item` method which creates a new instance of `item_class` by averaging a list of objects of the same
      type with given weights.
    """

    @property
    def available_interpolators(self):
        """dict: A mapping from names of available interpolators to the corresponding classes."""
        interpolators = {
            "idw": IDWInterpolator,
            "delaunay": DelaunayInterpolator,
        }
        return interpolators

    def create_interpolator(self, interpolator, **kwargs):  #pylint: disable=useless-super-delegation
        """Create a field interpolator whose name is defined by `interpolator`.

        Available options are:
        - "idw" - to create `IDWInterpolator`,
        - "delaunay" - to create `DelaunayInterpolator`.

        Parameters
        ----------
        interpolator : str
            Name of the interpolator to create.
        kwargs : misc, optional
            Additional keyword arguments to be passed to the constructor of interpolator class.

        Returns
        -------
        field : Field
            A field with created interpolator. Sets `is_dirty_interpolator` flag to `False`.
        """
        return super().create_interpolator(interpolator, **kwargs)

    @property
    def values(self):
        """None: The field is values-agnostic and does not require values to be passed to the interpolator class."""
        return None

    def construct_item(self, items, weights, coords):
        """Construct a new instance of `item_class` by averaging `items` with corresponding `weights`. Must be
        redefined in concrete child classes.

        Parameters
        ----------
        items : list of item_class
            Items to be aggregated.
        weights : list of float
            Weight of each item in `items`.
        coords : Coordinates
            Spatial coordinates of an item being constructed.

        Returns
        -------
        item : item_class
            Constructed item.
        """
        _ = items, weights, coords
        raise NotImplementedError

    def weights_to_items(self, coords_weights, items_coords):
        """Construct an item for each `dict` in `coords_weights` by averaging field items at coordinates, defined by
        its keys with weights from the corresponding values."""
        res_items = []
        for weights_dict, ret_coords in zip(coords_weights, items_coords):
            items = [self.item_container[coords] for coords in weights_dict.keys()]
            weights = list(weights_dict.values())
            res_items.append(self.construct_item(items, weights, ret_coords))
        return res_items

    def construct_items(self, field_coords, items_coords):
        """Evaluate the field at given `field_coords`. `field_coords` are guaranteed to be a 2d `np.ndarray` with
        shape (n_coords, 2), converted to the coordinate system of the field. Each constructed item must have
        coordinates defined by the corresponding values from `items_coords`: unlike `field_coords` they may be defined
        in another coordinate system."""
        coords_weights = self.interpolator.get_weights(field_coords)
        return self.weights_to_items(coords_weights, items_coords)


class VFUNCFieldMixin:
    """A mixing that defines methods to load and dump a field to a Paradigm Echos VFUNC format. Requires `items_class`
    to be a subclass of `VFUNC`."""

    @classmethod
    def from_file(cls, path, coords_cols=("INLINE_3D", "CROSSLINE_3D"), encoding="UTF-8", survey=None):
        """Init a field from a file with vertical functions in Paradigm Echos VFUNC format.

        The file may have one or more records with the following structure:
        VFUNC [coord_x] [coord_y]
        [x1] [y1] [x2] [y2] ... [xn] [yn]

        Parameters
        ----------
        path : str
            A path to the file.
        coords_cols : tuple with 2 elements, optional, defaults to ("INLINE_3D", "CROSSLINE_3D")
            Names of SEG-Y trace headers representing coordinates of the VFUNCs.
        encoding : str, optional, defaults to "UTF-8"
            File encoding.
        survey : Survey, optional
            A survey the field is describing.

        Returns
        -------
        field : Field
            Constructed field.
        """
        vfunc_data = read_vfunc(path, coords_cols=coords_cols, encoding=encoding)
        items = [cls.item_class(data_x, data_y, coords=coords) for coords, data_x, data_y in vfunc_data]
        return cls(items, survey=survey)

    def dump(self, path, encoding="UTF-8"):
        """Dump all items of the field to a file in Paradigm Echos VFUNC format.

        Notes
        -----
        See more about the format in :func:`~utils.file_utils.dump_vfunc`.

        Parameters
        ----------
        path : str
            A path to the created file.
        encoding : str, optional, defaults to "UTF-8"
            File encoding.
        """
        vfunc_data = [(coords, item.data_x, item.data_y) for coords, item in self.item_container.items()]
        dump_vfunc(path, vfunc_data, encoding=encoding)
