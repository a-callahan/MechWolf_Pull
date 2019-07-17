import asyncio
import time
from collections import namedtuple
from contextlib import ExitStack
from datetime import datetime
from typing import Union

from loguru import logger

from ..components import Component, Sensor
from .experiment import Experiment

Datapoint = namedtuple("Datapoint", ["data", "timestamp", "experiment_elapsed_time"])


async def main(experiment: Experiment, dry_run: Union[bool, int], strict: bool):
    """
    The function that actually does the execution of the protocol.

    Args:
    - `experiment`: The experiment to execute.
    - `dry_run`: Whether to simulate the experiment or actually perform it. If an integer greater than zero, the dry run will execute at that many times speed.
    - `strict`: Whether to stop execution upon any errors.
    """

    tasks = []

    # Run protocol
    # Enter context managers for each component (initialize serial ports, etc.)
    # We can do this with contextlib.ExitStack on an arbitrary number of components
    try:
        with ExitStack() as stack:
            if not dry_run:
                components = [
                    stack.enter_context(component)
                    for component in experiment.compiled_protocol.keys()
                ]
            else:
                components = experiment.compiled_protocol.keys()
            for component in components:
                # Find out when each component's monitoring should end
                end_time = max(
                    [
                        procedure["time"]
                        for procedure in experiment.compiled_protocol[component]
                    ]
                )

                logger.debug(f"Calculated {component} end time is {end_time}s")

                for procedure in experiment.compiled_protocol[component]:
                    tasks.append(
                        wait_and_execute_procedure(
                            procedure=procedure,
                            component=component,
                            experiment=experiment,
                            dry_run=dry_run,
                            strict=strict,
                        )
                    )

                # for sensors, add the monitor task
                if isinstance(component, Sensor):
                    logger.debug(f"Creating sensor monitoring task for {component}")
                    tasks.append(
                        monitor(
                            component=component, experiment=experiment, dry_run=dry_run
                        )
                    )
                    tasks.append(end_monitoring(component, end_time, dry_run))

            # Add a reminder about FF
            if type(dry_run) == int:
                logger.info(f"Simulating at {dry_run}x speed...")

            # begin the experiment
            experiment.start_time = time.time()
            start_msg = f"{experiment} started at {datetime.utcfromtimestamp(experiment.start_time)} UTC"
            logger.success(start_msg)
            try:
                await asyncio.gather(*tasks)

                # when this code block is reached, the tasks will have completed
                experiment.end_time = time.time()
                end_msg = f"{experiment} completed at {datetime.utcfromtimestamp(experiment.end_time)} UTC"
                logger.success(end_msg)
            except RuntimeError:
                logger.critical("Protocol execution is stopping NOW!")
            except:  # noqa
                logger.exception("Failed to execute protocol due to uncaught error!")
    finally:
        logger.trace("Cleaning up...")

        # allow sensors to start monitoring again
        for component in experiment.compiled_protocol.keys():
            if isinstance(component, Sensor):
                logger.trace(f"Re enabling {component} monitoring...")
                component._stop = False
        experiment.protocol.is_executing = False
        experiment.protocol.was_executed = True

        if experiment._bound_logger is not None:
            logger.trace("Deactivating logging to Jupyter notebook widget...")
            logger.remove(experiment._bound_logger)


async def wait_and_execute_procedure(
    procedure,
    component: Component,
    experiment: Experiment,
    dry_run: Union[bool, int],
    strict: bool,
):

    # wait for the right moment
    execution_time = procedure["time"]
    if type(dry_run) == int:
        await asyncio.sleep(execution_time / dry_run)
    else:
        await asyncio.sleep(execution_time)

    component.update_from_params(
        procedure["params"]
    )  # NOTE: this doesn't actually call the update() method

    if dry_run:
        logger.info(
            f"Simulating: {procedure['params']} on {component}"
            f" at {procedure['time']}s"
        )
        record = {}
        success = True
    else:
        logger.info(
            f"Executing: {procedure['params']} on {component}"
            f" at {procedure['time']}s"
        )
        success = component.update()  # NOTE: This does!

    if not success:
        logger.error(f"Failed to update {component}!")
        if strict:
            raise RuntimeError("Failure!")

    record = {
        "timestamp": time.time(),
        "params": procedure["params"],
        "type": "executed_procedure" if not dry_run else "simulated_procedure",
        "component": component,
        "success": success,
    }
    record["experiment_elapsed_time"] = record["timestamp"] - experiment.start_time

    experiment.executed_procedures.append(record)


async def monitor(component, experiment, dry_run):
    logger.debug(f"Started monitoring {component.name}")
    try:
        async for result in component.monitor(dry_run=dry_run):
            experiment.update(
                device=component.name,
                datapoint=Datapoint(
                    data=result["data"],
                    timestamp=result["timestamp"],
                    experiment_elapsed_time=result["timestamp"] - experiment.start_time,
                ),
            )
    except RuntimeError as e:
        logger.error(f"Failed to read {component}!")
        raise e


async def end_monitoring(component, end_time: float, dry_run: bool):
    """Creates a new async task that ends the monitoring for a `components.sensor.Sensor` when it is done for the protocol.


        component (`components.sensor.Sensor`): A `components.sensor.Sensor` to end monitoring for.
        end_time (float): The end time for the sensor in EET.
    """
    if type(dry_run) == int:
        await asyncio.sleep(end_time / dry_run)
    else:
        await asyncio.sleep(end_time)
    logger.debug(f"Setting {component}._stop to True in order to stop monitoring")
    component._stop = True
