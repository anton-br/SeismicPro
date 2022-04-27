"""Implements SeismicDataset class that allows for iteration over gathers in a survey or a group of surveys and their
joint processing"""

from functools import wraps
from textwrap import dedent

from .batch import SeismicBatch
from .index import SeismicIndex
from ..batchflow import Dataset


def delegate_constructors(*constructors):
    """Implement given `constructors` of `SeismicDataset` by calling the corresponding index constructor and then
    turning the result into a dataset. In addition to all the arguments of the index method each created constructor
    accepts `batch_class` argument with the same behavior as in `SeismicDataset.__init__`."""
    def decorator(cls):
        for constructor in constructors:
            index_constructor = getattr(cls.index_class, constructor)
            @wraps(index_constructor)
            def dataset_constructor(*args, index_constructor=index_constructor, batch_class=SeismicBatch, **kwargs):
                return cls(index_constructor(*args, **kwargs), batch_class=batch_class)
            setattr(cls, constructor, dataset_constructor)
        return cls
    return decorator


def delegate_to_index(*methods):
    """Implement given `methods` of `SeismicDataset` by calling the corresponding method of its index. Each created
    method preserves the signature of the index method."""
    def decorator(cls):
        for method in methods:
            index_method = getattr(cls.index_class, method)
            @wraps(index_method)
            def method_fn(self, *args, index_method=index_method, inplace=False, **kwargs):
                index = index_method(self.index, *args, inplace=inplace, **kwargs)
                if inplace:
                    return self.set_index(index)
                return self.from_index(index)
            setattr(cls, method, method_fn)
        return cls
    return decorator


