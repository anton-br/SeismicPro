"""Building blocks for interactive plots"""

from time import time

import matplotlib.pyplot as plt

from .general_utils import get_first_defined, align_args, MissingModule

# Safe import of modules for interactive plotting
try:
    from ipywidgets import widgets
except ImportError:
    widgets = MissingModule("ipywidgets")

try:
    from IPython.display import display
except ImportError:
    display = MissingModule("IPython.display")


# Maximum time between mouse button click and release events to consider them as a single click
MAX_CLICK_TIME = 0.2


# Default text widgets layout
TEXT_LAYOUT = {
    "height": "28px",
    "display": "flex",
    "width": "100%",
    "justify_content": "center",
    "align_items": "center",
}


# Default button widgets layout
BUTTON_LAYOUT = {
    "height": "28px",
    "width": "35px",
    "min_width": "35px",
}


# HTML style of plot titles
TITLE_STYLE = "<style>p{word-wrap:normal; text-align:center; font-size:14px}</style>"
TITLE_TEMPLATE = "{style} <b><p>{title}</p></b>"


class InteractivePlot:  # pylint: disable=too-many-instance-attributes
    """Construct an interactive plot with optional click handling.

    The plot may contain multiple views: one for each of the passed `plot_fn`. If more than one view is defined, an
    extra button is created in the header or toolbar to iterate over them. The plot is interactive: it can handle click
    and slice events, while each view may define its own processing logic.

    Plotting must be performed in a JupyterLab environment with the the `%matplotlib widget` magic executed and
    `ipympl` and `ipywidgets` libraries installed.

    Parameters
    ----------
    plot_fn : callable or list of callable, optional
        One or more plotters each accepting a single keyword argument `ax`. If more than one plotter is given, an extra
        button for view switching is displayed. If not given, an empty plot is created.
    click_fn : callable or list of callable, optional
        Click handlers for views defined by `plot_fn`. Each of them must accept a tuple with 2 elements defining
        click coordinates. If a single `click_fn` is given, it is used for all views. If not given, click events are
        not handled.
    slice_fn : callable or list of callable, optional
        Slice handlers for views defined by `plot_fn`. Slice is triggered by moving the mouse with the left button
        held. Each handlers must accept two tuples with 2 elements defining coordinates of slice edges. If a single
        `slice_fn` is given, it is used for all views. If not given, slice events are not handled.
    unclick_fn : callable or list of callable, optional
        Handlers that undo clicks and slices on views defined by `plot_fn`. Each of them is called without arguments.
        If a single `unclick_fn` is given, it is used for all views. If not given, clicks and slices can not be undone.
    marker_params : dict or list of dict, optional, defaults to {"marker": "+", "color": "black"}
        Click marker parameters for views defined by `plot_fn`. Passed directly to `Axes.scatter`. If a single `dict`
        is given, it is used for all views.
    title : str or callable or list of str or callable, optional
        Plot titles for views defined by `plot_fn`. If `callable`, it is called each time the title is being set (e.g.
        on `redraw`) allowing for dynamic title generation. If not given, an empty title is created.
    preserve_clicks_on_view_change : bool, optional, defaults to False
        Whether to preserve click/slice markers and trigger the corresponding event on view change.
    toolbar_position : {"top", "bottom", "left", "right"}, optional, defaults to "left"
        Matplotlib toolbar position relative to the main axes.
    figsize : tuple with 2 elements, optional, defaults to (4.5, 4.5)
        Size of the created figure. Measured in inches.

    Attributes
    ----------
    fig : matplotlib.figure.Figure
        The created figure.
    ax : matplotlib.axes.Axes
        Axes of the figure to plot views on.
    box : ipywidgets.widgets.widget_box.Box
        Main container that stores figure canvas, plot title, created buttons and, optionally, a toolbar.
    n_views : int
        The number of plot views.
    current_view : int
        An index of the current plot view.
    """
    def __init__(self, *, plot_fn=None, click_fn=None, slice_fn=None, unclick_fn=None, marker_params=None, title="",
                 preserve_clicks_on_view_change=False, toolbar_position="left", figsize=(4.5, 4.5)):
        list_args = align_args(plot_fn, click_fn, slice_fn, unclick_fn, marker_params, title)
        self.plot_fn_list, self.click_fn_list, self.slice_fn_list = list_args[:3]
        self.unclick_fn_list, marker_params_list, self.title_list = list_args[3:]
        self.marker_params_list = []
        for params in marker_params_list:
            if params is None:
                params = {}
            params = {"marker": "+", "color": "black", **params}
            self.marker_params_list.append(params)

        # View-related attributes
        self.n_views = len(self.plot_fn_list)
        self.current_view = 0
        self.preserve_clicks_on_view_change = preserve_clicks_on_view_change

        # Click-related attributes
        self.start_click_time = None
        self.start_click_coords = None
        self.click_coords = None
        self.slice_coords = None
        self.click_marker = None
        self.slice_marker = None

        # Construct a figure
        self.toolbar_position = toolbar_position
        if toolbar_position is None:
            toolbar_position = "left"
        with plt.ioff():
            # Add tight_layout to always correctly show colorbar ticks
            self.fig, self.ax = plt.subplots(figsize=figsize, tight_layout=True)  # pylint: disable=invalid-name
        self.fig.canvas.header_visible = False
        self.fig.canvas.toolbar_visible = False
        self.fig.canvas.toolbar_position = toolbar_position

        # Setup event handlers
        self.fig.interactive_plotter = self  # Always keep reference to self for all plots to remain interactive
        self.fig.canvas.mpl_connect("resize_event", self.on_resize)
        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.fig.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.fig.canvas.mpl_connect("button_release_event", self.on_release)
        self.fig.canvas.mpl_connect("key_press_event", self.on_press)

        # Build plot box
        self.title_widget = widgets.HTML(value="", layout=widgets.Layout(**TEXT_LAYOUT))
        self.view_button = widgets.Button(icon="exchange", tooltip="Switch to the next view",
                                          layout=widgets.Layout(**BUTTON_LAYOUT))
        self.view_button.on_click(self.on_view_toggle)
        self.header = self.construct_header()
        self.toolbar = self.construct_toolbar()
        self.box = self.construct_box()

    def __del__(self):
        """Close the figure on plot deletion."""
        del self.fig.interactive_plotter
        plt.close(self.fig)

    @property
    def plot_fn(self):
        """callable: plotter of the current view."""
        return self.plot_fn_list[self.current_view]

    @property
    def click_fn(self):
        """callable: click handler of the current view."""
        return self.click_fn_list[self.current_view]

    @property
    def is_clickable(self):
        """bool: whether the current view is clickable."""
        return self.click_fn is not None

    @property
    def slice_fn(self):
        """callable: slice handler of the current view."""
        return self.slice_fn_list[self.current_view]

    @property
    def is_sliceable(self):
        """bool: whether the current view is sliceable."""
        return self.slice_fn is not None

    @property
    def unclick_fn(self):
        """callable: undo click or slice on the current view."""
        return self.unclick_fn_list[self.current_view]

    @property
    def is_unclickable(self):
        """bool: whether the click/slice can be undone for the current view."""
        return self.unclick_fn is not None

    @property
    def marker_params(self):
        """dict: click marker parameters of the current view."""
        return self.marker_params_list[self.current_view]

    @property
    def title(self):
        """str: title of the current view. Evaluates callable titles."""
        title = self.title_list[self.current_view]
        if callable(title):
            return title()
        return title

    @property
    def toolbar_action_selected(self):
        """bool: Whether "Pan" or "Zoom" mode is selected in the figure toolbar."""
        return self.fig.canvas.toolbar.get_state()["_current_action"] != ""

    # Box construction

    def construct_buttons(self):
        """Return a list of extra buttons to add to a header or a toolbar. Can be overridden in child classes."""
        if self.n_views == 1:
            return []
        return [self.view_button]

    def construct_header(self):
        """Construct a header of the plot. Always contains view title, contains constructed buttons only if the
        toolbar is not visible."""
        buttons = self.construct_buttons()
        if self.toolbar_position is not None:
            buttons = []
        return widgets.HBox([*buttons, self.title_widget])

    def construct_toolbar(self):
        """Construct a plot toolbar which contains the toolbar of the canvas and constructed buttons."""
        toolbar = self.fig.canvas.toolbar
        if self.toolbar_position in {"top", "bottom"}:
            toolbar.orientation = "horizontal"
            return widgets.HBox([*self.construct_buttons(), toolbar])
        return widgets.VBox([*self.construct_buttons(), toolbar])

    def construct_box(self):
        """Construct the box of the whole plot which contains figure canvas, header and, optionally, a toolbar."""
        titled_box = widgets.HBox([widgets.VBox([self.header, self.fig.canvas])])
        if self.toolbar_position == "top":
            return widgets.VBox([self.toolbar, titled_box])
        if self.toolbar_position == "bottom":
            return widgets.VBox([titled_box, self.toolbar])
        if self.toolbar_position == "left":
            return widgets.HBox([self.toolbar, titled_box])
        if self.toolbar_position == "right":
            return widgets.HBox([titled_box, self.toolbar])
        return titled_box

    # Event handlers

    def on_resize(self, event):
        """Resize the plot on the `fig` canvas size change."""
        self.resize(event.width)

    def on_click(self, event):
        """Remember the mouse button click time to further distinguish between mouse click and hold events."""
        if event.inaxes != self.ax:
            return  # Discard clicks outside the main axes
        if event.button == 1:
            self.start_click_time = time()
            self.start_click_coords = (event.xdata, event.ydata)

    def on_motion(self, event):
        """Handle mouse movement with the pressed left mouse button. Redraw currently selected slice line."""
        if self.is_sliceable and event.button == 1:
            self._plot_slice(self.start_click_coords, (event.xdata, event.ydata))

    def on_release(self, event):
        """Handle clicks and slices of the plot."""
        if time() - self.start_click_time < MAX_CLICK_TIME:  # Single click
            if (event.inaxes == self.ax) and (event.button == 1):
                self.click((event.xdata, event.ydata))
            elif self.click_coords is not None:  # Restore previous valid click
                self.click(self.click_coords)
        elif not self.toolbar_action_selected:  # Process slice only if "Zoom" or "Pad" modes are not selected
            if (event.inaxes == self.ax) and (event.button == 1):
                self.slice(self.start_click_coords, (event.xdata, event.ydata))
            elif self.slice_coords is not None:  # Restore previous valid slice
                self.slice(*self.slice_coords)
        self.start_click_time = None
        self.start_click_coords = None

    def on_press(self, event):
        """Undo mouse click or slice on ESC key press if allowed."""
        if (event.inaxes == self.ax) and (event.key == "escape"):
            self.unclick()

    def on_view_toggle(self, event):
        """Switch the plot to the next view."""
        _ = event
        self.set_view((self.current_view + 1) % self.n_views)

    # General plot API

    def resize(self, width):
        """Resize the plot to have the given `width`."""
        width += 4  # Correction for main axes margins
        self.header.layout.width = f"{int(width)}px"

    def _clear_markers(self):
        """Remove click and slice markers. Does not force figure redrawing."""
        if self.click_marker is not None:
            self.click_marker.remove()
        self.click_marker = None

        if self.slice_marker is not None:
            self.slice_marker.remove()
        self.slice_marker = None

    def click(self, coords):
        """Trigger a click on the plot at given `coords`."""
        if not self.is_clickable:
            return
        coords = self.click_fn(coords)
        if coords is None:  # Ignore click
            return
        self._clear_markers()
        self.start_click_coords = None
        self.click_coords = coords
        self.slice_coords = None
        self.click_marker = self.ax.scatter(*coords, **self.marker_params, zorder=10)
        self.fig.canvas.draw_idle()

    def _plot_slice(self, start_coords, stop_coords):
        """Plot a line segment from `start_coords` to `stop_coords`."""
        self._clear_markers()
        self.slice_marker = self.ax.plot([start_coords[0], stop_coords[0]], [start_coords[1], stop_coords[1]],
                                         color="black", zorder=10)[0]
        self.fig.canvas.draw_idle()

    def slice(self, start_coords, stop_coords):
        """Trigger slicing of the plot from `start_coords` to `stop_coords`."""
        if not self.is_sliceable:
            return
        self.slice_fn(start_coords, stop_coords)
        self.start_click_coords = None
        self.click_coords = None
        self.slice_coords = (start_coords, stop_coords)
        self._plot_slice(start_coords, stop_coords)

    def unclick(self):
        """Undo last click or slice event."""
        if not self.is_unclickable:
            return
        if self.click_marker is None and self.slice_marker is None:
            return  # Do nothing if a click has not been performed
        self.unclick_fn()
        self._clear_markers()
        self.fig.canvas.draw_idle()

    def set_view(self, view):
        """Set the current view of the plot to the given `view`."""
        if view < 0 or view >= self.n_views:
            raise ValueError("Unknown view")
        self.unclick()
        if not self.preserve_clicks_on_view_change:
            self.click_coords = None
            self.slice_coords = None
        self.current_view = view
        self.redraw()

    def set_title(self, title=None):
        """Update the plot title. If `title` is not given, the default title of the current view is used."""
        title = get_first_defined(title, self.title)
        self.title_widget.value = TITLE_TEMPLATE.format(style=TITLE_STYLE, title=title)

    def clear(self):
        """Clear the plot axes and revert them to the initial state."""
        # Remove all axes except for the main one if they were created (e.g. a colorbar)
        for ax in self.fig.axes:
            if ax != self.ax:
                ax.remove()
        self.ax.clear()
        # Reset aspect ratio constraints if they were set
        self.ax.set_aspect("auto")
        # Stretch the axes to its original size
        self.ax.set_axes_locator(None)

    def redraw(self, clear=True):
        """Redraw the current view. Optionally clear the plot axes first."""
        if clear:
            self.clear()
        self.set_title()
        if self.plot_fn is not None:
            self.plot_fn(ax=self.ax)  # pylint: disable=not-callable
        if self.click_coords is not None:
            self.click(self.click_coords)
        if self.slice_coords is not None:
            self.slice(*self.slice_coords)

    def plot(self, display_box=True):
        """Display the interactive plot with the first view selected.

        Parameters
        ----------
        display_box : bool, optional, defaults to True
            Whether to display the plot in a JupyterLab frontend. Generally should be set to `False` if a parent object
            creates several `InteractivePlot` instances and controls their plotting.
        """
        self.redraw(clear=False)
        # Init the width of the box
        self.resize(self.fig.get_figwidth() * self.fig.dpi / self.fig.canvas.device_pixel_ratio)
        if display_box:
            display(self.box)


