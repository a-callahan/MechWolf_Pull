import asyncio
import time
from collections import namedtuple
from contextlib import ExitStack
from datetime import datetime

from loguru import logger

from .components import Sensor

Datapoint = namedtuple("Datapoint", ["data", "timestamp", "experiment_elapsed_time"])


async def main(experiment, dry_run):

    tasks = []

    logger.info(f"Compiling protocol with dry_run = {dry_run}")
    compiled_protocol = experiment.protocol.compile(dry_run=dry_run)

    # Run protocol
    # Enter context managers for each component (initialize serial ports, etc.)
    # We can do this with contextlib.ExitStack on an arbitrary number of components

    with ExitStack() as stack:
        components = [
            stack.enter_context(component) for component in compiled_protocol.keys()
        ]
        for component in components:
            # Find out when each component's monitoring should end
            end_time = max(
                [procedure["time"] for procedure in compiled_protocol[component]]
            ).magnitude

            logger.debug(f"Calculated {component} end time is {end_time}")

            for procedure in compiled_protocol[component]:
                tasks.append(
                    create_procedure(
                        procedure=procedure,
                        component=component,
                        experiment=experiment,
                        end_time=end_time,
                        dry_run=dry_run,
                    )
                )

            # for sensors, add the monitor task
            if isinstance(component, Sensor):
                logger.debug(f"Creating sensor monitoring task for {component}")
                tasks.append(
                    monitor(component=component, experiment=experiment, dry_run=dry_run)
                )

        experiment.start_time = time.time()

        start_msg = (
            f"{experiment} started at {datetime.fromtimestamp(experiment.start_time)}"
            f" ({experiment.start_time} Unix time)"
        )
        logger.success(start_msg)
        await asyncio.gather(*tasks)

        # when this code block is reached, the tasks will have completed
        experiment.end_time = time.time()
        end_msg = (
            f"{experiment} completed at {datetime.fromtimestamp(experiment.end_time)}"
            f" ({experiment.end_time} Unix time)"
        )
        logger.success(end_msg)


async def create_procedure(procedure, component, experiment, end_time, dry_run):

    # wait for the right moment
    execution_time = procedure["time"].to("seconds").magnitude
    await asyncio.sleep(execution_time)

    component.update_from_params(
        procedure["params"]
    )  # NOTE: this doesn't actually call the update() method

    if dry_run:
        logger.info(
            f"Simulating execution: {procedure} on {component} at {time.time()}"
        )
        procedure_record = {}
        success = True
    else:
        logger.info(f"Executing: {procedure} on {component} at {time.time()}")
        success = component.update()  # NOTE: This does!

    procedure_record = {
        "timestamp": time.time(),
        "params": procedure["params"],
        "type": "executed_procedure" if not dry_run else "simulated_procedure",
        "component": component,
        "success": success,
    }
    procedure_record["experiment_elapsed_time"] = (
        procedure_record["timestamp"] - experiment.start_time
    )

    experiment.executed_procedures.append(procedure_record)


async def monitor(component, experiment, dry_run):
    logger.debug(f"Started monitoring {component.name}")
    async for result in component.monitor(dry_run=dry_run):
        logger.trace(f"Updating experiment with new data from {component.name}")
        try:
            experiment.update(
                device=component.name,
                datapoint=Datapoint(
                    data=result["data"],
                    timestamp=result["timestamp"],
                    experiment_elapsed_time=result["timestamp"] - experiment.start_time,
                ),
            )
            logger.trace("Update successful")
        except Exception as e:
            logger.error(f"Failed to updated experiment! Error message: {str(e)}")
