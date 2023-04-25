"""Defines base containers - mixin classes that implement properties and basic processing logic for objects that store
particular types of data:
* `SamplesContainer` - implements extra properties for subclasses with defined `samples` attribute,
* `TraceContainer` - implements properties and processing methods for subclasses with defined `headers` attribute,
* `GatherContainer` - a subclass of `TraceContainer` that also implements fast selection of gather headers by index.
"""

import warnings
from copy import deepcopy

import numpy as np
import pandas as pd
import polars as pl

from .decorators import batch_method
from .utils import to_list, get_cols, create_indexer, maybe_copy


class SamplesContainer:
    """A mixin class that implements extra properties for concrete subclasses with defined `samples` attribute that
    stores recording times for each trace value as a 1d `np.ndarray`."""

    @property
    def times(self):
        """1d np.ndarray of floats: Recording time for each trace value. Measured in milliseconds."""
        return self.samples

    @property
    def n_samples(self):
        """int: Trace length in samples."""
        return len(self.samples)

    @property
    def n_times(self):
        """int: Trace length in samples."""
        return len(self.times)


class TraceContainer:
    """A mixin class that implements extra properties and processing methods for concrete subclasses with defined
    `headers` attribute that stores loaded trace headers as a `pd.DataFrame`."""

    @property
    def indexed_by(self):
        """str or list of str: Names of header indices."""
        index_names = list(self.headers.index.names)
        if len(index_names) == 1:
            return index_names[0]
        return index_names

    @property
    def available_headers(self):
        """set of str: Names of available trace headers: both loaded and created manually."""
        return set(self.headers.columns) | set(self.headers.index.names)

    @property
    def n_traces(self):
        """int: The number of traces."""
        return len(self.headers)

    def __getitem__(self, key):
        """Select values of trace headers by their names and return them as a `np.ndarray`. Unlike `pandas` indexing
        allows for selection of headers the container is indexed by. The returned array will be 1d if a single header
        is selected and 2d otherwise.

        Parameters
        ----------
        key : str or list of str
            Names of headers to get values for.

        Returns
        -------
        result : np.ndarray
            Headers values.
        """
        return get_cols(self.headers, key)

    def __setitem__(self, key, value):
        """Set given values to selected headers.

        Parameters
        ----------
        key : str or list of str
            Headers to set values for.
        value : np.ndarray
            Headers values to set.
        """
        self.headers[key] = value

    def get_headers(self, cols, preserve_dtype=False):
        """Select values of trace headers by their names and return them as a `pandas.DataFrame`. Unlike `pandas`
        indexing allows for selection of headers the container is indexed by.

        Parameters
        ----------
        cols : str or list of str
            Names of headers to get values for.

        Returns
        -------
        result : pandas.DataFrame
            Headers values.
        """
        if isinstance(self.headers.index, pd.MultiIndex):
            index_dtype = self.headers.index.dtypes
        else:
            index_dtype = pd.Series({self.headers.index.name: self.headers.index.dtype})
        dtypes = pd.concat((self.headers.dtypes, index_dtype))

        headers = pd.DataFrame(self[cols], columns=to_list(cols))
        return headers.astype(dtypes[cols]) if preserve_dtype else headers

    def copy(self, ignore=None):
        """Perform a deepcopy of all attributes of `self` except for those specified in `ignore`, which are kept
        unchanged.

        Parameters
        ----------
        ignore : str or array-like of str, defaults to None
            Attributes that won't be copied.

        Returns
        -------
        copy : same type as self
            Copy of `self`.
        """
        ignore = set() if ignore is None else set(to_list(ignore))
        ignore_attrs = [getattr(self, attr) for attr in ignore]

        # Construct a memo dict with attributes, that should not be copied
        memo = {id(attr): attr for attr in ignore_attrs}
        return deepcopy(self, memo)

    @staticmethod
    def _apply(func, df, axis, unpack_args, **kwargs):
        """Apply a function to a `pd.DataFrame` along the specified axis.

        Parameters
        ----------
        func : callable
            A function to be applied to `df`.
        df : pd.DataFrame
            A `DataFrame` to which the function will be applied.
        axis : {0 or "index", 1 or "columns", None}
            An axis along which the function is applied:
            - 0 or "index": apply a function to each column,
            - 1 or "columns": apply a function to each row,
            - `None`: apply a function to the `DataFrame` as a whole.
        unpack_args : bool
            If `True`, row or column values are passed to `func` as individual arguments, otherwise the whole array is
            passed as a single arg. If `axis` is `None` and `unpack_args` is `True`, columns of the `df` are passed to
            the `func` as individual arguments.
        kwargs : misc, optional
            Additional keyword arguments to be passed to `func` or `pd.DataFrame.apply`.

        Returns
        -------
        result : np.ndarray
            The result of applying `func` to `df`.
        """
        if axis is None:
            args = (col_val for _, col_val in df.items()) if unpack_args else (df,)
            res = func(*args, **kwargs)
        else:
            # FIXME: Workaround for a pandas bug https://github.com/pandas-dev/pandas/issues/34822
            # raw=True causes incorrect apply behavior when axis=1 and multiple values are returned from `func`
            raw = axis != 1

            apply_func = (lambda args, **kwargs: func(*args, **kwargs)) if unpack_args else func
            res = df.apply(apply_func, axis=axis, raw=raw, result_type="expand", **kwargs)

        # Convert np.ndarray/pd.Series/pd.DataFrame outputs from `func` to a 2d array
        return pd.DataFrame(res).to_numpy()

    def _post_filter(self, mask):
        """Implement extra filtering logic of concrete subclass attributes if some of them should also be filtered
        besides `headers`."""
        _ = mask
        return

    @batch_method(target="for")
    def filter(self, cond, cols, axis=None, unpack_args=False, inplace=False, **kwargs):
        """Keep only those rows of `headers` where `cond` is `True`.

        Examples
        --------
        Keep only traces whose offset is less than 1500 meters:
        >>> survey = Survey(path, header_index="FieldRecord", header_cols=["TraceNumber", "offset"], name="survey")
        >>> survey.filter(lambda offset: offset < 1500, cols="offset", inplace=True)

        Parameters
        ----------
        cond : callable
            A function to be applied to `self.headers` to get a filtering mask. Must return a boolean array whose
            length equals to the length of `headers` and `True` values correspond to traces to keep.
        cols : str or list of str
            `self.headers` columns for which condition is checked.
        axis : {0 or "index", 1 or "columns", None}, optional, defaults to None
            An axis along which `cond` is applied:
            - 0 or "index": apply `cond` to each column,
            - 1 or "columns": apply `cond` to each row,
            - `None`: apply `cond` to the `DataFrame` as a whole.
        unpack_args : bool, optional, defaults to False
            If `True`, row or column values are passed to `cond` as individual arguments, otherwise the whole array is
            passed as a single arg. If `axis` is `None` and `unpack_args` is `True`, each column from `cols` is passed
            to the `cond` as an individual argument.
        inplace : bool, optional, defaults to False
            Whether to perform filtering inplace or process a copy.
        kwargs : misc, optional
            Additional keyword arguments to be passed to `cond` or `pd.DataFrame.apply`.

        Returns
        -------
        result : same type as self
            Filtered `self`.

        Raises
        ------
        ValueError
            If `cond` returns more than one bool value for each row of `headers`.
        """
        self = maybe_copy(self, inplace, ignore="headers")  # pylint: disable=self-cls-assignment
        cols = to_list(cols)
        headers = self.get_headers(cols)
        mask = self._apply(cond, headers, axis=axis, unpack_args=unpack_args, **kwargs)
        if (mask.ndim != 2) or (mask.shape[1] != 1):
            raise ValueError("cond must return a single value for each header row")
        if mask.dtype != np.bool_:
            raise ValueError("cond must return a bool value for each header row")
        mask = mask[:, 0]
        # Guarantee that a copy is set
        self.headers = self.headers.loc[mask].copy()  # pylint: disable=attribute-defined-outside-init
        if len(self.headers) == 0:
            warnings.warn("Empty headers after filtering", RuntimeWarning)
        self._post_filter(mask)
        return self

    @batch_method(target="for")
    def apply(self, func, cols, res_cols=None, axis=None, unpack_args=False, inplace=False, **kwargs):
        """Apply a function to `self.headers` along the specified axis.

        Examples
        --------
        Convert signed offsets to their absolute values:
        >>> survey = Survey(path, header_index="FieldRecord", header_cols=["TraceNumber", "offset"], name="survey")
        >>> survey.apply(lambda offset: np.abs(offset), cols="offset", inplace=True)

        Parameters
        ----------
        func : callable
            A function to be applied to `self.headers`. Must return a 2d object with shape (`len(self.headers)`,
            `len(res_cols)`).
        cols : str or list of str
            `self.headers` columns for which the function is applied.
        res_cols : str or list of str, optional, defaults to None
            `self.headers` columns in which the result is saved. If not given, equals to `cols`.
        axis : {0 or "index", 1 or "columns", None}, optional, defaults to None
            An axis along which the function is applied:
            - 0 or "index": apply a function to each column,
            - 1 or "columns": apply a function to each row,
            - `None`: apply a function to the `DataFrame` as a whole.
        unpack_args : bool, optional, defaults to False
            If `True`, row or column values are passed to `func` as individual arguments, otherwise the whole array is
            passed as a single arg. If `axis` is `None` and `unpack_args` is `True`, each column from `cols` is passed
            to the `func` as an individual argument.
        inplace : bool, optional, defaults to False
            Whether to apply the function inplace or to a copy.
        kwargs : misc, optional
            Additional keyword arguments to be passed to `func` or `pd.DataFrame.apply`.

        Returns
        -------
        result : same type as self
            `self` with the function applied.
        """
        self = maybe_copy(self, inplace)  # pylint: disable=self-cls-assignment
        cols = to_list(cols)
        headers = self.get_headers(cols)
        res_cols = cols if res_cols is None else to_list(res_cols)
        res = self._apply(func, headers, axis=axis, unpack_args=unpack_args, **kwargs)
        self.headers[res_cols] = res
        return self

    def load_headers(self, path, headers=None, join_on_headers=None, format="fwf", has_header=False, usecols=None,  # pylint: disable=too-many-arguments
                     sep=None, skiprows=None, decimal=None, encoding="UTF-8", keep_all_headers=False, inplace=False,
                     **kwargs):
        """Load headers from a file and join them with the existing `self.headers`.
        TODO: REWRITE
        Parameters:
        -----------
        path : str
            Path to the file with headers.
        names : list of str, optional, defaults to None
            List of column names to use for headers. If not provided, column names will be inferred from the file.
            Passed directly to `pandas.read_csv`.
        index_col : int, str or array-like, optional, defaults to None
            Column(s) to use as index. If not provided, default index will be used.
            Passed directly to `pandas.read_csv`.
        format : "fwf" or "csv", optional, defaults to "fwf"
            Format of the file with headers. Only "fwf" (fixed-width format) and "csv" format with single-character
            separator are supported.
        sep : str, optional, defaults to None
            Delimiter to use. If not provided, it will be inferred base on the `format`.
        usecols : array-like, optional, defaults to None
            Columns to read from the file. If not provided, all columns will be read.
        skiprows : int, optional, defaults to None
            Number of rows to skip from the beginning of the file.
        engine : str, optional, defaults to "pyarrow"
            Parser engine to use for the "csv" format.
        decimal : str, optional, defaults to None
            Decimal point character. If not provided, it will be inferred from the file.
        encoding : str, optional, defaults to "UTF-8"
            Encoding to use for the file.
        keep_all_headers : bool, optional, defaults to False
            Whether to keep headers for traces that were missed in the loaded file.
        inplace : bool, optional, defaults to False
            Whether to load headers inplace or to a copy.
        **kwargs : misc, optional
            Additional arguments to pass to the pandas `read_csv` function.

        Returns
        -------
        result : same type as self
            `self` with the loaded headers.

        Raises:
        -------
        ValueError
            If the `format` argument is not one of the supported formats ('fwf', 'csv').
        """
        if format not in ["fwf", "csv"]:
            raise ValueError(f"Unknown format `{format}`, available formats are ('fwf', 'csv')")

        self = maybe_copy(self, inplace, ignore="headers")  # pylint: disable=self-cls-assignment

        # Processing negative usecols
        if usecols is not None:
            usecols = np.asarray(usecols)
            if any(usecols < 0):
                sep = sep or ',' if format == "csv" else None
                with open(path, 'r', encoding=encoding) as f:
                    n_cols = len(f.readline().split(sep))
                usecols[usecols < 0] = n_cols + usecols[usecols < 0]
            usecols = usecols.tolist()

        if format == "fwf":
            # If decimal is not provided, try inferring it from the file
            if decimal is None:
                with open(path, 'r', encoding=encoding) as f:
                    n_skip = has_header + 1 + (0 if skiprows is None else skiprows)
                    row = [next(f) for _ in range(n_skip)][-1]
                decimal = '.' if '.' in row else ','
            header = 0 if has_header else None
            loaded_headers = pd.read_csv(path, sep=r'\s+', header=header, names=headers, usecols=usecols,
                                         decimal=decimal, skiprows=skiprows, encoding=encoding, **kwargs)
            loaded_headers = pl.from_pandas(loaded_headers)
        else:
            sep = ',' if sep is None else sep
            if has_header:
                columns = headers
                new_columns = None
            else:
                columns = usecols
                new_columns = headers
            skiprows = 0 if skiprows is None else skiprows
            loaded_headers = pl.read_csv(path, has_header=has_header, columns=columns, new_columns=new_columns,
                                         separator=sep, skip_rows=skiprows, encoding=encoding, **kwargs)

        how = "left" if keep_all_headers else "inner"
        index_cols = self.headers.index.names  # pylint: disable=access-member-before-definition
        headers = pl.from_pandas(self.headers.reset_index())  # pylint: disable=access-member-before-definition
        # Use intersection of columns from file and self.headers as join columns by default
        if join_on_headers is None:
            join_on_headers = list(set(headers.columns) & set(loaded_headers.columns))
        casts = [loaded_headers[column].cast(headers[column].dtype) for column in to_list(join_on_headers)]
        loaded_headers = loaded_headers.with_columns(*casts)
        headers = headers.join(loaded_headers, on=join_on_headers, how=how, suffix="_loaded")
        self.headers = headers.to_pandas().set_index(index_cols)  # pylint: disable=attribute-defined-outside-init

        if self.is_empty:
            warnings.warn("Empty headers after headers loading", RuntimeWarning)
        return self

    def dump_headers(self, path, columns, format="fwf", sep=',', col_space=8, dump_col_names=False, **kwargs):
        """Save the selected columns from headers into a file.

        Parameters
        ----------
        path : str
            Path to the output file.
        columns : str or array-like of str
            The column names from `self.headers` to be included in the output file.
        format : "fwf" or "csv", optional, defaults to "fwf"
            The output file format. If "fwf", use fixed-width format with a width defined by `col_space`. If "csv", use
            single-separated values format with a separator `sep`.
        sep : str, optional, defaults to ','
            The separator used in the output file. It is only used when `format="csv"`.
        col_space : int, optional, defaults to 8
            The column width in characters when `format="fwf"`.
        dump_col_names : bool, optional, defaults to False
            Whether to include the column names in the output file.
        kwargs : misc, optional
            Additional arguments for dumping function. If `format="fwf"`, passed to `pandas.to_string`.
            If `format="csv"`, passed to `polars.write_csv`.

        Returns
        -------
        result : same type as self
            `self` unchanged.

        Raises
        ------
        ValueError
            If the `format` argument is not one of the supported formats ('fwf', 'csv').
        """
        dump_df = self.get_headers(columns, preserve_dtype=True)
        if format == "fwf":
            dump_df.to_string(path, col_space=col_space, header=dump_col_names, index=False, **kwargs)
        elif format == "csv":
            dump_df = pl.from_pandas(dump_df)
            dump_df.write_csv(path, has_header=dump_col_names, separator=sep, **kwargs)
        else:
            raise ValueError(f"Unknown format {format}, available formats are ('fwf', 'csv')")
        return self