class DropdownViewPlot(InteractivePlot):
    """Construct an interactive plot with optional click handling.

    The plot may contain multiple views: one for each of the passed `plot_fn`. The views can be iterated over either
    using a dropdown list on top of the plot or arrow buttons on its sides. The plot is interactive: it can handle
    click and slice events, while each view may define its own processing logic.

    Plotting must be performed in a JupyterLab environment with the the `%matplotlib widget` magic executed and
    `ipympl` and `ipywidgets` libraries installed.

    Parameters
    ----------
    plot_fn : callable or list of callable, optional
        One or more plotters each accepting a single keyword argument `ax`. If not given, an empty plot is created.
    click_fn : callable or list of callable, optional
        Click handlers for views defined by `plot_fn`. Each of them must accept a tuple with 2 elements defining
        click coordinates. If a single `click_fn` is given, it is used for all views. If not given, click events are
        not handled.
    slice_fn : callable or list of callable, optional
        Slice handlers for views defined by `plot_fn`. Slice is triggered by moving the mouse with the left button
        held. Each handlers must accept two tuples with 2 elements defining coordinates of slice edges. If a single
        `slice_fn` is given, it is used for all views. If not given, slice events are not handled.
    unclick_fn : callable or list of callable, optional
        Handlers that undo clicks and slices on views defined by `plot_fn`. Each of them is called without arguments.
        If a single `unclick_fn` is given, it is used for all views. If not given, clicks and slices can not be undone.
    marker_params : dict or list of dict, optional, defaults to {"marker": "+", "color": "black"}
        Click marker parameters for views defined by `plot_fn`. Passed directly to `Axes.scatter`. If a single `dict`
        is given, it is used for all views.
    title : str or list of str, optional
        Plot titles for views defined by `plot_fn`, act as dropdown options.
    preserve_clicks_on_view_change : bool, optional, defaults to False
        Whether to preserve click/slice markers and trigger the corresponding event on view change.
    toolbar_position : {"top", "bottom", "left", "right"}, optional, defaults to "left"
        Matplotlib toolbar position relative to the main axes.
    figsize : tuple with 2 elements, optional, defaults to (4.5, 4.5)
        Size of the created figure. Measured in inches.

    Attributes
    ----------
    fig : matplotlib.figure.Figure
        The created figure.
    ax : matplotlib.axes.Axes
        Axes of the figure to plot views on.
    box : ipywidgets.widgets.widget_box.Box
        Main container that stores figure canvas, plot title, created buttons and, optionally, a toolbar.
    n_views : int
        The number of plot views.
    current_view : int
        An index of the current plot view.
    """
    def __init__(self, **kwargs):
        # Define widgets for view selection
        self.prev = widgets.Button(icon="angle-left", disabled=True, layout=widgets.Layout(**BUTTON_LAYOUT))
        self.drop = widgets.Dropdown(layout=widgets.Layout(**TEXT_LAYOUT))
        self.next = widgets.Button(icon="angle-right", disabled=True, layout=widgets.Layout(**BUTTON_LAYOUT))

        super().__init__(**kwargs)
        self.drop.options = self.title_list

        # Define handlers after options are set, otherwise plotting will be triggered
        self.prev.on_click(self.prev_view)
        self.drop.observe(self.select_view, names="value")
        self.next.on_click(self.next_view)

    def construct_buttons(self):
        """Don't use a parent button for view switching."""
        return []

    def construct_header(self):
        """Construct a header of the plot. Contains a dropdown widget with available views and two arrow buttons on its
        sides to iterate over views."""
        return widgets.HBox([self.prev, self.drop, self.next])

    def set_view(self, view):
        """Set the current view of the plot to the given `view`."""
        super().set_view(view)
        self.drop.index = view
        self.prev.disabled = (view == 0)
        self.next.disabled = (view == (self.n_views - 1))

    def next_view(self, event):
        """Switch to the next view."""
        _ = event
        self.set_view(min(self.current_view + 1, self.n_views - 1))

    def prev_view(self, event):
        """Switch to the previous view."""
        _ = event
        self.set_view(max(self.current_view - 1, 0))

    def select_view(self, change):
        """Set the current view of the plot according to the selected dropdown option."""
        _ = change
        self.set_view(self.drop.index)


