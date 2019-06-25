import asyncio
import time
from math import sin
from warnings import warn

from . import ureg
from .component import ActiveComponent


class Sensor(ActiveComponent):
    """A generic sensor.

    Note:
        Users should not directly instantiate an :class:`Sensor` for use in a :class:`~mechwolf.Protocol` becuase
        it is not an actual laboratory instrument.

    Attributes:
        name (str, optional): The name of the Sensor.
        rate (Quantity): Data collection rate in Hz. A rate of 0 Hz corresponds to the sensor being off.
    """

    def __init__(self, name):
        super().__init__(name=name)
        self.rate = ureg.parse_expression("0 Hz")
        self._visualization_shape = "ellipse"
        self.done = False

    def base_state(self):
        """Default to being inactive."""
        return dict(rate="0 Hz")

    def read(self):
        """Collect the data."""
        raise NotImplementedError

    def update(self):
        return {
            "timestamp": time.time(),
            "params": {"rate": str(self.rate.to_base_units())},
            "device": self.name,
        }

    async def monitor(self, dry_run=False):
        """If data collection is off and needs to be turned on, turn it on.
           If data collection is on and needs to be turned off, turn off and return data."""
        while True:
            if self.done:
                break
            frequency = self.rate.to_base_units().magnitude
            if frequency != 0:
                if not dry_run:
                    yield {"datapoint": self.read(), "timestamp": time.time()}
                else:
                    yield {"datapoint": "simulated read", "timestamp": time.time()}
                await asyncio.sleep(1 / frequency)
            else:
                await asyncio.sleep(frequency)

    def validate(self, dry_run):
        if not dry_run:
            try:
                res = self.read()
            except NotImplementedError:
                warn("Sensors must have a read method that returns the sensor's data")
                return False

            if not res:
                warn(
                    "Sensor reads should probably return data. "
                    f"Currently, {self}.read() does not return anything."
                )

        return super().validate(dry_run=dry_run)


class DummySensor(Sensor):
    """A dummy sensor returning the number of times it has been read.

    Warning:
        Don't use this in a real apparatus! It doesn't return real data.

    Attributes:
        name (str, optional): The name of the Sensor.
        rate (Quantity): Data collection rate in Hz. A rate of 0 Hz corresponds to the sensor being off.
    """

    def __init__(self, name):
        super().__init__(name=name)
        self.counter = 0

    def read(self):
        """Collect the data."""
        self.counter += 1
        return self.counter * sin(self.counter * 0.314)