@delegate_constructors("from_parts", "from_survey", "concat", "merge")
@delegate_to_index("reindex", "filter", "apply")
class SeismicDataset(Dataset):
    """A dataset, that contains identifiers of seismic gathers from a survey or a group of surveys and allows for
    generation of small subsets of gathers called batches for their joint processing.

    Gather identification in a dataset is performed via :class:`~index.SeismicIndex`, which is constructed on
    dataset instantiation and stored in the `index` attribute. Moreover, the dataset redirects almost all method calls
    to the underlying index, so please refer to its documentation to learn more about its functionality.

    Examples
    --------
    Let's consider a survey we want to process:
    >>> survey = Survey(path, header_index="FieldRecord", header_cols=["TraceNumber", "offset"], name="survey")

    Dataset creation is identical to that of :class:`~index.SeismicIndex`. Here we create a dataset from a single
    survey:
    >>> dataset = SeismicDataset(survey)

    Several surveys can be passed as well, in this case they will be combined together either by merging or
    concatenating depending on the `mode` provided.

    After the dataset is created, a subset of gathers can be obtained via :func:`~SeismicDataset.next_batch` method:
    >>> batch = dataset.next_batch(10)

    A batch of 10 gathers was created and can now be processed using the methods defined in
    :class:`~batch.SeismicBatch`. The batch does not contain any data yet and gather loading is usually the first
    method you want to call:
    >>> batch.load(src="survey")

    Note, that here we've specified the name of the survey we want to obtain gathers from in `src` argument.

    Parameters
    ----------
    args : tuple of Survey, IndexPart or SeismicIndex
        A sequence of surveys, indices or parts to construct an index.
    mode : {"c", "concat", "m", "merge", None}, optional, defaults to None
        A mode used to combine multiple `args` into a single index. If `None`, only one positional argument can be
        passed.
    copy_headers : bool, optional, defaults to False
        Whether to copy a `DataFrame` of trace headers while constructing index parts.
    batch_class : type, optional, defaults to SeismicBatch
        A class of batches, generated by a dataset. Must be inherited from :class:`~batchflow.Batch`.
    kwargs : misc, optional
        Additional keyword arguments to :func:`~SeismicIndex.merge` if the corresponding mode was chosen.

    Attributes
    ----------
    index : SeismicIndex
        Unique identifiers of seismic gathers in the constructed dataset. Contains combined trace headers and
        references to surveys to get gathers from.
    batch_class : type
        A class of batches, generated by a dataset. Usually has :class:`~batch.SeismicBatch` type.
    """
    index_class = SeismicIndex

    def __init__(self, *args, mode=None, copy_headers=False, batch_class=SeismicBatch, **kwargs):
        index = self.index_class(*args, mode=mode, copy_headers=copy_headers, **kwargs)
        super().__init__(index, batch_class=batch_class)

    def __getattr__(self, name):
        """Redirect requests to undefined attributes and methods to the underlying index."""
        return getattr(self.index, name)

    def __dir__(self):
        """Fix autocompletion for redirected methods."""
        return sorted(set(super().__dir__()) | set(dir(self.index)))

    def __len__(self):
        """The number of gathers in the dataset."""
        return self.n_gathers

    def __str__(self):
        """Print dataset metadata including information about its batch class and index."""
        msg = f"""
        Batch class:               {self.batch_class}
        Index class:               {type(self.index)}

        """
        return (dedent(msg) + str(self.index)).strip()

    def info(self):
        """Print dataset metadata including information about its batch class and index."""
        print(self)

    @classmethod
    def from_index(cls, index, copy_headers=False, batch_class=SeismicBatch):
        """Construct a dataset from a `SeismicIndex`. Recursively create all subsets defined for the index.

        Parameters
        ----------
        index : SeismicIndex
            Unique identifiers of seismic gathers in the dataset.
        copy_headers : bool, optional, defaults to False
            Whether to copy a `DataFrame` of trace headers of index while constructing the dataset.
        batch_class : type, optional, defaults to SeismicBatch
            A class of batches, generated by a dataset. Must be inherited from :class:`~batchflow.Batch`.

        Returns
        -------
        dataset : SeismicDataset
            Created dataset.
        """
        if copy_headers:
            index = index.copy()
        dataset = cls(index, copy_headers=False, batch_class=batch_class)
        for split_name, split_index in dataset.splits.items():
            setattr(dataset, split_name, cls(split_index, copy_headers=False, batch_class=batch_class))
        return dataset

    def set_index(self, index):
        """Set a new index for the dataset. Recursively update indices for all subsets.

        Parameters
        ----------
        index : SeismicIndex
            A new index to be set.

        Returns
        -------
        dataset : SeismicDataset
            Dataset with updated index.
        """
        self._index = index
        self._iter_params = self.index._iter_params  # pylint: disable=protected-access

        # Recursively process splits
        for split_name in ("train", "test", "validation"):
            index_split = getattr(index, split_name)
            dataset_split = getattr(self, split_name)

            if dataset_split is not None and index_split is not None:
                dataset_split.set_index(index_split)
            elif dataset_split is None and index_split is not None:
                setattr(self, split_name, self.from_index(index_split))
            elif dataset_split is not None and index_split is None:
                setattr(self, split_name, None)
        return self

    def create_subset(self, index):
        """Return a new dataset object based on a subset of its indices given.

        Parameters
        ----------
        index : SeismicIndex or tuple of pd.Index
            Gather indices of the subset to create a new `SeismicDataset` object for. If `tuple` of `pd.Index`, each
            item defines gather indices of the corresponding part in `self`.

        Returns
        -------
        subset : SeismicDataset
            A subset of the dataset.
        """
        if not isinstance(index, self.index_class):
            index = self.index.create_subset(index)
        return type(self)(index, batch_class=self.batch_class)

    @wraps(SeismicIndex.collect_stats)
    def collect_stats(self, n_quantile_traces=100000, quantile_precision=2, limits=None, bar=True):
        _ = self.index.collect_stats(n_quantile_traces=n_quantile_traces, quantile_precision=quantile_precision,
                                     limits=limits, bar=bar)
        return self

    def copy(self, ignore=None):
        return self.from_index(self.index.copy(ignore=ignore))
