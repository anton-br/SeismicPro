"""Miscellaneous utility functions for refractor velocity estimation"""

import numpy as np
import pandas as pd

from ..utils import Coordinates, to_list


def get_param_names(n_refractors):
    """Return names of parameters of a near-surface velocity model describing given number of refractors."""
    return ["t0"] + [f"x{i}" for i in range(1, n_refractors)] + [f"v{i}" for i in range(1, n_refractors + 1)]


def postprocess_params(params):
    """Postprocess array of parameters of a near-surface velocity model so that the following constraints are
    satisfied:
    - Intercept time is non-negative,
    - Crossover offsets are non-negative and increasing,
    - Velocities of refractors are non-negative and increasing.
    """
    is_1d = (params.ndim == 1)

    # Ensure that all params are non-negative
    params = np.clip(np.atleast_2d(params), 0, None)

    # Ensure that velocities of refractors and crossover offsets are non-decreasing
    n_refractors = params.shape[1] // 2
    np.maximum.accumulate(params[:, n_refractors:], axis=1, out=params[:, n_refractors:])
    np.maximum.accumulate(params[:, 1:n_refractors], axis=1, out=params[:, 1:n_refractors])

    if is_1d:
        return params[0]
    return params

def dump_refractor_velocities(refractor_velocities, path, encoding="UTF-8"):
    """Dump parameters of passed near-surface velocity models to a file.

    The file should define a near-surface velocity model at a given location and have the following structure:
    - The first row contains names of the coordinates parameters ("name_x", "name_y", "x", "y") and names of
      the parameters ("t0", "x1"..."x{n-1}", "v1"..."v{n}") of near-surface velocity model.
    - Each next row contains the corresponding values of one near-surface velocity model in the field.

    File example:
     name_x     name_y          x          y        t0        x1        v1        v2
    SourceX    SourceY    1111100    2222220     50.25   1000.10   1500.25   2000.10
    ...
    SourceX    SourceY    1111100    2222220     50.50   1000.20   1500.50   2000.20

    Parameters
    ----------
    refractor_velocities : RefractorVelocity or iterable of RefractorVelocities
        The near-surface velocity models to dump to the file.
    path : str
        Path to the created file.
    encoding : str, optional, defaults to "UTF-8"
        File encoding.
    """
    rv_list = to_list(refractor_velocities)
    columns = ['name_x', 'name_y', 'x', 'y'] + list(rv_list[0].params.keys())
    data = np.empty((len(rv_list), len(columns)), dtype=object)
    for i, rv in enumerate(rv_list):
        data[i] = [*rv.coords.names] + [*rv.coords.coords] + list(rv.params.values())
    df = pd.DataFrame(data, columns=columns).convert_dtypes()
    df.to_string(buf=path, float_format=lambda x: f"{x:.2f}", index=False, encoding=encoding)

def load_refractor_velocities(path, encoding="UTF-8"):
    """Load parameters of the near-surface velocity models from a file.

    Notes
    -----
    See more about the format in :func:`~dump_refractor_velocities`.

    Parameters
    ----------
    path : str
        Path to a file.
    encoding : str, optional, defaults to "UTF-8"
        File encoding.

    Returns
    -------
    rv_list : list of RefractorVelocity
        List of the near-surface velocity models that are created from the parameters loaded from the file.
    """
    #pylint: disable-next=import-outside-toplevel
    from .refractor_velocity import RefractorVelocity  # import inside to avoid the circular import
    df = pd.read_csv(path, sep=r'\s+', encoding=encoding).convert_dtypes()
    params_names = df.columns[4:]
    return [RefractorVelocity(**dict(zip(params_names, row[4:])), coords=Coordinates(row[2:4], row[:2]))
            for row in df.itertuples(index=False)]
