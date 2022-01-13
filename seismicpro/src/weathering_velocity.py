from collections import OrderedDict

import matplotlib.transforms as mtransforms
import numpy as np
from sklearn.linear_model import LinearRegression, SGDRegressor, HuberRegressor
from scipy import optimize

from .decorators import plotter
from .utils import to_list
from .utils.interpolation import interp1d


class WeatheringVelocity:

    def __init__(self, offsets, picking_times, n_layers=None, init=None, bounds=None, **kwargs):
        '''
        bounds passed as dict with next structure:
        {'t0': [0, 1000],
         'c1': [1000, 2000],
         'c2': [1500, 2500],
         'v1': [1, 3],
         'v2': [1, 4],
         'v3': [2, 5]}
        init passed as dict with next structure:
        {'t0': 200,
         'c1': 1000,
         'c2': 2000,
         'v1': 1,
         'v2': 2,
         'v3': 3}
        '''

        if n_layers is None and init is None and bounds is None:
            raise ValueError('One of the `n_layers`, `init`, `bounds` should be passed')

        self.count = 0

        self.offsets = offsets
        self.offsets_max = offsets.max()
        self.picking_times = picking_times

        init = {} if init is None else init
        bounds = {} if bounds is None else bounds

        self.init = {**self._calc_params_by_layers_3(n_layers), **self._calc_init(bounds), **init}
        self.bounds = {**self._calc_bounds(self.init), **bounds}
        self.n_layers = len(self.bounds) // 2

        if set(self._create_keys()) != set(self.bounds.keys()):
            raise ValueError(f"Insufficient parameters to fit a weathering velocity curve. ",
                             f"Add {set(self._create_keys()) - set(self.bounds.keys())} keys or use `n_layers` parameter")
        # fitting
        # self._interpolator = self._create_interpolator(*self._parse_params(self.init))
        fitted, _ = optimize.curve_fit(self.piecewise_linear, offsets, picking_times, p0=self._parse_params(self.init),
                                       bounds=self._parse_params(self.bounds), method='trf', loss='soft_l1', **kwargs)
        self._fitted_args = dict(zip(self._create_keys(), fitted))

    def __call__(self, offsets):
        ''' return a predicted times using the fitted crossovers and velocities. '''
        return self.piecewise_linear(offsets, *self._parse_params(self._fitted_args))

    def __getattr__(self, key):
        return self._fitted_args[key]

    def _create_keys(self, n_layers=None):
        n_layers = self.n_layers if n_layers is None else n_layers
        return ['t0'] + [f'c{i+1}' for i in range(n_layers - 1)] + [f'v{i+1}' for i in range(n_layers)]

    def _calc_bounds(self, init):
        ''' calc bounds based on init or calc init based on bounds '''
        # checking inital values
        for key, value in init.items():
            if value < 0:
                raise ValueError(f"Used parameters for a bounds calculation is non positive. " \
                                    f"Parameter {key} is {float(init[key]):.2f}")
        # t0 bounds could be too narrow
        return {key: [val / 2, val * 2] for key, val in init.items()}

    def _calc_init(self, bounds):
        return {key: val1 + (val2 - val1) / 3 for key, (val1, val2) in bounds.items()}

    def _parse_params(self, parsing_dict):
        return np.stack([parsing_dict[key] for key in self._create_keys()], axis=-1)

    def piecewise_linear(self, offsets, *args):
        '''
        args = [t0, *crossovers, *velocities]
        '''
        times = np.empty(self.n_layers + 1)
        times[0] = args[0]

        cross_offsets = np.zeros(self.n_layers + 1)
        cross_offsets[1:self.n_layers] = args[1:self.n_layers]
        cross_offsets[-1] = self.offsets_max

        for i in range(self.n_layers):
            times[i+1] = (cross_offsets[i+1] - cross_offsets[i]) / args[self.n_layers + i] + times[i]
        self.count += 1
        return np.interp(offsets, cross_offsets, times)

        # times[1:] = np.diff(cross_offsets) / args[self.n_layers:]
        # return np.interp(offsets, cross_offsets, np.cumsum(times))

    # def _create_interpolator(self, *args):
    #     # print(args)
    #     times = np.empty(self.n_layers + 1)
    #     times[0] = args[0]

    #     cross_offsets = np.zeros(self.n_layers + 1)
    #     cross_offsets[1:self.n_layers] = args[1:self.n_layers]
    #     cross_offsets[-1] = self.offsets_max
    #     return interp1d(cross_offsets, times)

    # def piecewise_linear(self, offsets, *args):
    #     '''
    #     args = [t0, *crossovers, *velocities]
    #     '''
    #     #interpolator = interp1d() if self._interpolator is None else self._interpolator
        
    #     self._interpolator.y[0] = args[0]
    #     for i in range(self.n_layers):
    #         self._interpolator.y[i+1] = (self._interpolator.x[i+1] - self._interpolator.x[i]) / args[self.n_layers+i] \
    #                                     + self._interpolator.y[i]
    #     self.count += 1
    #     return self._interpolator(offsets)

    def _calc_params_by_layers_1(self, n_layers):
        ''' '''
        if n_layers is None:
            return {}
        lin_reg = SGDRegressor(loss='huber', early_stopping=True, penalty=None, shuffle=True, epsilon=0.01, 
                               eta0=.003, alpha=0)
        lin_reg.fit(self.offsets.reshape(-1, 1), self.picking_times,
                    coef_init=0.5, # (max(self.picking_times) - min(self.picking_times)) / self.offsets_max
                    intercept_init=min(self.picking_times))
        t0_base = lin_reg.intercept_
        slope_base = lin_reg.coef_[0]

        offsets = np.linspace(0, self.offsets.max(), num=n_layers+1)
        velocities = np.linspace(0, 2 / lin_reg.coef_[0], num=n_layers+2)[1:-1]
        slopes = 1 / velocities

        sq_base = t0_base * self.offsets_max + .5 * slope_base * (self.offsets_max ** 2)
        sq_cap = np.sum(.5 * slopes * (np.diff(offsets) ** 2) + (np.diff(offsets) ** 2) * np.array([0, *np.cumsum(slopes)])[:-1])
        
        init = np.empty(shape=2 * n_layers)
        init[0] = max(50, (sq_base - sq_cap) / self.offsets_max)
        init[1:n_layers] = offsets[1:-1]
        init[n_layers:] = velocities

        init = dict(zip(self._create_keys(n_layers), init))
        return init

    def _calc_params_by_layers_2(self, n_layers):
        ''' '''
        if n_layers is None:
            return {}
        lin_reg = SGDRegressor(loss='huber', early_stopping=True, penalty=None, shuffle=True, epsilon=0.01, 
                               eta0=.003, alpha=0)
        lin_reg.fit(self.offsets.reshape(-1, 1), self.picking_times,
                    coef_init=0.5,
                    intercept_init=min(self.picking_times))

        t0 = lin_reg.intercept_ / 2
        slope = lin_reg.coef_[0]
        # there are some math to calc better init. just believe.
        indices = np.arange(n_layers) + 1
        k = 6 * t0 * n_layers / (slope * self.offsets_max * (1 - n_layers**2))
        alphas = k * (indices - (n_layers + 1) / 2) + 1
        slopes = slope * alphas

        offsets = np.linspace(0, self.offsets_max, num=n_layers+1)
        velocities = 1 / slopes

        init = np.empty(shape=2 * n_layers)
        init[0] = t0
        init[1:n_layers] = offsets[1:-1]
        init[n_layers:] = velocities

        init = dict(zip(self._create_keys(n_layers), init))
        return init

    def _calc_params_by_layers_3(self, n_layers):
        ''' n regressions '''
        if n_layers is None:
            return {}
        
        cross_offsets = np.linspace(0, self.offsets_max, num=n_layers+1)
        times = np.empty(n_layers)
        slopes = np.empty(n_layers)
        start_params = [0.5, min(self.picking_times)]
        cross_offsets_ident = 0.2 * self.offsets_max / n_layers
        for i in range(n_layers):
            idx = np.argwhere((self.offsets >= cross_offsets[i] + cross_offsets_ident) & 
                              (self.offsets < cross_offsets[i+1] - cross_offsets_ident))[:, 0]
            slopes[i], times[i] = self._fit_regressor(np.take(self.offsets, idx).reshape(-1, 1), 
                                                      np.take(self.picking_times, idx), start_params)
            start_params[0] = slopes[i] * (n_layers / (n_layers + 1))
            start_params[1] = times[i] + (slopes[i] - start_params[0]) * self.offsets_max * (i + 1) / n_layers

        velocities = 1 / slopes
        
        init = np.empty(shape=2 * n_layers)
        init[0] = times[0]
        init[1:n_layers] = cross_offsets[1:-1]
        init[n_layers:] = velocities

        init = dict(zip(self._create_keys(n_layers), init))
        return init

    def _fit_regressor(self, x, y, start_params):
        lin_reg = SGDRegressor(loss='huber', early_stopping=True, penalty=None, shuffle=True, epsilon=0.01, 
                               eta0=.003, alpha=0)
        lin_reg.fit(x, y, coef_init=start_params[0], intercept_init=start_params[1])
        return lin_reg.coef_[0], lin_reg.intercept_

    @plotter(figsize=(10, 5))
    def plot(self, ax, title=None, show_params=False, **kwargs):
        # TODO: add thresholds lines
        ax.scatter(self.offsets, self.picking_times)
        ax.scatter(self.offsets, self(self.offsets), s=5)

        if show_params:
            crossover_title = 'crossovers offsets = '
            if self.n_layers > 1:
                crossovers = [f"{getattr(self, f'c{i + 1}'):.2f}" for i in range(self.n_layers - 1)]
                crossover_title += ', '.join(crossovers)
            else:
                crossover_title += 'None'
            velocity_title = 'velocities = '
            velocities = [f"{getattr(self, f'v{i + 1}'):.2f}" for i in range(self.n_layers)]
            velocity_title += ', '.join(velocities)

            ax.text(0.03, .94, f"t0={self.t0:.2f}\n{crossover_title}\n{velocity_title}", fontsize=15, va='top',
                    transform=ax.transAxes)
        return self
