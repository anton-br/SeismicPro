import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from .refractor_velocity import RefractorVelocity
from ..field import SpatialField
from ..utils import to_list, Coordinates, IDWInterpolator


class RefractorVelocityField(SpatialField):
    item_class = RefractorVelocity

    def __init__(self, items=None, n_refractors=None, survey=None, is_geographic=None):
        self.n_refractors = n_refractors
        super().__init__(items, survey, is_geographic)

    @property
    def param_names(self):
        if self.n_refractors is None:
            raise ValueError("The number of refractors is undefined")
        return ["t0"] + [f"x{i}" for i in range(1, self.n_refractors)] + [f"v{i+1}" for i in range(self.n_refractors)]

    def validate_items(self, items):
        super().validate_items(items)
        if len({item.n_refractors for item in items}) != 1:
            raise ValueError("Each RefractorVelocity instance must describe the same number of refractors")

    def update(self, items):
        items = to_list(items)
        super().update(items)
        if items:
            self.n_refractors = items[0].n_refractors
        return self

    @staticmethod
    def item_to_values(item):
        return np.array(list(item.params.values()))

    def _interpolate(self, coords):
        values = self.interpolator(coords)

        # Ensure that t0 is non-negative
        np.clip(values[:, 0], 0, None, out=values[:, 0])

        # Ensure that velocities of refractors are non-negative and increasing
        velocities = values[:, self.n_refractors:]
        np.clip(velocities[:, 0], 0, None, out=velocities[:, 0])
        np.maximum.accumulate(velocities, axis=1, out=velocities)

        # Ensure that crossover offsets are non-negative and increasing
        if self.n_refractors > 1:
            cross_offsets = values[:, 1:self.n_refractors]
            np.clip(cross_offsets[:, 0], 0, None, out=cross_offsets[:, 0])
            np.maximum.accumulate(cross_offsets, axis=1, out=cross_offsets)

        return values

    def construct_item(self, values, coords):
        return self.item_class.from_params(dict(zip(self.param_names, values)), coords=coords)

    def dump(self, path, encoding="UTF-8", col_size=11):
        """Save the RefractorVelocityField instance to a file.

        File example:
        SourceX   SourceY        t0        x1        v1        v2
        1111100   2222220     50.00   1000.00   1500.00   2000.00
        ...
        1111200   2222240     60.00   1050.00   1550.00   1950.00

        Parameters
        ----------
        path : str
            Path to the file.
        encoding : str, optional, defaults to "UTF-8"
            File encoding.
        col_size : int, defaults to 10
            Size of each columns in file. `col_size` will be increased for coordinate columns if coordinate names
            are longer.

        Returns
        -------
        self : RefractorVelocityField
            RefractorVelocityField unchanged.

        Raises
        ------
        ValueError
            If RefractorVelocityField is empty.
        """
        if self.is_empty:
            raise ValueError("Field is empty. Could not dump empty field.")
        new_col_size = max(col_size, max(len(name) for name in self.coords_cols) + 1)
        values = np.array([list(item.params.values()) for coords, item in self.item_container.items()])

        columns = list(self.coords_cols) + list(self.param_names)
        cols_format = '{:>{new_col_size}}' * 2 + '{:>{col_size}}' * (len(columns) - 2)
        cols_str = cols_format.format(*columns, new_col_size=new_col_size, col_size=col_size)

        data = np.hstack((self.coords, values))
        data_format = ('\n' + "{:>{new_col_size}.0f}" * 2 + '{:>{col_size}.2f}' * 2 * self.n_refractors) * data.shape[0]
        data_str = data_format.format(*data.ravel(), new_col_size=new_col_size, col_size=col_size)

        with open(path, 'w', encoding=encoding) as f:
            f.write(cols_str + data_str)
        return self

    @classmethod
    def load(cls, path, encoding="UTF-8"):
        """Load RefractorVelocityField from a file.

        File example:
        SourceX   SourceY        t0        x1        v1        v2
        1111100   2222220     50.00   1000.00   1500.00   2000.00
        ...
        1111200   2222240     60.00   1050.00   1550.00   1950.00

        Parameters
        ----------
        path : str,
            path to the file.
        encoding : str, defaults to "UTF-8"
            File encoding.

        Returns
        -------
        self : RefractorVelocityField
            RefractorVelocityField instance created from a file.
        """
        self = cls()
        df = pd.read_csv(path, sep=r'\s+', encoding=encoding)
        self.n_refractors = (len(df.columns) - 2) // 2
        rv_list = []
        for row in df.to_numpy():
            params = dict(zip(self.param_names, row[2:]))
            coords = Coordinates(names=tuple(df.columns[:2]), coords=tuple(row[:2]))
            rv = RefractorVelocity.from_params(params=params, coords=coords)
            rv_list.append(rv)
        self.update(rv_list)
        return self

    def plot(self, mode="grid", grid_size=200):
        """Plot field parameteres on the grid.

        Plot each parameters on a separate axis with expected values. Expected values calculate by the grid.

        Parameters
        ----------
        mode : str, optional defualts to "grid". Should be one of "grid" or "items:
            "grid" mode use interpolator to calc expected values and show it
            "items" mode shows values stored in items_container.
        grid_size: int, defaults to 200
            Grid size for calc values

        """
        n_items = len(self.param_names)
        if mode == "grid":
            if self.is_dirty_interpolator:
                raise ValueError("Update or create interpolator first")
            data_coords, data_params = self._calc_plot_data_by_grid(grid_size=grid_size)
        elif mode == "items":
            data_coords, data_params = self._calc_plot_data_by_items()
        fig, ax = plt.subplots(nrows=1, ncols=n_items, figsize=(n_items * 8, 7))
        for i in range(n_items):
            img = ax[i].scatter(data_coords[:, 0], data_coords[:, 1], c=data_params[:, i], s=10)
            ax[i].set_title(self.param_names[i])
            fig.colorbar(img, ax=ax[i])
        return self

    def _calc_plot_data_by_items(self):
        n_items = len(self.param_names)
        data_params = np.empty(shape=(len(self.item_container), n_items))
        data_coords = np.empty(shape=(len(self.item_container), 2))
        for i, (coords, rv) in enumerate(self.item_container.items()):
            data_coords[i] = np.array(coords)
            data_params[i] = list(rv.params.values())
        return data_coords, data_params

    def _calc_plot_data_by_grid(self, grid_size):
        # TODO: test with field constructed with supergather
        n_items = len(self.param_names)
        min_x, min_y = np.min(self.coords, axis=0)
        max_x, max_y = np.max(self.coords, axis=0)
        grid_x = np.linspace(min_x, max_x, int((max_x - min_x) // grid_size) + 1)
        grid_y = np.linspace(min_y, max_y, int((max_y - min_y) // grid_size) + 1)

        data_params = np.empty(shape=(grid_x.shape[0] * grid_y.shape[0], n_items))
        data_coords = np.empty(shape=(grid_x.shape[0] * grid_y.shape[0], 2))
        x, y = np.meshgrid(grid_x, grid_y)

        for i, (x, y) in enumerate(zip(x.ravel(), y.ravel())):
            coords = Coordinates(coords=(x, y), names=self.coords_cols)
            data_coords[i] = np.array([x, y])
            data_params[i] = list(self(coords).params.values())
        return data_coords, data_params
