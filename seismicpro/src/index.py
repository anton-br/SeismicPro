"""Implements SeismicIndex class that allows for iteration over gathers in a survey or a group of surveys"""

import os
import warnings
from textwrap import indent, dedent
from functools import partial, reduce

import numpy as np
import pandas as pd

from .survey import Survey
from .containers import GatherContainer
from .utils import to_list
from ..batchflow import DatasetIndex


class IndexPart(GatherContainer):
    def __init__(self):
        self._headers = None
        self.indexer = None
        self.common_headers = set()
        self.surveys_dict = {}

    @property
    def survey_names(self):
        return sorted(self.surveys_dict.keys())

    @classmethod
    def from_attributes(cls, headers, surveys_dict, common_headers):
        part = cls()
        part.headers = headers
        part.common_headers = common_headers
        part.surveys_dict = surveys_dict
        return part

    @classmethod
    def from_survey(cls, survey, copy_headers=False):
        headers = survey.headers.copy(copy_headers)
        headers.columns = pd.MultiIndex.from_product([[survey.name], headers.columns])

        part = cls()
        part._headers = headers  # Avoid calling headers setter since the indexer is already calculated
        part.indexer = survey.indexer
        part.common_headers = set(headers.columns) - {"TRACE_SEQUENCE_FILE"}
        part.surveys_dict = {survey.name: survey}
        return part

    @staticmethod
    def _filter_equal(df, cols):
        drop_mask = reduce(np.logical_or, [np.ptp(df.loc[:, (slice(None), col)], axis=1) for col in cols])
        return df.loc[~drop_mask]

    def merge(self, other, on=None, validate="1:1"):
        self_indexed_by = set(to_list(self.indexed_by))
        other_indexed_by = set(to_list(other.indexed_by))
        if self_indexed_by != other_indexed_by:
            raise ValueError("All parts must be indexed by the same headers")
        if set(self.survey_names) & set(other.survey_names):
            raise ValueError("Only surveys with unique names can be merged")

        common_headers = self.common_headers & other.common_headers
        if on is None:
            on = common_headers
            left_df = self.headers
            right_df = other.headers
        else:
            on = set(to_list(on)) - self_indexed_by
            # Filter both self and other by equal values of on
            left_df = self._filter_equal(self.headers, on)
            right_df = self._filter_equal(other.headers, on)
        headers_to_check = common_headers - on

        merge_on = sorted(on)
        left_survey_name = self.survey_names[0]
        right_survey_name = other.survey_names[0]
        left_on = to_list(self.indexed_by) + [(left_survey_name, header) for header in merge_on]
        right_on = to_list(other.indexed_by) + [(right_survey_name, header) for header in merge_on]

        headers = pd.merge(left_df, right_df, how="inner", left_on=left_on, right_on=right_on, copy=True, sort=False,
                           validate=validate)

        # Recalculate common headers in the merged DataFrame
        common_headers = on | {header for header in headers_to_check
                                      if headers[left_survey_name, header].equals(headers[right_survey_name, header])}
        return self.from_attributes(headers, {**self.surveys_dict, **other.surveys_dict}, common_headers)

    def create_subset(self, indices):
        subset_headers = self.get_headers_by_indices(indices)
        return self.from_attributes(subset_headers, self.surveys_dict, self.common_headers)


