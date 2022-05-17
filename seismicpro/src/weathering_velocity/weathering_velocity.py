"""Implements WeatheringVelocity class to estimate nearsurface refractors velocities."""

from functools import partial

import numpy as np
from sklearn.linear_model import SGDRegressor
from scipy import optimize

from ..decorators import plotter
from ..utils import set_ticks, set_text_formatting
from ..utils.interpolation import interp1d


# pylint: disable=too-many-instance-attributes, protected-access
class WeatheringVelocity:
    """The class fits and stores parameters of a weathering model based on gather's offsets and first break picking
    times.

    The weathering model is a velocity model of the first few subsurface layers. The class uses the intercept time,
    cross offsets and velocities values as the weathering model parameters. The weathering model could be present as
    a piecewise linear function of the picking time from the offsets. Also referred to as the weathering velocity
    curve.
    Since the class uses the first break picking time to fit the detected velocities, any detected underlaying layers
    should have a higher velocity than any of the overlaying.

    The class could be created with `from_params` and `from_picking` classmethods.
    If you have the weathering model parameters.
    `from_params` - create a WeatheringVelocity instance and store parameters of the weathering model.
        Parameters : `params`.
            `params` : dict with weathering model parameters. Read the valid keys notation below.

    If you have the estimate weathering model parameters.
    `from_picking` - create a WeatheringVelocity instance, fit the parameters of the weathering model by the offsets,
                     first break picking data and estimate parameters of the weathering model.
        Data : first break picking times and the corresponding offsets.
        Parameters : `init`, `bounds`, `n_layers`.
            `init` : dict with the estimate weathering model parameters. Read the valid keys notation below.
            `bounds` : dict with left and right bound for each weathering model parameter.
                    Read the valid keys notation below.
            `n_layers` : the quantity of the weathering model layers.

    The WeatheringVelocity created with `from_picking` classmethod could calculate the missing parameters from
    the passed parameters.
    Rules for the calculating missing parameters:
        `init` : calculated from `bounds`. lower bound + (upper bounds - lower bounds) / 3
                 calculated from `n_layers`. Read `_calc_init_by_layers` docs to get more info.
        `bounds` : calculated from `init`. The lower bound is init / 2, the upper bound is init * 2.
                   calculated from `n_layers`. `init` calculates first and uses the above expression.
        `n_layers`: calculated from `bounds`. Half of the the `bounds` keys.
                    calculated from `init`. Calculates `bounds` from `init` first and uses the above rule.
    Note: All passed parameters have a greater priority than any calculated parameters.

    Valid keys notation.
        `init`, `bounds` and `params` is dicts with the valid key notation.
            `t0`: a intercept time of the first subweathering layer. Same as double wave travel time to the first
                  refractor. Measured in milliseconds.
            `x{i}`: cross offset. The offset where refracted wave from the i-th layer comes at the same time with
                    a refracted wave from the next underlaying layer. Measured in meters.
            `v{i}`: velocity of the i-th layer. Measured in kilometers/seconds.

    Examples
    --------
    Create the Survey and load the first break picking.
    >>> survey = Survey(survey_path, header_index="FieldRecord", header_cols="offset")
    >>> survey = survey.load_first_breaks(picking_path)
    Load a randomly selected common source gather.
    >>> gather = survey.sample_gather()

    Create the WeatheringVelocity instance with `from_picking` method for two layers weathering model.
    >>> offsets = gahter.offsets
    >>> picking = gather['FirstBreak'].ravel()
    >>> weathering_velocity = WeatheringVelocity.from_picking(offsets, picking, n_layers=2)
    Note: Gather's method `calculate_weathering_velocity` uses the `from_picking` classmethod.

    A WeatheringVelocity object with initial parameters for two layers weathering model:
    >>> initial_params = {'t0': 100, 'x1': 1500, 'v1': 2, 'v2': 3}
    >>> weathering_velocity = WeatheringVelocity.from_picking(offsets, picking, init=initial_params)

    A WeatheringVelocity object with bounds for fitted intercept time and velocity of the first layer for the one
    layer weathering model:
    >>> weathering_velocity = WeatheringVelocity.from_picking(offsets, picking, bounds={'t0': [0, 200], 'v1': [1, 3]})

    Also mixing parameters are possible (two layers weathering model):
    >>> weathering_velocity = WeatheringVelocity.from_picking(offsets, picking, init={'x1': 200, 'v1': 1},
                                                                                bounds={'t0': [0, 50]},
                                                                                n_layers=3)

    A Weathering Velocity object with known parameters (avoid the fitting procedure):
    >>> weathering_velocity = WeatheringVelocity.from_params(params={'t0': 100, 'x1': 1500, 'v1': 2, 'v2': 3})

    Attributes
    ----------
    offsets : 1d ndarray
        Offsets of traces. Measured in meters.
    picking_times : 1d ndarray
        Picking times of traces. Measured in milliseconds.
    init : dict
        The inital values used to fit the parameters of the weathering model. Includes the calculated non-passed
        keys and values. Have the common key notation.
    bounds : dict
        The left and right bounds used to fit the parameters of the weathering model. Includes the calculated
        non-passed keys and values. Have the common key notation.
    n_layers : int
        Number of the weathering model layers used to fit the parameters of the weathering model.
    params : dict
        The fitted parameters of a weathering model. Have the common key notation.
    """
    def __init__(self):
        self.offsets = None
        self.picking_times = None
        self.max_offset = None
        self.init = None
        self.bounds = None
        self.n_layers = None
        self.negative_t0 = None
        self.params = None
        self.interpolator = lambda offsets: np.zeros_like(offsets, dtype=np.float32)

        self._valid_keys = None
        self._empty_layers = None
        self._piecewise_offsets = None
        self._piecewise_times = None
        self._model_params = None

    @classmethod   # fit_intercept is debug parameter
    def from_picking(cls, offsets, picking_times, init=None, bounds=None, n_layers=None, acsending_velocities=True,
                     freeze_t0=False, negative_t0=False, fit_intercept=False, **kwargs):
        """Method fits the weathering model parameters from the offsets and the first break picking times.

        Parameters
        ----------
        offsets : 1d ndarray
            Offsets of the traces. Measured in meters.
        picking_times : 1d ndarray
            First break picking times. Measured in milliseconds.
        init : dict, defaults to None
            Initial parameters of a weathering model.
        bounds : dict, defaults to None
            Lower and upper bounds of the weathering model parameters.
        n_layers : int, defaults to None
            Number of layers of a weathering model.
        acsending_velocities : bool, defaults to True
            Keep the ascend of the fitted velocities from i-th layer to i+1 layer.
        freeze_t0 : bool, defaults to False
            Avoid the fitting `t0`.
        kwargs : dict, optional
            Additional keyword arguments to `scipy.optimize.minimize`.

        Raises
        ------
        ValueError
            If any `init` values are negative.
            If any `bounds` values are negative.
            If left bound greater than right bound.
            If init value is out of the bound interval.
            If passed `init` and/or `bounds` keys are insufficient or excessive.
            If an union of `init` and `bounds` keys less than 2 or `n_layers` less than 1.
        """
        self = cls()
        init = {} if init is None else init
        bounds = {} if bounds is None else bounds
        self.negative_t0 = negative_t0
        self._validate_values(init, bounds)

        self.offsets = offsets
        self.picking_times = picking_times
        self.max_offset = offsets.max()

        self.init = {**self._calc_init_by_layers(n_layers, fit_intercept), **self._calc_init_by_bounds(bounds), **init}
        self.bounds = {**self._calc_bounds_by_init(), **bounds}
        self._validate_keys(self.bounds)
        self.n_layers = len(self.bounds) // 2
        self._valid_keys = self._get_valid_keys()

        # ordering `init` and `bounds` dicts to put all values in the required order.
        self.init = {key: self.init[key] for key in self._valid_keys}
        self.bounds = {key: self.bounds[key] for key in self._valid_keys}

        # piecewise func parameters
        self._piecewise_offsets, self._piecewise_times = self._create_piecewise_coords(self.n_layers, offsets.max())
        self._piecewise_offsets, self._piecewise_times = \
            self._update_piecewise_coords(self._piecewise_offsets, self._piecewise_times, list(self.init.values()),
                                          self.n_layers)
        self._empty_layers = np.histogram(self.offsets, self._piecewise_offsets)[0] ==  0

        constraints_list = self._get_constraints(acsending_velocities, freeze_t0)

        # fitting piecewise linear regression
        partial_loss_func = partial(self.loss_piecewise_linear, loss=kwargs.pop('loss', 'L1'),
                                    huber_coef=kwargs.pop('huber_coef', .1))
        minimizer_kwargs = {'method': 'SLSQP', 'constraints': constraints_list, **kwargs}
        self._model_params = optimize.minimize(partial_loss_func, x0=list(self.init.values()),
                                               bounds=list(self.bounds.values()), **minimizer_kwargs)
        self.params = dict(zip(self._valid_keys,
                               self._params_postprocceissing(self._model_params.x,
                                                             ascending_velocity=acsending_velocities)))
        self.interpolator = interp1d(self._piecewise_offsets, self._piecewise_times)
        return self

    @classmethod
    def from_params(cls, params):
        """Init WeatheringVelocity from parameters.

        Parameters should be dict with common key notation.

        Parameters
        ----------
        params : dict,
            Parameters of the weathering model. Have the common key notation.

        Returns
        -------
        WeatheringVelocity
            WeatheringVelocity instance based on passed params.

        Raises
        ------
        ValueError
            If passed `params` keys are insufficient or excessive.
        """
        self = cls()

        self._validate_keys(params)
        self.n_layers = len(params) // 2
        self._valid_keys = self._get_valid_keys(self.n_layers)
        self.params = {key: params[key] for key in self._valid_keys}

        self._piecewise_offsets, self._piecewise_times = \
            self._create_piecewise_coords(self.n_layers, list(self.params.values())[self.n_layers-1] + 1000)
        self._piecewise_offsets, self._piecewise_times = \
            self._update_piecewise_coords(self._piecewise_offsets, self._piecewise_times,
                                          list(self.params.values()), self.n_layers)
        self.interpolator = interp1d(self._piecewise_offsets, self._piecewise_times)
        return self

    def __call__(self, offsets):
        """Return predicted picking times using the offsets and fitted parameters of the weathering model."""
        return self.interpolator(offsets)

    def __getattr__(self, key):
        return self.params[key]

    def _create_piecewise_coords(self, n_layers, max_offset=np.nan):
        """Create two array corresponding to the piecewise linear function coords."""
        offsets = np.zeros(n_layers + 1)
        times = np.zeros(n_layers + 1)
        offsets[-1] = max_offset
        return offsets, times

    def _update_piecewise_coords(self, offsets, times, params, n_layers):
        """Update the given `offsets` and `times` arrays based on the `params` and `n_layers`."""
        times[0] = params[0]
        offsets[1:n_layers] = params[1:n_layers]
        for i in range(n_layers):
            times[i + 1] = ((offsets[i + 1] - offsets[i]) / params[n_layers + i]) + times[i]
        return offsets, times

    def loss_piecewise_linear(self, args, loss='L1', huber_coef=.1):
        """Update the piecewise linear attributes and returns the loss function result.

        Method calls `_update_piecewise_coords` to update piecewise linear attributes of a WeatheringVelocity instance.
        After that, the method calculates the loss function between the true picking times stored in
        the `self.picking_times` and predicted piecewise linear function. The points at which the loss function
        is calculated correspond to the offset.

        Piecewise linear function is defined by the given `args`. `args` should be list-like and have the following
        structure:
            args[0] : t0
            args[1:n_layers] : cross offsets points in meters.
            args[n_layers:] : velocities of each weathering model layer in km/s.
            Total lenght of args should be n_layers * 2.
        The list-like initial is due to the `scipy.optimize.minimize`.

        Parameters
        ----------
        args : tuple, list, or 1d ndarray
            Parameters of the piecewise linear function.
        loss : str, optional, defaults to "L1".
            The loss function type. Should be one of "MSE", "L1", "huber", "soft_L1", or "cauchy".
            All implemented loss functions have a mean reduction.
        huber_coef : float, default to 0.1
            Coefficient for Huber loss.

        Returns
        -------
        loss : float
            Loss function result between true picking times and a predicted piecewise linear function.

        Raises
        ------
        ValueError
            If given `loss` does not exist.
        """
        self._piecewise_offsets, self._piecewise_times = \
            self._update_piecewise_coords(self._piecewise_offsets, self._piecewise_times, args, self.n_layers)
        diff_abs = np.abs(np.interp(self.offsets, self._piecewise_offsets, self._piecewise_times) - self.picking_times)
        if loss == 'MSE':
            return (diff_abs ** 2).mean()
        if loss == 'L1':
            return diff_abs.mean()
        if loss == 'huber':
            loss = np.empty_like(diff_abs)
            mask = diff_abs <= huber_coef
            loss[mask] = .5 * (diff_abs[mask] ** 2)
            loss[~mask] = huber_coef * diff_abs[~mask] - .5 * (huber_coef ** 2)
            return loss.mean()
        if loss == 'soft_L1':
            return 2 * ((1 + diff_abs) ** .5 - 1).mean()
        if loss == 'cauchy':
            return np.log(diff_abs + 1).mean()
        raise ValueError('Unknown loss type for `loss_piecewise_linear`.')

    def _get_valid_keys(self, n_layers=None):
        """Returns a list with the valid keys based on `n_layers` or `self.n_layers`."""
        n_layers = self.n_layers if n_layers is None else n_layers
        return ['t0'] + [f'x{i+1}' for i in range(n_layers - 1)] + [f'v{i+1}' for i in range(n_layers)]

    def _get_constraints(self, acsending_velocities, freeze_t0):
        """Return list with constaints based on parameters."""
        constraint_offset = {  # cross offsets ascend
            "type": "ineq",
            "fun": lambda x: np.diff(np.concatenate((x[1:self.n_layers], [self.max_offset])))}
        constraint_velocity = {  # velocities ascend
            "type": "ineq",
            "fun": lambda x: np.diff(x[self.n_layers:])}
        constraint_freeze_velocity = {  # freeze the velocity if no data for layer is found.
            "type": "eq",
            "fun": lambda x: np.array(list(self.init.values()))[self.n_layers:][self._empty_layers]
                             - x[self.n_layers:][self._empty_layers]}
        constraint_freeze_t0 = {  # freeze the intercept time if no data for layer is found.
            "type": "eq",
            "fun": lambda x: np.array(list(self.init.values()))[:1][self._empty_layers[:1]]
                                      - x[:1][self._empty_layers[:1]]}
        constraint_freeze_init_t0 = {  # freeze `t0` at init value
            "type": "eq",
            "fun": lambda x: x[0] - self.init['t0']}
        constraints_list = [constraint_offset, constraint_freeze_velocity]
        constraints_list.append(constraint_freeze_init_t0 if freeze_t0 else constraint_freeze_t0)
        if acsending_velocities:
            constraints_list.append(constraint_velocity)
        return constraints_list

    def _fit_regressor(self, x, y, start_slope, start_time, fit_intercept):
        """Method fits the linear regression by given data and initinal values.

        Parameters
        ----------
        x : 1d ndarray of shape (n_samples, 1)
            Training data.
        y : 1d ndarray of shape (n_samples,)
            Target values.
        start_slope : float
            Starting coefficient to fit a linear regression.
        start_time : float
            Starting intercept to fit a linear regression.
        fit_intercept : bool
            Fit the intercept with `True` or hold it with `False`.

        Returns
        -------
        params : tuple
            Linear regression `coef` and `intercept`.
        """
        lin_reg = SGDRegressor(loss='huber', early_stopping=True, penalty=None, shuffle=True, epsilon=0.1, eta0=.1,
                               alpha=0., tol=1e-6, fit_intercept=fit_intercept)
        lin_reg.fit(x, y, coef_init=start_slope, intercept_init=start_time)
        return lin_reg.coef_[0], lin_reg.intercept_

    def _calc_init_by_layers(self, n_layers, fit_intercept):
        """Calculates `init` dict by a given an estimated quantity of layers.

        Method splits the picking times into `n_layers` equal part by cross offsets and fits a separate linear
        regression on each part. Fitting the coefficient and intercept for the first part and fitting the coefficient
        only for any next part. These linear functions are compiled together as a piecewise linear function.
        Parameters of this piecewise function are recalculated to the weathering model parameters and returned as
        `init` dict.

        Parameters
        ----------
        n_layers : int
            Number of layers.

        Returns
        -------
        init : dict
            Estimated initial to fit the piecewise linear function.
        """
        if n_layers is None or n_layers < 1:
            return {}

        max_picking_times = self.picking_times.max()  # normalization parameter.
        initial_slope = 2 / 3  # base slope corresponding velocity is 1,5 km/s (v = 1 / slope)
        initial_slope = initial_slope * self.max_offset / max_picking_times  # add normalization
        initial_time = 0
        normalized_offsets = self.offsets / self.max_offset
        normalized_times = self.picking_times / max_picking_times

        # split cross offsets on an equal intervals
        cross_offsets = np.linspace(0, 1, num=n_layers+1)
        current_slope = np.empty(n_layers)
        current_time = np.empty(n_layers)

        for i in range(n_layers):  # inside the loop work with normalized data only
            mask = (normalized_offsets > cross_offsets[i]) & (normalized_offsets <= cross_offsets[i + 1])
            _fit_intercept = True if fit_intercept else i==0  # debug parameter
            if mask.sum() > 1:  # at least two point to fit
                current_slope[i], current_time[i] = \
                    self._fit_regressor(normalized_offsets[mask].reshape(-1, 1) - cross_offsets[i],
                                        normalized_times[mask], initial_slope, initial_time,
                                        fit_intercept=_fit_intercept)
            else:
                current_slope[i] = initial_slope
                current_time[i] = initial_time
            # raise base velocity for next layers (v = 1 / slope)
            current_slope[i] = max(.167, current_slope[i])  # move maximal velocity to 6 km/s
            current_time[i] =  current_time[i] if self.negative_t0 else max(0, current_time[i])
            # raise base velocity for next layers (v = 1 / slope)
            initial_slope = current_slope[i] * (n_layers / (n_layers + 1))
            initial_time = current_time[i] + (cross_offsets[i + 1] - cross_offsets[i]) * current_slope[i]
            # initial_time = current_time[i] + (current_slope[i] - initial_slope) * cross_offsets[i + 1]
        velocities = 1 / (current_slope * max_picking_times / self.max_offset)
        init = [current_time[0] * max_picking_times, *cross_offsets[1:-1] * self.max_offset, *velocities]
        init = dict(zip(self._get_valid_keys(n_layers), init))
        return init

    def _calc_init_by_bounds(self, bounds):
        """Method returns dict with a calculated init from a bounds dict."""
        return {key: val1 + (val2 - val1) / 3 for key, (val1, val2) in bounds.items()}

    def _calc_bounds_by_init(self):
        """Method returns dict with calculated bounds from a init dict."""
        bounds = {key: [val / 2, val * 2] for key, val in self.init.items()}
        if 't0' in self.init:
            lower_bound = -200 if self.negative_t0 else 0
            bounds['t0'] = [min(lower_bound, bounds['t0'][0]), max(200, bounds['t0'][1])]
        return bounds

    def _validate_values(self, init, bounds):
        """Check the values of an `init` and `bounds` dicts."""
        negative_init = {key: val for key, val in init.items() if val < 0}
        if negative_init:
            raise ValueError(f"Init parameters contain negative values {negative_init}.")
        negative_bounds = {key: val for key, val in bounds.items() if min(val) < 0}
        if self.negative_t0:
            negative_bounds.pop('t0', None)
        if negative_bounds:
            raise ValueError(f"Bounds parameters contain negative values {negative_bounds}.")
        reversed_bounds = {key: [left, right] for key, [left, right] in bounds.items() if left > right}
        if reversed_bounds:
            raise ValueError(f"Left bound is greater than right bound for {reversed_bounds}.")
        both_keys = {*init.keys()} & {*bounds.keys()}
        outbounds_keys = {key for key in both_keys if init[key] < bounds[key][0] or init[key] > bounds[key][1]}
        if outbounds_keys:
            raise ValueError(f"Init parameters are out of the bounds for {outbounds_keys} key(s).")

    def _validate_keys(self, checked_dict):
        """Check the keys of given dict for a minimum quantity, an excessive, and an insufficient."""
        expected_layers = len(checked_dict) // 2
        if expected_layers < 1:
            raise ValueError("Insufficient parameters to fit a weathering velocity curve.")
        missing_keys = set(self._get_valid_keys(expected_layers)) - set(checked_dict.keys())
        if missing_keys:
            raise ValueError("Insufficient parameters to fit a weathering velocity curve. ",
                            f"Check {missing_keys} key(s) or define `n_layers`")
        excessive_keys = set(checked_dict.keys()) - set(self._get_valid_keys(expected_layers))
        if excessive_keys:
            raise ValueError(f"Excessive parameters to fit a weathering velocity curve. Remove {excessive_keys}.")

    def _params_postprocceissing(self, params, ascending_velocity):
        """Checks the parameters and fix it if constraints are not met."""
        mask_offsets = self._piecewise_offsets[2:] - params[1:self.n_layers] < 0
        params[1:self.n_layers][mask_offsets] = self._piecewise_offsets[2:][mask_offsets]  # move cross offsets
        if ascending_velocity:  # move velocities
            for i in range(self.n_layers, 2 * self.n_layers - 1):
                params[i + 1] = max(params[i], params[i + 1])
        return params

    def _calc_coords_from_params(self, params, max_offset=np.nan):
        """Method calculate coords for the piecewise linear curve from params dict.

        Parameters
        ----------
        params : dict,
            Dict with parameters of weathering model. Dict have the common key notation.

        Returns
        -------
        offsets : 1d npdarray
            Coords of the points on the x axis.
        times : 1d ndarray
            Coords of the points on the y axis.
        """
        expected_layers = len(params) // 2
        keys = self._get_valid_keys(n_layers=expected_layers)
        params = {key: params[key] for key in keys}
        params_values = list(params.values())

        offsets, times = self._create_piecewise_coords(expected_layers, max_offset)
        offsets, times = self._update_piecewise_coords(offsets, times, params_values, expected_layers)
        return offsets, times

    @plotter(figsize=(10, 5))
    def plot(self, *, ax=None, title=None, x_ticker=None, y_ticker=None, show_params=True, threshold_times=None,
            compared_params=None, txt_kwargs=None, **kwargs):
        """Plot the WeatheringVelocity data, fitted curve, cross offsets, and additional information.

        Parameters
        ----------
        title : str, optional, defaults to None
            Plot title.
        x_ticker : dict, optional, defaults to None
            Parameters for ticks and ticklabels formatting for the x-axis; see `.utils.set_ticks` for more details.
        y_ticker : dict, optional, defaults to None
            Parameters for ticks and ticklabels formatting for the y-axis; see `.utils.set_ticks` for more details.
        show_params : bool, optional, defaults to True
            Shows the weathering model parameters on a plot.
        threshold_times : int or float, optional. Defaults to None
            Neighborhood margins of the fitted curve to fill in the area inside. If None the area don't show.
        compared_params : dict, optional, defaults to None
            Dict with the valid weathering velocity params. Used to plot an additional the weathering velocity curve.
        kwargs : dict, optional
            Additional keyword arguments to the plotter.

        Returns
        -------
        self : WeatheringVelocity
            WeatheringVelocity without changes.
        """

        (title, x_ticker, y_ticker, txt_kwargs), kwargs = set_text_formatting(title, x_ticker, y_ticker, txt_kwargs,
                                                                              **kwargs)
        set_ticks(ax, "x", tick_labels=None, label="offset, m", **x_ticker)
        set_ticks(ax, "y", tick_labels=None, label="time, ms", **y_ticker)

        if self.picking_times is not None:
            ax.scatter(self.offsets, self.picking_times, s=1, color='black', label='First Breaks')
        ax.plot(self._piecewise_offsets, self._piecewise_times, '-', color=kwargs.get('curve_color', 'red'),
                label='Weathering velocity curve')
        for i in range(self.n_layers - 1):
            ax.axvline(self._piecewise_offsets[i+1], 0, ls='--', color=kwargs.get('crossover_color', 'blue'),
                       label='Crossover point' if self.n_layers <= 2 else 'Crossover points' if i == 0 else None)
        if show_params:
            params = [self.params[key] for key in self._valid_keys]
            txt_info = f"t0 : {params[0]:.2f} ms"
            if self.n_layers > 1:
                txt_info += '\ncrossover offsets : ' + ', '.join(f"{round(x)}" for x in params[1:self.n_layers]) + ' m'
            txt_info += '\nvelocities : ' + ', '.join(f"{v:.2f}" for v in params[self.n_layers:]) + ' km/s'
            txt_kwargs = {'fontsize': 15, 'va': 'top', **txt_kwargs}
            txt_ident = txt_kwargs.pop('ident', (.03, .94))
            ax.text(*txt_ident, txt_info, transform=ax.transAxes, **txt_kwargs)

        if threshold_times is not None:
            ax.fill_between(self._piecewise_offsets, self._piecewise_times - threshold_times,
                            self._piecewise_times + threshold_times, color='red',
                            label=f'+/- {threshold_times}ms threshold area', alpha=.2)
        if compared_params is not None:
            compared_wv = WeatheringVelocity.from_params(compared_params)
            compared_wv.plot(ax=ax, show_params=False, curve_color='#ff7900', crossover_color='green')
        ax.set_xlim(0)
        ax.set_ylim(0)
        ax.legend(loc='lower right')
        ax.set_title(**{"label": None, **title})
        return self
