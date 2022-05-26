"""Test Gather loading methods"""

# pylint: disable=redefined-outer-name
import pytest
import numpy as np

from seismicpro import Survey, make_prestack_segy


@pytest.fixture(scope="module", params=[  # Data type of trace amplitudes and maximum absolute value
    [1, 0.1],
    [1, 1],
    [1, 100],
    [1, 100000000],
    [5, 0.1],
    [5, 1],
    [5, 100],
    [5, 100000000],
])
def load_segy(tmp_path_factory, request):
    """Return a path to a SEG-Y file and its trace data to test data loading."""
    n_traces = 16
    n_samples = 20
    segy_fmt, max_amplitude = request.param
    trace_data = np.random.uniform(-max_amplitude, max_amplitude, size=(n_traces, n_samples)).astype(np.float32)

    def gen_trace(TRACE_SEQUENCE_FILE, **kwargs):  # pylint: disable=invalid-name
        """Return a corresponding trace from pregenerated data."""
        _ = kwargs
        return trace_data[TRACE_SEQUENCE_FILE - 1]

    path = tmp_path_factory.mktemp("load") / "load.sgy"
    make_prestack_segy(path, fmt=segy_fmt, survey_size=(4, 4), origin=(0, 0), sources_step=(3, 3),
                       receivers_step=(1, 1), bin_size=(1, 1), activation_dist=(1, 1), n_samples=n_samples,
                       sample_rate=2000, delay=0, bar=False, trace_gen=gen_trace)
    return path, trace_data


class TestLoad:
    """Test `Gather` loading methods."""

    @pytest.mark.parametrize("init_limits", [slice(None), slice(10)])
    @pytest.mark.parametrize("load_limits", [None, slice(None, None, 2), slice(-5, None)])
    @pytest.mark.parametrize("use_segyio_trace_loader", [True, False])
    @pytest.mark.parametrize("traces_pos", [
        [0],  # Single trace
        [1, 5, 3, 2],  # Multiple traces
        [6, 6, 10, 10, 10],  # Duplicated traces
        np.arange(16),  # All traces
    ])
    def test_load_traces(self, load_segy, init_limits, load_limits, use_segyio_trace_loader, traces_pos):
        """Compare loaded traces with the actual ones."""
        path, trace_data = load_segy
        survey = Survey(path, header_index="TRACE_SEQUENCE_FILE", limits=init_limits,
                        use_segyio_trace_loader=use_segyio_trace_loader, bar=False)

        # load_limits take priority over init_limits
        limits = init_limits if load_limits is None else load_limits
        trace_data = trace_data[traces_pos, limits]
        loaded_data = survey.load_traces(traces_pos, limits=load_limits)
        assert np.allclose(loaded_data, trace_data)

    @pytest.mark.parametrize("init_limits", [slice(None), slice(5)])
    @pytest.mark.parametrize("load_limits", [None, slice(2, 15, 5)])
    @pytest.mark.parametrize("traces_pos", [
        [5],  # Single trace
        [1, 2, 3],  # Multiple traces
        [8, 8, 8],  # Duplicated traces
        np.arange(16),  # All traces
    ])
    def test_load_gather(self, load_segy, init_limits, load_limits, traces_pos):
        """Test gather loading by its headers."""
        path, trace_data = load_segy
        survey = Survey(path, header_index="FieldRecord", limits=init_limits, bar=False)

        gather_headers = survey.headers.iloc[traces_pos]
        gather = survey.load_gather(gather_headers, limits=load_limits)

        # load_limits take priority over init_limits
        limits = init_limits if load_limits is None else load_limits
        gather_data = trace_data[traces_pos, limits]

        assert gather.headers.equals(gather_headers)
        assert np.allclose(gather.data, gather_data)
        assert np.allclose(gather.samples, survey.file_samples[limits])