class SeismicIndex(DatasetIndex):
    def __init__(self, *args, mode=None, copy_headers=False, **kwargs):
        self.parts = tuple()
        super().__init__(*args, mode=mode, copy_headers=copy_headers, **kwargs)

    @property
    def n_parts(self):
        return len(self.parts)

    @property
    def n_gathers_by_part(self):
        return [part.n_gathers for part in self.parts]

    @property
    def n_gathers(self):
        return sum(self.n_gathers_by_part)

    @property
    def n_traces_by_part(self):
        return [part.n_traces for part in self.parts]

    @property
    def n_traces(self):
        return sum(self.n_traces_by_part)

    @property
    def indexed_by(self):
        if self.parts:
            return self.parts[0].indexed_by
        return None

    @property
    def survey_names(self):
        if self.parts:
            return self.parts[0].survey_names
        return None

    @property
    def is_empty(self):
        return self.n_parts == 0

    def __len__(self):
        return self.n_gathers

    def get_index_info(self, index_path="index", indent_size=0):
        """Recursively fetch index description string from the index itself and all the nested subindices."""
        if self.is_empty:
            return "Empty index"

        splits = [(name, getattr(self, name)) for name in ("train", "test", "validation")
                                            if getattr(self, name) is not None]

        info_df = pd.DataFrame({"Gathers": self.n_gathers_by_part, "Traces": self.n_traces_by_part},
                            index=pd.RangeIndex(self.n_parts, name="Part"))
        for sur in self.survey_names:
            info_df[f"Survey {sur}"] = [os.path.basename(part.surveys_dict[sur].path) for part in self.parts]

        msg = f"""
        {index_path} info:

        Indexed by:                {', '.join(to_list(self.indexed_by))}
        Number of gathers:         {self.n_gathers}
        Number of traces:          {self.n_traces}
        Is split:                  {any(splits)}

        Index parts info:
        """
        msg = indent(dedent(msg) + info_df.to_string() + "\n", " " * indent_size)

        for name, index in splits:
            msg += "_" * 79 + "\n" + index.get_index_info(index_path=f"{index_path}.{name}", indent_size=indent_size+4)
        return msg

    def __str__(self):
        """Print index metadata including information about its surveys and total number of traces and gathers."""
        msg = self.get_index_info()
        for i, part in enumerate(self.parts):
            for sur in part.survey_names:
                msg += "_" * 79 + "\n\n" + f"Part {i}, Survey {sur}\n\n" + str(part.surveys_dict[sur]) + "\n"
        return msg.strip()

    def info(self):
        """Print index metadata including information about its surveys and total number of traces and gathers."""
        print(self)

    #------------------------------------------------------------------------#
    #                         Index creation methods                         #
    #------------------------------------------------------------------------#

    @classmethod
    def _args_to_indices(cls, *args, copy_headers=False):
        indices = []
        for arg in args:
            if isinstance(arg, Survey):
                builder = cls.from_survey
            elif isinstance(arg, IndexPart):
                builder = cls.from_parts
            elif isinstance(arg, SeismicIndex):
                builder = cls.from_index
            else:
                raise ValueError(f"Unsupported type {type(arg)} to convert to index")
            indices.append(builder(arg, copy_headers=copy_headers))
        return indices

    @classmethod
    def _combine_indices(cls, *indices, mode=None, **kwargs):
        builders_dict = {
            "m": cls.merge,
            "merge": cls.merge,
            "c": partial(cls.concat, copy_headers=False),
            "concat": partial(cls.concat, copy_headers=False),
        }
        if mode not in builders_dict:
            raise ValueError(f"Unknown mode {mode}")
        return builders_dict[mode](*indices, **kwargs)

    def build_index(self, *args, mode=None, copy_headers=False, **kwargs):
        # Create an empty index if no args are given
        if not args:
            return tuple()

        # Don't copy headers if args are merged since pandas.merge will return a copy
        if mode in {"m", "merge"}:
            copy_headers = False

        # Convert all args to SeismicIndex and combine them into a single index
        args = self._args_to_indices(*args, copy_headers=copy_headers)
        index = args[0] if len(args) == 1 else self._combine_indices(*args, mode=mode, **kwargs)

        # Copy parts from the created index to self and return gather indices for each part
        self.parts = index.parts
        return index.index

    @classmethod
    def from_parts(cls, *parts, copy_headers=False):
        survey_names = parts[0].survey_names
        if any(survey_names != part.survey_names for part in parts[1:]):
            raise ValueError("Only parts with the same survey names can be concatenated into one index")

        indexed_by = parts[0].indexed_by
        if any(indexed_by != part.indexed_by for part in parts[1:]):
            raise ValueError("All parts must be indexed by the same columns")

        # Warn about empty index or some of its parts
        empty_parts = [i for i, part in enumerate(parts) if not part]
        if len(empty_parts) == len(parts):
            warnings.warn("All index parts are empty, empty index is created", RuntimeWarning)
        elif empty_parts:
            warnings.warn(f"Index parts {empty_parts} are empty", RuntimeWarning)

        if copy_headers:
            # TODO: copy headers
            parts = parts

        index = cls()
        index.parts = parts
        index._index = tuple(part.indices for part in parts)
        index.reset("iter")
        return index

    @classmethod
    def from_survey(cls, survey, copy_headers=False):
        return cls.from_parts(IndexPart.from_survey(survey, copy_headers=copy_headers))

    @classmethod
    def from_index(cls, index, copy_headers=False):
        if not copy_headers:
            return index
        # TODO: copy headers
        return index

    @classmethod
    def concat(cls, *args, copy_headers=False):
        indices = cls._args_to_indices(*args, copy_headers=copy_headers)
        parts = sum([index.parts for index in indices], tuple())
        return cls.from_parts(*parts, copy_headers=False)

    @classmethod
    def merge(cls, *args, **kwargs):
        indices = cls._args_to_indices(*args, copy_headers=False)
        if len({ix.n_parts for ix in indices}) != 1:
            raise ValueError
        ix_parts = [ix.parts for ix in indices]
        merged_parts = [reduce(lambda x, y: x.merge(y, **kwargs), parts) for parts in zip(*ix_parts)]
        return cls.from_parts(*merged_parts, copy_headers=False)

    #------------------------------------------------------------------------#
    #                 DatasetIndex interface implementation                  #
    #------------------------------------------------------------------------#

    def build_pos(self):
        return None

    def subset_by_pos(self, pos):
        pos = np.sort(np.atleast_1d(pos))
        part_pos_borders = np.cumsum([0] + self.n_gathers_by_part)
        pos_by_part = np.split(pos, np.searchsorted(pos, part_pos_borders[1:]))
        part_indices = [part_pos - part_start for part_pos, part_start in zip(pos_by_part, part_pos_borders[:-1])]
        return tuple(index[subset] for index, subset in zip(self.index, part_indices))

    def create_subset(self, index):
        if len(index) != self.n_parts:
            raise ValueError("Index length must match the number of parts")
        return self.from_parts(*[part.create_subset(ix) for part, ix in zip(self.parts, index)], copy_headers=False)

    #------------------------------------------------------------------------#
    #                     Statistics computation methods                     #
    #------------------------------------------------------------------------#

    def collect_stats(self, n_quantile_traces=100000, quantile_precision=2, limits=None, bar=True):
        """Collect the following trace data statistics for each survey in the dataset:
        1. Min and max amplitude,
        2. Mean amplitude and trace standard deviation,
        3. Approximation of trace data quantiles with given precision,
        4. The number of dead traces.

        Since fair quantile calculation requires simultaneous loading of all traces from the file we avoid such memory
        overhead by calculating approximate quantiles for a small subset of `n_quantile_traces` traces selected
        randomly. Moreover, only a set of quantiles defined by `quantile_precision` is calculated, the rest of them are
        linearly interpolated by the collected ones.

        After the method is executed all calculated values can be obtained via corresponding attributes for all the
        surveys in the dataset and theirs `has_stats` flag is set to `True`.

        Examples
        --------
        Statistics calculation for the whole dataset can be done as follows:
        >>> survey = Survey(path, header_index="FieldRecord", header_cols=["TraceNumber", "offset"], name="survey")
        >>> dataset = SeismicDataset(surveys=survey).collect_stats()

        After a train-test split is performed, `train` and `test` parts of the dataset share lots of their attributes
        in common allowing for `collect_stats` to be used to calculate statistics for the training set and be available
        for gathers in the testing set avoiding data leakage during machine learning model training:
        >>> dataset.split()
        >>> dataset.train.collect_stats()
        >>> dataset.test.next_batch(1).load(src="survey").scale_standard(src="survey", use_global=True)

        But note that if no gathers from a particular survey were included in the training set its stats won't be
        collected!

        Parameters
        ----------
        n_quantile_traces : positive int, optional, defaults to 100000
            The number of traces to use for quantiles estimation.
        quantile_precision : positive int, optional, defaults to 2
            Calculate an approximate quantile for each q with `quantile_precision` decimal places. All other quantiles
            will be linearly interpolated on request.
        stats_limits : int or tuple or slice, optional
            Time limits to be used for statistics calculation. `int` or `tuple` are used as arguments to init a `slice`
            object. If not given, whole traces are used. Measured in samples.
        bar : bool, optional, defaults to True
            Whether to show a progress bar.

        Returns
        -------
        dataset : SeismicDataset
            A dataset with collected stats. Sets `has_stats` flag to `True` and updates statistics attributes inplace
            for each of the underlying surveys.
        """
        for part in self.parts:
            for sur in part.surveys_dict.values():
                sur.collect_stats(indices=part.indices, n_quantile_traces=n_quantile_traces,
                                  quantile_precision=quantile_precision, limits=limits, bar=bar)
        return self

    #------------------------------------------------------------------------#
    #                            Loading methods                             #
    #------------------------------------------------------------------------#

    def get_gather(self, index, part=None, survey_name=None, limits=None, copy_headers=False):
        if part is None and self.n_parts > 1:
            raise ValueError("part must be specified if the index is concatenated")
        if part is None:
            part = 0
        index_part = self.parts[part]

        if survey_name is None and len(self.survey_names) > 1:
            raise ValueError("survey_name must be specified if the index is merged")
        if survey_name is None:
            survey_name = self.survey_names[0]
        survey = index_part.surveys_dict[survey_name]

        gather_headers = index_part.get_headers_by_indices((index,))[survey_name]
        return survey.load_gather(headers=gather_headers, limits=limits, copy_headers=copy_headers)

    def sample_gather(self, part=None, survey_name=None, limits=None, copy_headers=False):
        if part is None:
            part_weights = np.array(self.n_gathers_by_part) / self.n_gathers
            part = np.random.choice(self.n_parts, p=part_weights)
        if survey_name is None:
            survey_name = np.random.choice(self.survey_names)
        index = np.random.choice(self.parts[part].indices)
        return self.get_gather(index, part, survey_name, limits=limits, copy_headers=copy_headers)