class PairedPlot:
    """Construct a plot that contains two interactive plots stacked together.

    Usually one wants to display a clickable plot (`main`) which updates an auxiliary plot (`aux`) on each click. In
    this case both plots may need to have access to the current state of each other. `PairedPlot` can be treated as
    such a state container: if its bound method is used as a `plot_fn`/`click_fn`/`unclick_fn` of `main` or `aux` plots
    it gets access to both `InteractivePlot`s and all the attributes created in `PairedPlot.__init__`.

    Parameters
    ----------
    orientation : {"horizontal", "vertical"}, optional, defaults to "horizontal"
        Defines whether to stack the main and auxiliary plots horizontally or vertically.

    Attributes
    ----------
    main : InteractivePlot
        The main plot.
    aux : InteractivePlot
        The auxiliary plot.
    box : ipywidgets.widgets.widget_box.Box
        A container that stores boxes of both `main` and `aux`.
    """
    def __init__(self, orientation="horizontal"):
        if orientation == "horizontal":
            box_type = widgets.HBox
        elif orientation == "vertical":
            box_type = widgets.VBox
        else:
            raise ValueError("Unknown plot orientation, must be either 'horizontal' or 'vertical'")

        self.main = self.construct_main_plot()
        self.aux = self.construct_aux_plot()
        self.box = box_type([self.main.box, self.aux.box])

    def construct_main_plot(self):
        """Construct the main plot. Must be overridden in child classes."""
        raise NotImplementedError

    def construct_aux_plot(self):
        """Construct the auxiliary plot. Must be overridden in child classes."""
        raise NotImplementedError

    def plot(self):
        """Display the paired plot."""
        self.main.plot(display_box=False)
        self.aux.plot(display_box=False)
        display(self.box)