class GatherContainer(TraceContainer):
    """A mixin class that implements extra properties and processing methods for concrete subclasses with defined
    `headers` attribute that stores loaded trace headers for several gathers as a `pd.DataFrame` and means for fast
    selection of gather headers by index."""

    def __len__(self):
        """The number of gathers."""
        return self.n_gathers

    def __contains__(self, index):
        """Returns whether a gather with given `index` is presented in `headers`."""
        return index in self.indices

    @property
    def headers(self):
        """pd.DataFrame: loaded trace headers."""
        return self._headers

    @headers.setter
    def headers(self, headers):
        """Reconstruct an indexer on each headers assignment."""
        if not (headers.index.is_monotonic_increasing or headers.index.is_monotonic_decreasing):
            headers = headers.sort_index(kind="stable")
        self._indexer = create_indexer(headers.index)
        self._headers = headers

    @property
    def indices(self):
        """pd.Index: indices of gathers."""
        return self._indexer.unique_indices

    @property
    def n_gathers(self):
        """int: The number of gathers."""
        return len(self.indices)

    @property
    def is_empty(self):
        """bool: Whether no gathers are stored in the container."""
        return self.n_gathers == 0

    def get_traces_locs(self, indices):
        """Get positions of traces in `headers` by `indices` of their gathers.

        Parameters
        ----------
        indices : array-like
            Indices of gathers to get trace locations for.

        Returns
        -------
        locations : array-like
            Locations of traces of the requested gathers.
        """
        return self._indexer.get_locs_in_indices(indices)

    def get_gathers_locs(self, indices):
        """Get ordinal positions of gathers in the container by their `indices`.

        Parameters
        ----------
        indices : array-like
            Indices of gathers to get ordinal positions for.

        Returns
        -------
        locations : np.ndarray
            Locations of the requested gathers.
        """
        return self._indexer.get_locs_in_unique_indices(indices)

    def get_headers_by_indices(self, indices):
        """Return headers for gathers with given `indices`.

        Parameters
        ----------
        indices : array-like
            Indices of gathers to get headers for.

        Returns
        -------
        headers : pd.DataFrame
            Selected headers values.
        """
        return self.headers.iloc[self.get_traces_locs(indices)]

    def copy(self, ignore=None):
        """Perform a deepcopy of all attributes of `self` except for indexer and those specified in `ignore`, which are
        kept unchanged.

        Parameters
        ----------
        ignore : str or array-like of str, defaults to None
            Attributes that won't be copied.

        Returns
        -------
        copy : same type as self
            Copy of `self`.
        """
        ignore = set() if ignore is None else set(to_list(ignore))
        return super().copy(ignore | {"_indexer"})

    def reindex(self, new_index, inplace=False):
        """Change the index of `self.headers` to `new_index`.

        Parameters
        ----------
        new_index : str or list of str
            Headers columns to become a new index.
        inplace : bool, optional, defaults to False
            Whether to perform reindexation inplace or return a new instance.

        Returns
        -------
        self : same type as self
            Reindexed self.
        """
        self = maybe_copy(self, inplace)  # pylint: disable=self-cls-assignment
        headers = self.headers
        headers.reset_index(inplace=True)
        headers.set_index(new_index, inplace=True)
        headers.sort_index(kind="stable", inplace=True)
        self.headers = headers
        return self
