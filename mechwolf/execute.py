import asyncio
import json
import logging
import time
from collections import namedtuple
from contextlib import ExitStack
from uuid import uuid1

from colorama import Back, Fore, Style, init

import aiohttp

server = "http://localhost:5000"

class Experiment(object):
    '''
        Experiments contain all data from execution of a protocol.
    '''
    def __init__(self,
                 experiment_id,
                 protocol,
                 apparatus,
                 start_time,
                 data,
                 executed_procedures):
        self.experiment_id = experiment_id
        self.protocol = protocol
        self.apparatus = apparatus
        self.start_time = start_time
        self.data = data
        self.executed_procedures = executed_procedures

class DeviceNotFound(Exception):
    '''Raised if a device specified in the protocol is not in the apparatus.'''
    pass

def jupyter_execute (protocol, **kwargs):
    '''
        Executes the specified protocol in a jupyter notebook.

        Args:
            protocol: A protocol of the form mechwolf.Protocol

        Returns:
            mechwolf.Experiment object containing information about the running
            protocol.

        Raises:
            DeviceNotFound: if a device in the protocol is not in the apparatus.
    '''
    #reinitialize objects
    for component in protocol.compile().keys():
        component.done = False
    #Extract the protocol from the Protocol object (or protocol json)
    apparatus = protocol.apparatus
    experiment_id = f'{time.strftime("%Y_%m_%d")}_{uuid1()}'
    start_time = time.time()
    print(f'Experiment {experiment_id} in progress')
    experiment = Experiment(experiment_id,
                  protocol,
                  apparatus,
                  start_time,
                  data = {},
                  executed_procedures = [])

    tasks = asyncio.ensure_future(main(protocol, apparatus, start_time, experiment_id, experiment))
    return experiment

    #try:
    #    logs = asyncio.ensure_future(main(protocol, apparatus, start_time, experiment_id))
    #finally:
    #    for component in protocol.compile().keys():
    #        component.done = False
    #
    # executed_procedures = []
    # data = {}
    # for log in logs:
    #     if log['type'] == 'executed_procedure':
    #         executed_procedures.append(log)
    #     if log['type'] == 'data':
    #         component_name = log['component_name']
    #         data[component_name] = log['data']
    #
    # return Experiment(experiment_id,
    #                   protocol,
    #                   apparatus,
    #                   start_time,
    #                   data,
    #                   executed_procedures,
    #                   logs)


def execute (protocol, delay=5, **kwargs):
    '''
        Executes the specified protocol.
        Starts after the specified delay.

        Args:
            protocol: A protocol of the form mechwolf.Protocol
            apparatus: An apparatus of the form mechwolf.Apparatus
            delay (sec): Number of seconds to delay execution of the protocol.

        Returns:
            mechwolf.Experiment object containing information about the running
            protocol.

        Raises:
            DeviceNotFound: if a device in the protocol is not in the apparatus.
    '''

    #Extract the protocol from the Protocol object (or protocol json)
    apparatus = protocol.apparatus
    experiment_id = f'{time.strftime("%Y_%m_%d")}_{uuid1()}'
    start_time = time.time() + delay
    print(f'Experiment {experiment_id} in progress')

    try:
        logs = asyncio.run(main(protocol, apparatus, start_time, experiment_id))
    finally:
        for component in protocol.compile().keys():
            component.done = False

    executed_procedures = []
    data = {}
    for log in logs:
        if log['type'] == 'executed_procedure':
            executed_procedures.append(log)
        if log['type'] == 'data':
            component_name = log['component_name']
            data[component_name] = log['data']

    return Experiment(experiment_id,
                      protocol,
                      apparatus,
                      start_time,
                      data,
                      executed_procedures)

async def main(protocol, apparatus, start_time, experiment_id, experiment):

    if protocol.__class__.__name__ == 'Protocol':
        p = protocol.compile()
    else:
        raise TypeError('protocol not of type mechwolf.Protocol')
        #TODO allow JSON protocol parsing

    if apparatus.__class__.__name__ != 'Apparatus':
        raise TypeError('apparatus not of type mechwolf.Apparatus')
        #Todo allow parsing of apparatus json

    #Check that all devices in the protocol were passed to the executor.
    for component in p.keys():
        if component not in apparatus.components:
            raise DeviceNotFound(f'Component {component} not in apparatus.')

    protocol_json = protocol.json()

    tasks = []

    # Run protocol
    # Enter context managers for each component (initialize serial ports, etc.)
    # We can do this with contextlib.ExitStack on an arbitrary number of components

    tasks += [log_start(protocol, start_time, experiment_id, experiment)]
    with ExitStack() as stack:
        components = [stack.enter_context(component) for component in p.keys()]
        for component in components:
            # Find out when each component's monitoring should end
            times = [procedure['time'] for procedure in p[component]]
            end_time = max(times).magnitude
            print(end_time)

            tasks += [create_procedure(procedure, component, experiment_id, experiment, end_time)
                      for procedure in p[component]]
            tasks += [monitor(component, end_time, experiment_id, experiment)]

        completed_tasks = await asyncio.gather(*tasks)
        return completed_tasks

async def create_procedure(procedure, component, experiment_id, experiment, end_time):

    execution_time = procedure["time"].to("seconds").magnitude
    await asyncio.sleep(execution_time)
    logging.info(Fore.GREEN + f"Executing: {procedure} on {component} at {time.time()}" + Style.RESET_ALL)
    component.update_from_params(procedure["params"])
    procedure_record = component.update()
    procedure_record['type'] = 'executed_procedure'
    if end_time == execution_time:
        component.done = True

    experiment.executed_procedures.append(procedure_record)
    return procedure_record

async def monitor(component, end_time, experiment_id, experiment):
    device_id=component.name
    experiment.data[device_id] = []
    Datapoint = namedtuple('Datapoint',['datapoint', 'timestamp'])
    async for result in component.monitor():
        datapoint=result['datapoint']
        timestamp=result['timestamp']
        experiment.data[device_id].append(Datapoint(datapoint=datapoint, timestamp=timestamp))

    return {'component_name': component.name, 'data': experiment.data[device_id], 'type': 'data'}

async def log_start(protocol, start_time, experiment_id, experiment):
    return {"experiment_id": experiment_id,
            "experiment_start_time": start_time,
            "type": "experiment_start"}
