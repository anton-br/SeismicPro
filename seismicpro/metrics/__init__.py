"""Classes for metrics calculation, accumulation and visualization"""

from .metric import Metric, is_metric, initialize_metrics
from .pipeline_metric import PipelineMetric, pass_coords, pass_batch, pass_calc_args, define_pipeline_metric
from .metric_map import MetricMap, ScatterMap, BinarizedMap
from .interactive_map import MetricMapPlot, ScatterMapPlot, BinarizedMapPlot
