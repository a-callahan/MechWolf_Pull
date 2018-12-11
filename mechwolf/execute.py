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
                 executed_procedures,
                 logs):
        self.experiment_id = experiment_id
        self.protocol = protocol
        self.apparatus = apparatus
        self.start_time = start_time
        self.logs = logs
        self.data = data
        self.executed_procedures = executed_procedures

class DeviceNotFound(Exception):
    '''Raised if a device specified in the protocol is not in the apparatus.'''
    pass

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
                      executed_procedures,
                      logs)

async def main(protocol, apparatus, start_time, experiment_id):

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
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=None)) as session:
        tasks += [log_start(protocol, start_time, experiment_id, session)]
        with ExitStack() as stack:
            components = [stack.enter_context(component) for component in p.keys()]
            for component in components:
                # Find out when each component's monitoring should end
                times = [procedure['time'] for procedure in p[component]]
                end_time = max(times).magnitude
                print(end_time)

                tasks += [create_procedure(procedure, component, experiment_id, session, end_time)
                          for procedure in p[component]]
                tasks += [monitor(component, end_time, experiment_id, session)]

            completed_tasks = await asyncio.gather(*tasks)
            return completed_tasks

async def create_procedure(procedure, component, experiment_id, session, end_time):

    execution_time = procedure["time"].to("seconds").magnitude
    await asyncio.sleep(execution_time)
    logging.info(Fore.GREEN + f"Executing: {procedure} on {component} at {time.time()}" + Style.RESET_ALL)
    component.update_from_params(procedure["params"])
    procedure_record = component.update()
    procedure_record['type'] = 'executed_procedure'
    logging.debug(f"logging procedure {procedure} to hub")
    if end_time == execution_time:
        component.done = True

    await log_procedure(procedure_record, experiment_id, session)
    return procedure_record

async def monitor(component, end_time, experiment_id, session):
    data = []
    Datapoint = namedtuple('Datapoint',['datapoint', 'timestamp'])
    async for result in component.monitor():
        datapoint=result['datapoint']
        device_id=component.name
        timestamp=result['timestamp']
        data.append(Datapoint(datapoint=datapoint, timestamp=timestamp))

        logging.debug(f"Logging results {datapoint} from {device_id} to hub")
        await log_data(datapoint, timestamp, device_id, experiment_id, session)

    return {'component_name': component.name, 'data': data, 'type': 'data'}

async def log_start(protocol, start_time, experiment_id, session):
    try:
        async with session.post(f"{server}/log_start", json=json.dumps({"protocol": protocol.dict(),
                                                                        "protocol_start_time": start_time,
                                                                        "experiment_id": experiment_id})) as resp:
            await resp.text()
    except (aiohttp.client_exceptions.ClientConnectorError, aiohttp.client_exceptions.ClientOSError):
        print('Could not connect')
    except (aiohttp.client_exceptions.ServerDisconnectedError):
        print('Hub disconnected')
    return {"experiment_id": experiment_id,
            "experiment_start_time": start_time,
            "type": "experiment_start"}

async def log_procedure(procedure_record, experiment_id, session):
    try:
        async with session.post(f"{server}/log_procedure", json=json.dumps({"procedure": procedure_record, "experiment_id": experiment_id})) as resp:
            await resp.text()
    except (aiohttp.client_exceptions.ClientConnectorError, aiohttp.client_exceptions.ClientOSError):
        print('Could not connect')
    except (aiohttp.client_exceptions.ServerDisconnectedError):
        print('Hub disconnected')

async def log_data(datapoint, timestamp, device_id, experiment_id, session):
    try:
        async with session.post(f"{server}/log_data", json=json.dumps({"datapoint": datapoint,
                                                                       "experiment_id": experiment_id,
                                                                       "device_id": device_id,
                                                                       "timestamp": timestamp})) as resp:
            await resp.text()
    except (aiohttp.client_exceptions.ClientConnectorError, aiohttp.client_exceptions.ClientOSError):
        print('Could not connect')
