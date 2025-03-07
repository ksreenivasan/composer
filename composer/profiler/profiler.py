# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""Composer Profiler."""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple, Union

from composer.core import Callback
from composer.loggers import Logger
from composer.profiler.json_trace_handler import JSONTraceHandler
from composer.profiler.marker import Marker
from composer.profiler.profiler_action import ProfilerAction
from composer.profiler.system_profiler import SystemProfiler
from composer.profiler.torch_profiler import TorchProfiler
from composer.profiler.trace_handler import TraceHandler
from composer.utils import ensure_tuple, parse_uri

if TYPE_CHECKING:
    from composer.core import State

__all__ = ['Profiler']

log = logging.getLogger(__name__)


class Profiler(Callback):
    """Composer Profiler.

    See the :doc:`Profiling Guide </trainer/performance_tutorials/profiling>` for additional information.

    Args:
        schedule ((State) -> ProfilerAction): The profiling scheduling function.

            It takes the training state and returns a :class:`.ProfilerAction`.
            For convenience, Composer includes a :meth:`~composer.profiler.cyclic_schedule.cyclic_schedule` helper.

            .. testsetup::

                from composer.profiler import Profiler, cyclic_schedule

                original_profiler_init = Profiler.__init__

                def new_profiler_init(self, dummy_ellipsis=None, **kwargs):
                    if 'trace_handlers' not in kwargs:
                        kwargs['trace_handlers'] = []
                    kwargs['torch_prof_memory_filename'] = None
                    original_profiler_init(self, **kwargs)

                Profiler.__init__ = new_profiler_init

            .. testcode::

                from composer.profiler import Profiler, cyclic_schedule

                profiler = Profiler(
                    ...,
                    schedule=cyclic_schedule(
                        skip_first=1,
                        wait=0,
                        warmup=1,
                        active=4,
                        repeat=1,
                    ),
                    torch_prof_memory_filename=None,
                )

        trace_handlers (TraceHandler | Sequence[TraceHandler]): Trace handlers which record and
            save profiling data to traces. Additionally supports full object store paths.
        sys_prof_cpu (bool, optional): Whether to record cpu statistics. (default: ``True``).
        sys_prof_memory (bool, optional): Whether to record memory statistics. (default: ``False``).
        sys_prof_disk (bool, optional): Whether to record disk statistics. (default: ``False``).
        sys_prof_net (bool, optional): Whether to record network statistics. (default: ``False``).
        sys_prof_stats_thread_interval_seconds (float, optional): Interval to record stats, in seconds.
            (default: ``0.5``).
        torch_prof_folder (str, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_filename (str, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_remote_file_name (str, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
            Additionally supports full object store paths e.g: s3://bucket/path/to/file.
        torch_prof_memory_filename (str, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_memory_remote_file_name (str, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
            Additionally supports full object store paths e.g: s3://bucket/path/to/file.
        torch_prof_overwrite (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_use_gzip (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_record_shapes (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_profile_memory (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_with_stack (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_with_flops (bool, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
        torch_prof_num_traces_to_keep (int, optional): See :class:`~composer.profiler.torch_profiler.TorchProfiler`.
    """

    def __init__(
        self,
        schedule: Callable[[State], ProfilerAction],
        trace_handlers: List[TraceHandler],
        sys_prof_cpu: bool = True,
        sys_prof_memory: bool = False,
        sys_prof_disk: bool = False,
        sys_prof_net: bool = False,
        sys_prof_stats_thread_interval_seconds: float = 0.5,
        torch_prof_folder: str = '{run_name}/torch_traces',
        torch_prof_filename: str = 'rank{rank}.{batch}.pt.trace.json',
        torch_prof_remote_file_name: Optional[str] = '{run_name}/torch_traces/rank{rank}.{batch}.pt.trace.json',
        torch_prof_memory_filename: Optional[str] = None,
        torch_prof_memory_remote_file_name: Optional[
            str] = '{run_name}/torch_memory_traces/rank{rank}.{batch}.pt.memory_trace.html',
        torch_prof_overwrite: bool = False,
        torch_prof_use_gzip: bool = False,
        torch_prof_record_shapes: bool = False,
        torch_prof_profile_memory: bool = True,
        torch_prof_with_stack: bool = False,
        torch_prof_with_flops: bool = True,
        torch_prof_num_traces_to_keep: int = -1,
    ) -> None:
        self._names_to_markers: Dict[str, Marker] = {}
        self._trace_handlers = list(ensure_tuple(trace_handlers))
        self.schedule = schedule
        self.state = None
        self._callbacks: List[Callback] = []
        # Used to count skip_first starting from resumption timestamp
        self.resumption_batch_idx: int = 0
        self.remote_filenames: List[str] = []
        # First, add each remote file name to self.remote_filenames to create RemoteUploaderDownloader logger in trainer. [s3://bucket/path/to/file]
        # Then modify remote file name to be a local path to pass into torch_profiler and system_profiler. e.g: path/to/file
        if torch_prof_remote_file_name:
            self.remote_filenames.append(torch_prof_remote_file_name)
            _, _, torch_prof_remote_file_name = parse_uri(torch_prof_remote_file_name)
        if torch_prof_memory_remote_file_name:
            self.remote_filenames.append(torch_prof_memory_remote_file_name)
            _, _, torch_prof_memory_remote_file_name = parse_uri(torch_prof_memory_remote_file_name)
        for handler in self._trace_handlers:
            if isinstance(handler, JSONTraceHandler):
                if handler.remote_file_name:
                    self.remote_filenames.append(handler.remote_file_name)
                    _, _, handler.remote_file_name = parse_uri(handler.remote_file_name)

                if handler.merged_trace_remote_file_name:
                    self.remote_filenames.append(handler.merged_trace_remote_file_name)
                    _, _, handler.merged_trace_remote_file_name = parse_uri(handler.merged_trace_remote_file_name)

        if sys_prof_cpu or sys_prof_memory or sys_prof_disk or sys_prof_net:
            self._callbacks.append(
                SystemProfiler(profile_cpu=sys_prof_cpu,
                               profile_memory=sys_prof_memory,
                               profile_disk=sys_prof_disk,
                               profile_net=sys_prof_net,
                               stats_thread_interval_seconds=sys_prof_stats_thread_interval_seconds))

        if torch_prof_memory_filename is not None:
            if not (torch_prof_with_stack and torch_prof_record_shapes and torch_prof_profile_memory):
                raise ValueError(
                    f'torch_prof_memory_filename is set. Generating the memory timeline graph requires all the three flags torch_prof_with_stack, torch_prof_record_shapes, and torch_prof_profile_memory to be true. Got torch_prof_with_stack={torch_prof_with_stack}, torch_prof_record_shapes={torch_prof_record_shapes}, torch_prof_profile_memory={torch_prof_profile_memory}'
                )
            log.info(
                f'Memory profiling is enabled and uses {torch_prof_memory_filename} as the filename to generate the memory timeline graph. To disable the memory timeline graph generation, explicitly set torch_prof_memory_filename to None.'
            )
        else:
            log.info(f'torch_prof_memory_filename is set to None. Memory timeline will not be be generated.')

        if torch_prof_record_shapes or torch_prof_profile_memory or torch_prof_with_stack or torch_prof_with_flops:
            self._callbacks.append(
                TorchProfiler(filename=torch_prof_filename,
                              folder=torch_prof_folder,
                              remote_file_name=torch_prof_remote_file_name,
                              memory_filename=torch_prof_memory_filename,
                              memory_remote_file_name=torch_prof_memory_remote_file_name,
                              num_traces_to_keep=torch_prof_num_traces_to_keep,
                              overwrite=torch_prof_overwrite,
                              record_shapes=torch_prof_record_shapes,
                              profile_memory=torch_prof_profile_memory,
                              use_gzip=torch_prof_use_gzip,
                              with_stack=torch_prof_with_stack,
                              with_flops=torch_prof_with_flops))

    def bind_to_state(
        self,
        state: State,
    ):
        """Bind the profiler to the ``state``.

        .. note::

            The :class:`.Trainer` automatically invokes this method.

        Args:
            state (State): The training state.
        """
        self.state = state
        self.state.callbacks.append(self)
        self.state.callbacks.extend(self._callbacks)
        self.state.callbacks.extend(self._trace_handlers)

    @property
    def trace_handlers(self):
        """Profiler trace handlers."""
        return self._trace_handlers

    @trace_handlers.setter
    def trace_handlers(self, trace_handlers: Union[TraceHandler, Sequence[TraceHandler]]):
        """Profiler trace handlers."""
        self._trace_handlers[:] = ensure_tuple(trace_handlers)

    def record_chrome_json_trace_file(self, filepath: Union[str, pathlib.Path]):
        """Record trace events in Chrome JSON format in the trace handlers.

        See `this document <https://docs.google.com/document/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU/preview>`_
        for more information about Chrome JSON format.

        .. note::

            For custom profiling, it is recommended to use :meth:`marker` instead of manually creating a Chrome JSON
            trace file. By default, the Composer Profiler will automatically saving :class:`.Marker` events in Chrome
            JSON format.

            This method exists for external profilers that natively record events in Chrome JSON format (such as the
            :class:`~composer.profiler.torch_profiler.TorchProfiler`). These profilers can use this method to route
            their profiling traces to the Composer profiler :attr:`~trace_handlers` so events from both the Composer
            Profiler and external profilers are recorded in the same trace file.
        """
        for recorder in self.trace_handlers:
            recorder.process_chrome_json_trace_file(pathlib.Path(filepath))

    def marker(
            self,
            name: str,
            actions: Sequence[ProfilerAction] = (ProfilerAction.WARMUP, ProfilerAction.ACTIVE,
                                                 ProfilerAction.ACTIVE_AND_SAVE),
            record_instant_on_start: bool = False,
            record_instant_on_finish: bool = False,
            categories: Union[List[str], Tuple[str, ...]] = (),
    ) -> Marker:
        """Create and get an instance of a :class:`.Marker`.

        If a :class:`.Marker` with the specified ``name`` does not already exist, it will be created.
        Otherwise, the existing instance will be returned.

        .. note::

            :meth:`.Profiler.marker()` should be used to construct markers.  :class:`.Marker` **should not** be
            instantiated directly by the user.

            For example:

            .. testsetup:: composer.profiler.profiler.Profiler.marker

                from composer.profiler import Profiler, cyclic_schedule

                profiler = Profiler(schedule=cyclic_schedule(), trace_handlers=[], torch_prof_memory_filename=None)
                profiler.bind_to_state(state)
                state.profiler = profiler

            .. doctest:: composer.profiler.profiler.Profiler.marker

                >>> marker = profiler.marker("foo")
                >>> marker
                <composer.profiler.marker.Marker object at ...>

        Please see :meth:`.Marker.start()` and :meth:`.Marker.finish()` for usage on creating markers to measure duration events,
        :meth:`.Marker.instant()` for usage on creating markers to mark instant events and :meth:`.Marker.counter()` for usage on
        creating markers for counting.

        Args:
            name (str): The name for the :class:`.Marker`.
            actions (Sequence[ProfilerAction], optional): :class:`.ProfilerAction` states to record on.
                Defaults to (:attr:`~.ProfilerAction.WARMUP`, :attr:`~.ProfilerAction.ACTIVE`,
                :attr:`~.ProfilerAction.ACTIVE_AND_SAVE`).
            record_instant_on_start (bool, optional): Whether to record an instant event whenever the marker is started.
                Defaults to ``False``.
            record_instant_on_finish (bool, optional): Whether to record an instant event whenever the marker is finished.
                Defaults to ``False``.
            categories (Union[List[str], Tuple[str, ...]], optional): Categories for this marker. Defaults to ``None``.

        Returns:
            Marker: Marker instance.
        """
        if self.state is None:
            raise RuntimeError('Profiler.bind_to_state() must be invoked before the Profiler can be used.')
        if name not in self._names_to_markers:

            def should_record(state: State) -> bool:
                return self.schedule(state) in actions

            self._names_to_markers[name] = Marker(
                state=self.state,
                trace_handlers=self.trace_handlers,
                name=name,
                should_record=should_record,
                record_instant_on_start=record_instant_on_start,
                record_instant_on_finish=record_instant_on_finish,
                categories=categories,
            )
        self._names_to_markers[name].categories = categories
        return self._names_to_markers[name]

    def after_load(self, state: State, logger: Logger) -> None:
        del logger
        self.resumption_batch_idx = int(state.timestamp.batch_in_epoch)
