from typing import Mapping

from ..stdlib import Component, Valve


class ViciValve(Valve):
    """
    A VICI Universal Electric Actuator

    This is used with VICI switching valves.
    Please see: https://www.vici.com/act/ua.php

    Arguments:

    - `serial_port`: Serial port through which device is connected
    - `mapping`: A dict that maps vessels to valve inputs

    """

    metadata = {
        "author": [
            {
                "first_name": "Murat",
                "last_name": "Ozturk",
                "email": "muzcuk@gmail.com",
                "institution": "Indiana University, School of Informatics, Computing and Engineering",
                "github_username": "littleblackfish",
            }
        ],
        "stability": "beta",
        "supported": True,
    }

    def __init__(self, serial_port, mapping: Mapping[Component, int], name=None):
        super().__init__(name=name, mapping=mapping)
        self.serial_port = serial_port

    def __enter__(self):
        import aioserial

        # create the serial connection
        self._ser = aioserial.AioSerial(
            self.serial_port,
            9600,
            parity=aioserial.PARITY_NONE,
            stopbits=1,
            timeout=0.2,
            write_timeout=0.1,
        )

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # close the serial connection
        del self._ser

    def _get_position(self):
        """Returns the position of the valve.

        Note:
            This method was used for introspection and debugging.
            It is preserved but not currently used by any MechWolf function.
            Note that this needs about 200 ms after the last GO command.

        Returns:
            int: The position of the valve.
        """
        self._ser.reset_input_buffer()

        self._ser.write(b"CP\r")
        response = self._ser.readline()

        if response:
            position = int(response[2:4])  # Response is in the form 'CPXX\r'
            return position
        return False

    async def _go(self, position):
        command = f"GO{position}\r"
        await self._ser.write_async(command.encode())

    async def _update(self):
        await self._go(self.setting)
