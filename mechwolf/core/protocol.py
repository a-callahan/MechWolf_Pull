import json
import os
from copy import deepcopy
from datetime import timedelta
from math import isclose
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Union
from warnings import warn

import altair as alt
import pandas as pd
import yaml
from IPython import get_ipython
from IPython.display import Code
from loguru import logger

from .. import _ureg
from ..components import ActiveComponent, TempControl, Valve
from .apparatus import Apparatus
from .experiment import Experiment


class Protocol(object):
    """
    A set of procedures for an apparatus.

    A protocol is defined as a list of procedures, atomic steps for the individual active components of an apparatus.

    ::: tip
    The same `Apparatus` object can create multiple distinct `Protocol` objects.
    :::

    Arguments:
    - `apparatus`: The apparatus for which the protocol is being defined.
    - `name`: The name of the protocol. Defaults to "Protocol_X" where *X* is protocol count.
    - `description`: A longer description of the protocol.

    Attributes:
    - `apparatus`: The apparatus for which the protocol is being defined.
    - `description`: A longer description of the protocol.
    - `is_executing`: Whether the protocol is executing.
    - `name`: The name of the protocol. Defaults to "Protocol_X" where *X* is protocol count.
    - `procedures`: A list of the procedures for the protocol in which each procedure is a dict.
    - `was_executed`: Whether the protocol was executed.
    """

    _id_counter = 0

    def __init__(
        self,
        apparatus: Apparatus,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """See main docstring."""
        # type checking
        if not isinstance(apparatus, Apparatus):
            raise TypeError(
                f"Must pass an Apparatus object. Got {type(apparatus)}, "
                "which is not an instance of mechwolf.Apparatus."
            )

        # ensure apparatus is valid
        if not apparatus._validate():
            raise ValueError("Apparaus is not valid.")

        # store the passed args
        self.apparatus = apparatus
        self.description = description

        # generate the name
        if name is not None:
            self.name = name
        else:
            self.name = "Protocol_" + str(Protocol._id_counter)
            Protocol._id_counter += 1

        # default values
        self.procedures: List[
            Dict[str, Union[float, None, ActiveComponent, Dict[str, Any]]]
        ] = []

    def __repr__(self):
        return f"<{self.__str__()}>"

    def __str__(self):
        return f"Protocol {self.name} defined over {repr(self.apparatus)}"

    def _check_added_valve_mapping(self, valve: Valve, **kwargs) -> dict:
        setting = kwargs["setting"]

        if valve.mapping is None:
            raise ValueError(f"{repr(valve)} does not have a mapping.")

        # the valve itself was given
        if setting in valve.mapping:
            logger.trace(f"{setting} in {repr(valve)}'s mapping.")
            kwargs["setting"] = valve.mapping[setting]

        # the valve's name was given
        # in this case, we get the mapped valve with that name
        # we don't have to worry about duplicate names since that's checked later
        elif setting in [c.name for c in valve.mapping]:
            logger.trace(f"{setting} in {repr(valve)}'s mapping.")
            mapped_component = [c for c in valve.mapping if c.name == setting]
            kwargs["setting"] = valve.mapping[mapped_component[0]]

        # the user gave the actual port mapping number
        elif setting in valve.mapping.values() and isinstance(setting, int):
            logger.trace(f"User supplied manual setting for {valve}")
        else:
            raise ValueError(f"Invalid setting {setting} for {repr(valve)}.")

        return kwargs

    def _check_component_kwargs(self, component: ActiveComponent, **kwargs) -> None:
        """Checks that the given keyword arguments are valid for a component."""
        for kwarg, value in kwargs.items():
            # check that the component even has the attribute
            if not hasattr(component, kwarg):
                # id nor determine valid attrs for the error message
                valid_attrs = [x for x in vars(component).keys()]
                # we don't care about the name attr
                valid_attrs = [x for x in valid_attrs if x != "name"]
                # or internal ones
                valid_attrs = [x for x in valid_attrs if not x.startswith("_")]

                msg = f"Invalid attribute {kwarg} for {component}. "
                msg += f"Valid attributes are {valid_attrs}"
                raise ValueError(msg)

            # for kwargs that will be converted later, just check that the units match
            if isinstance(component.__dict__[kwarg], _ureg.Quantity):
                try:
                    value_dim = _ureg.parse_expression(value).dimensionality
                except AttributeError:
                    value_dim = type(value)
                kwarg_dim = component.__dict__[kwarg].dimensionality

                # perform the check
                if value_dim != kwarg_dim:
                    msg = f"Bad dimensionality of {kwarg} for {component}. "
                    msg += f"Expected {kwarg_dim} but got {value_dim}."
                    raise ValueError(msg)

            # if it's not a quantity, check the types
            elif not isinstance(value, type(component.__dict__[kwarg])):
                expected_type = type(component.__dict__[kwarg])
                msg = "Bad type matching. "
                msg += f"Expected '{kwarg}' to an instance of {expected_type} but got"
                msg += f"{repr(value)}, which is of type {type(value)}."
                raise ValueError(msg)

    def _add_single(
        self, component: ActiveComponent, start=None, stop=None, duration=None, **kwargs
    ) -> None:
        """Adds a single procedure to the protocol.

        See add() for full documentation.
        """

        # make sure that the component being added is part of the apparatus
        self.apparatus[component]

        # don't let users give empty procedures
        if not kwargs:
            raise RuntimeError(
                "No kwargs supplied. "
                "This will not manipulate the state of your sythesizer. "
                "Ensure your call to add() is valid."
            )

        # perform the mapping for valves
        if isinstance(component, Valve):
            kwargs = self._check_added_valve_mapping(component, **kwargs)

        # make sure the component and keywords are valid
        self._check_component_kwargs(component, **kwargs)

        if stop is not None and duration is not None:
            raise RuntimeError("Must provide one of stop and duration, not both.")

        # parse the start time if given
        if isinstance(start, timedelta):
            start = str(start.total_seconds()) + " seconds"
        elif start is None:  # default to the beginning of the protocol
            start = "0 seconds"
        start = _ureg.parse_expression(start)

        # parse duration if given
        if duration is not None:
            if isinstance(duration, timedelta):
                duration = str(duration.total_seconds()) + " seconds"
            stop = start + _ureg.parse_expression(duration)
        elif stop is not None:
            if isinstance(stop, timedelta):
                stop = str(stop.total_seconds()) + " seconds"
            if isinstance(stop, str):
                stop = _ureg.parse_expression(stop)

        if stop is not None and start > stop:
            raise ValueError("Procedure beginning is after procedure end.")

        # a little magic for temperature controllers
        if isinstance(component, TempControl):
            if kwargs.get("temp") is not None and kwargs.get("active") is None:
                kwargs["active"] = True
            elif not kwargs.get("active") and kwargs.get("temp") is None:
                kwargs["temp"] = "0 degC"
            elif kwargs["active"] and kwargs.get("temp") is None:
                raise RuntimeError(
                    f"TempControl {component} is activated but temperature "
                    "setting is not given. Specify 'temp' in your call to add()."
                )

        # add the procedure to the procedure list
        self.procedures.append(
            dict(
                start=float(start.to_base_units().magnitude)
                if start is not None
                else start,
                stop=float(stop.to_base_units().magnitude)
                if stop is not None
                else stop,
                component=component,
                params=kwargs,
            )
        )

    def add(
        self,
        component: Union[ActiveComponent, Iterable[ActiveComponent]],
        start=None,
        stop=None,
        duration=None,
        **kwargs,
    ):
        """
        Adds a procedure to the protocol.

        ::: warning
        If stop and duration are both `None`, the procedure's stop time will be inferred as the end of the protocol.
        :::

        Arguments:
        - `component_added`: The component(s) for which the procedure being added. If an interable, all components will have the same parameters.
        - `start`: The start time of the procedure relative to the start of the protocol, such as `"5 seconds"`. May also be a `datetime.timedelta`. Defaults to `"0 seconds"`, *i.e.* the beginning of the protocol.
        - `stop`: The stop time of the procedure relative to the start of the protocol, such as `"30 seconds"`. May also be a `datetime.timedelta`. May not be given if `duration` is used.
        duration: The duration of the procedure, such as "1 hour". May not be used if `stop` is used.
        - `**kwargs`: The state of the component for the procedure.

        Raises:
        - `TypeError`: A component is not of the correct type (*i.e.* a Component object)
        - `ValueError`: An error occurred when attempting to parse the kwargs.
        - `RuntimeError`: Stop time of procedure is unable to be determined or invalid component.
        """

        if isinstance(component, Iterable):
            for _component in component:
                self._add_single(
                    _component, start=start, stop=stop, duration=duration, **kwargs
                )
        else:
            self._add_single(
                component, start=start, stop=stop, duration=duration, **kwargs
            )

    @property
    def _inferred_duration(self):
        # infer the duration of the protocol
        computed_durations = sorted(
            [x["stop"] for x in self.procedures],
            key=lambda z: z if z is not None else 0,
        )
        if all([x is None for x in computed_durations]):
            raise RuntimeError(
                "Unable to automatically infer duration of protocol. "
                "Must define stop or duration for at least one procedure"
            )
        return computed_durations[-1]

    def _compile(
        self, dry_run: bool = True, _visualization: bool = False
    ) -> Dict[ActiveComponent, List[Dict[str, Union[float, str, Dict[str, Any]]]]]:
        """
        Compile the protocol into a dict of devices and their procedures.

        Returns:
        - A dict with components as the values and lists of their procedures as the value.
        The elements of the list of procedures are dicts with two keys: "time" in seconds, and "params", whose value is a dict of parameters for the procedure.

        Raises:
        - `RuntimeError`: When compilation fails.
        """
        output = {}

        # deal only with compiling active components
        for component in self.apparatus[ActiveComponent]:
            # determine the procedures for each component
            component_procedures: List[MutableMapping] = sorted(
                [x for x in self.procedures if x["component"] == component],
                key=lambda x: x["start"],
            )

            # skip compiling components without procedures
            if not len(component_procedures):
                warn(
                    f"{component} is an active component but was not used in this procedure."
                    " If this is intentional, ignore this warning."
                )
                continue

            # validate each component
            try:
                component._validate(dry_run=dry_run)
            except Exception as e:
                raise RuntimeError(f"{component} isn't valid. Got error: '{str(e)}'.")

            # check for conflicting continuous procedures
            if (
                len(
                    [
                        x
                        for x in component_procedures
                        if x["start"] is None and x["stop"] is None
                    ]
                )
                > 1
            ):
                raise RuntimeError(
                    f"{component} cannot have two procedures for the entire duration of the protocol. "
                    "If each procedure defines a different attribute to be set for the entire duration, "
                    "combine them into one call to add(). Otherwise, reduce ambiguity by defining start "
                    "and stop times for each procedure. "
                    ""
                )

            for i, procedure in enumerate(component_procedures):
                # automatically infer start and stop times
                try:
                    # the start time of the next procedure
                    next_start = component_procedures[i + 1]["start"]
                    if next_start == 0:
                        raise RuntimeError(
                            f"Ambiguous start time for {procedure['component']}. "
                        )
                    elif next_start is not None and procedure["stop"] is None:
                        warn(
                            f"Automatically inferring stop time for {procedure['component']} "
                            f"as beginning of {procedure['component']}'s next procedure."
                        )
                        procedure["stop"] = next_start

                    # check for overlapping procedures
                    elif next_start < procedure["stop"] and not isclose(
                        next_start, procedure["stop"]
                    ):
                        msg = "Cannot have two overlapping procedures. "
                        msg += f"{procedure} and {component_procedures[i + 1]} conflict"
                        raise RuntimeError(msg)

                except IndexError:
                    if procedure["stop"] is None:
                        warn(
                            f"Automatically inferring stop for {procedure['component']} as the end of the protocol. "
                            f"To override, provide stop in your call to add()."
                        )
                        procedure["stop"] = self._inferred_duration

            # give the component instructions at all times
            compiled = []
            for i, procedure in enumerate(component_procedures):
                if _visualization:
                    compiled.append(
                        dict(
                            start=procedure["start"],
                            stop=procedure["stop"],
                            params=procedure["params"],
                        )
                    )
                else:
                    compiled.append(
                        dict(time=procedure["start"], params=procedure["params"])
                    )

                    # if the procedure is over at the same time as the next
                    # procedure begins, don't go back to the base state
                    try:
                        if isclose(
                            component_procedures[i + 1]["start"], procedure["stop"]
                        ):
                            continue
                    except IndexError:
                        pass

                    # otherwise, go back to base state
                    new_state = {
                        "time": procedure["stop"],
                        "params": component._base_state,
                    }
                    compiled.append(new_state)

            output[component] = compiled

            # raise warning if duration is explicitly given but not used?
        return output

    def to_dict(self):
        compiled = deepcopy(self._compile(dry_run=True))
        compiled = {k.name: v for (k, v) in compiled.items()}
        return compiled

    def to_list(self):
        output = []
        for procedure in deepcopy(self.procedures):
            procedure["component"] = procedure["component"].name
            output.append(procedure)
        return output

    def yaml(self) -> Union[str, Code]:
        """
        Outputs the uncompiled procedures to YAML.

        Internally, this is a conversion of the output of `Protocol.json` for the purpose of enhanced human readability.

        Returns:
        - YAML of the procedure list.
        When in Jupyter, this string is wrapped in an `IPython.display.Code` object for nice syntax highlighting.

        """
        compiled_yaml = yaml.safe_dump(self.to_list(), default_flow_style=False)

        if get_ipython():
            return Code(compiled_yaml, language="yaml")
        return compiled_yaml

    def json(self) -> Union[str, Code]:
        """
        Outputs the uncompiled procedures to JSON.

        Returns:
        - JSON of the protocol.
          When in Jupyter, this string is wrapped in a `IPython.display.Code` object for nice syntax highlighting.
        """
        compiled_json = json.dumps(self.to_list(), sort_keys=True, indent=4)

        if get_ipython():
            return Code(compiled_json, language="json")
        return compiled_json

    def visualize(self, legend: bool = False, width=500, renderer: str = "notebook"):
        """
        Generates a Gantt plot visualization of the protocol.

        Arguments:
        - `legend`: Whether to show a legend.
        - `renderer`: Which renderer to use. Defaults to "notebook" but can also be "jupyterlab", or "nteract", depending on the development environment. If not in a Jupyter Notebook, this argument is ignored.
        - `width`: The width of the Gantt chart.

        Returns:
        - An interactive visualization of the protocol.
        """

        # don't try to render a visualization to the notebook if we're not in one
        if get_ipython():
            alt.renderers.enable(renderer)

        for component, procedures in self._compile(_visualization=True).items():
            # generate a dict that will be a row in the dataframe
            for procedure in procedures:
                procedure["component"] = str(component)
                procedure["start"] = pd.Timestamp(procedure["start"], unit="s")
                procedure["stop"] = pd.Timestamp(procedure["stop"], unit="s")

                # hoist the params to the main dict
                assert isinstance(procedure["params"], dict)  # needed for typing
                for k, v in procedure["params"].items():
                    procedure[k] = v

                # show what the valve is actually connecting to
                if isinstance(component, Valve) and type(procedure["setting"]) == int:
                    assert isinstance(component.mapping, Mapping)
                    # guess the component, c, which the valve is set to
                    mapped_component = [
                        repr(k)
                        for k, v in component.mapping.items()
                        if v == procedure["setting"]
                    ][0]
                    procedure["mapped component"] = mapped_component
                # TODO: make this deterministic for color coordination
                procedure["params"] = json.dumps(procedure["params"])

            # prettyify the tooltips
            tooltips = [
                alt.Tooltip("utchoursminutesseconds(start):T", title="start (h:m:s)"),
                alt.Tooltip("utchoursminutesseconds(stop):T", title="stop (h:m:s)"),
                "component",
            ]

            # just add the params to the tooltip
            tooltips.extend(
                [
                    x
                    for x in procedures[0].keys()
                    if x not in ["component", "start", "stop", "params"]
                ]
            )

            # generate the component's graph
            source = pd.DataFrame(procedures)
            component_chart = (
                alt.Chart(source, width=width)
                .mark_bar()
                .encode(
                    x="utchoursminutesseconds(start):T",
                    x2="utchoursminutesseconds(stop):T",
                    y="component",
                    color=alt.Color("params:N", legend=None)
                    if not legend
                    else "params",
                    tooltip=tooltips,
                )
            )

            # label the axes
            component_chart.encoding.x.title = "Experiment Elapsed Time (h:m:s)"
            component_chart.encoding.y.title = "Component"

            # combine with the other charts
            try:
                chart += component_chart  # type: ignore
            except NameError:
                chart = component_chart

        return chart.interactive()

    def execute(
        self,
        dry_run: Union[bool, int] = False,
        verbosity: str = "info",
        confirm: bool = False,
        strict: bool = True,
        log_file: Union[str, bool, os.PathLike, None] = True,
        log_file_verbosity: Optional[str] = "trace",
        log_file_compression: Optional[str] = None,
        data_file: Union[str, bool, os.PathLike, None] = True,
    ) -> Experiment:
        """
        Executes the procedure.

        Arguments:
        - `confirm`: Whether to bypass the manual confirmation message before execution.
        - `dry_run`: Whether to simulate the experiment or actually perform it. Defaults to `False`, which means executing the protocol on real hardware. If an integer greater than zero, the dry run will execute at that many times speed.
        - `strict`: Whether to stop execution upon encountering any errors. If False, errors will be noted but ignored.
        - `verbosity`: The level of logging verbosity. One of "critical", "error", "warning", "success", "info", "debug", or "trace" in descending order of severity. "debug" and (especially) "trace" are not meant to be used regularly, as they generate significant amounts of usually useless information. However, these verbosity levels are useful for tracing where exactly a bug was generated, especially if no error message was thrown.
        - `log_file`: The file to write the logs to during execution. If `True`, the data will be written to a file in `~/.mechwolf` with the filename `{experiment_id}.log.jsonl`. If falsey, no logs will be written to the file.
        - `log_file_verbosity`: How verbose the logs in file should be. By default, it is "trace", which is the most verbose logging available. If `None`, it will use the same level as `verbosity`.
        - `log_file_compression`: Whether to compress the log file after the experiment.
        - `data_file`: The file to write the experimental data to during execution. If `True`, the data will be written to a file in `~/.mechwolf` with the filename `{experiment_id}.data.jsonl`. If falsey, no data will be written to the file.

        Returns:
        - An `Experiment` object. In a Jupyter notebook, the object yields an interactive visualization. If protocol execution fails for any reason that does not raise an error, the return type is None.

        Raises:
        - `RuntimeError`: When attempting to execute a protocol on invalid components.
        """

        # the Experiment object is going to hold all the info
        E = Experiment(self)
        E._execute(
            dry_run=dry_run,
            verbosity=verbosity,
            confirm=confirm,
            strict=strict,
            log_file=log_file,
            log_file_verbosity=log_file_verbosity,
            log_file_compression=log_file_compression,
            data_file=data_file,
        )

        return E
