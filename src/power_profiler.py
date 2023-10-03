import time
import csv
import datetime
import logging
from threading import Event, RLock, Thread
from ppk2_api.ppk2_api import PPK2_MP as PPK2_API

logger = logging.getLogger(__name__)

class PowerProfiler():
    def __init__(self, serial_port=None, source_voltage_mV=3300, filename=None, source_meter=True, fetch_interval_s=0.1):
        """Initialize PPK2 power profiler with serial.
        
        Keyword arguments:
        serial_port -- the name of the serial port use to access the PPK2. If None, the port is detected automatically.
        source_voltage_mV -- the output voltage in mV the source meter is set to initially.
        filename -- the path to a CSV file. If not None, this file will be created and the collected data is written to it whenever stop_measurement() is called.
        source_meter -- if set to True, then the device is operated in source meter mode. If set to false, ampere meter mode is used.
        fetch_interval -- the number of seconds betweem fetching data from the measurement process. The measurement process keeps up to 10 seconds of data, so it is not necessarry to use a very low value here.
        """
        self.measuring = None
        self.measurement_thread = None
        # measure_lock is taken by the measurement thread whenever it is fetching data and by the main thread whenever measuring is paused
        # it is also used to protect the member variables that are accessed both by the main thread and the measurement thread
        self.measure_lock = RLock()
        self.measure_lock.acquire()

        self.fetch_interval = fetch_interval_s

        # stop is a flag which is used to signal to the 
        self.stop = Event()
        self.ppk2 = None

        logger.debug("Initing power profiler")

        # try:
        if serial_port:
            self.ppk2 = PPK2_API(serial_port)
        else:
            serial_port = self.discover_port()
            logger.debug("Opening serial port: %s", serial_port)
            if serial_port:
                self.ppk2 = PPK2_API(serial_port)

        try:
            ret = self.ppk2.get_modifiers()  # try to read modifiers, if it fails serial port is probably not correct
            logger.debug("Initialized ppk2 api: %s", ret)
        except Exception as e:
            logger.debug("Error initializing power profiler: %s", e)
            ret = None
            raise e

        if not ret:
            self.ppk2 = None
            raise Exception(f"Error when initing PowerProfiler with serial port {serial_port}")
        else:
            if source_meter:
                self.ppk2.use_source_meter()
            else:
                self.ppk2.use_ampere_meter()

            self.source_voltage_mV = source_voltage_mV

            self.ppk2.set_source_voltage(self.source_voltage_mV)  # set to 3.3V

            logger.debug("Set power profiler source voltage: %s mV", self.source_voltage_mV)

            self.measuring = False
            self.current_measurements = []

            # local variables used to calculate power consumption
            self.measurement_start_time = None
            self.measurement_stop_time = None

            time.sleep(1)

            self.measurement_thread = Thread(target=self.measurement_loop, daemon=True)
            self.measurement_thread.start()

            # write to csv
            self.filename = filename
            if self.filename is not None:
                with open(self.filename, 'w', newline='') as file:
                    writer = csv.writer(file)
                    row = []
                    for key in ["ts", "avg1000"]:
                        row.append(key)
                    writer.writerow(row)

    def write_csv_rows(self, samples):
        """Write csv row"""
        with open(self.filename, 'a', newline='') as file:
            writer = csv.writer(file)
            for sample in samples:
                row = [datetime.datetime.now().strftime('%d-%m-%Y %H:%M:%S.%f'), sample]
                writer.writerow(row)

    def delete_power_profiler(self):
        """Join thread"""
        self.stop.set()
        self.stop_measuring()

        logger.debug("Deleting power profiler")

        if self.measurement_thread:
            logger.debug("Joining measurement thread")
            self.measurement_thread.join()
            self.measurement_thread = None

        if self.ppk2:
            logger.debug("Disabling ppk2 power")
            self.disable_power()
            del self.ppk2

        logger.debug("Deleted power profiler")

    def discover_port(self):
        """Discovers ppk2 serial port"""
        ppk2s_connected = PPK2_API.list_devices()
        if(len(ppk2s_connected) == 1):
            ppk2_port = ppk2s_connected[0]
            logger.debug('Found PPK2 at %s', ppk2_port)
            return ppk2_port
        else:
            logger("Too many connected PPK2s: %s", ppk2s_connected)
            return None

    def enable_power(self):
        """Enable ppk2 power"""
        with self.measure_lock:
            if self.ppk2:
                self.ppk2.toggle_DUT_power("ON")
                return True
            return False

    def disable_power(self):
        """Disable ppk2 power"""
        with self.measure_lock:
            if self.ppk2:
                self.ppk2.toggle_DUT_power("OFF")
                return True
            return False

    def use_source_meter(self):
        """Switch to source meter mode"""
        with self.measure_lock:
            if self.ppk2:
                self.ppk2.use_source_meter()
                return True
            return False

    def use_ampere_meter(self):
        """Switch to source meter mode"""
        with self.measure_lock:
            if self.ppk2:
                self.ppk2.use_ampere_meter()
                return True
            return False

    def measurement_loop(self):
        """Endless measurement loop will run in a thread"""
        while True and not self.stop.is_set():
            with self.measure_lock:
                # read data if currently measuring
                read_data = self.ppk2.get_data()
                if read_data != b'':
                    samples = self.ppk2.get_samples(read_data)
                    self.current_measurements += samples  # can easily sum lists, will append individual data
            time.sleep(self.fetch_interval)

    def _average_samples(self, list, window_size):
        """Average samples based on window size"""
        chunks = [list[val:val + window_size] for val in range(0, len(list), window_size)]
        avgs = []
        for chunk in chunks:
            avgs.append(sum(chunk) / len(chunk))

        return avgs

    def start_measuring(self):
        """Start measuring"""
        with self.measure_lock:  
            if not self.measuring:  # toggle measuring flag only if currently not measuring
                self.current_measurements = []  # reset current measurements
                self.measure_lock.release()
                self.measuring = True  # set internal flag
                self.ppk2.start_measuring()  # send command to ppk2
                self.measurement_start_time = time.time()

    def stop_measuring(self):
        """Stop measuring and return average of period"""
        with self.measure_lock:
            self.measuring = False
            self.measure_lock.acquire()
            self.measurement_stop_time = time.time()
            self.ppk2.stop_measuring()  # send command to ppk2

            #samples_average = self._average_samples(self.current_measurements, 1000)
            if self.filename is not None:
                self.write_csv_rows(self.current_measurements)

    def get_min_current_mA(self):
        with self.measure_lock:
            return min(self.current_measurements) / 1000

    def get_max_current_mA(self):
        with self.measure_lock:
            return max(self.current_measurements) / 1000

    def get_num_measurements(self):
        with self.measure_lock:
            return len(self.current_measurements)

    def get_average_current_mA(self):
        """Returns average current of last measurement in mA"""
        with self.measure_lock:
            if len(self.current_measurements) == 0:
                return 0

            average_current_mA = (sum(self.current_measurements) / len(self.current_measurements)) / 1000 # measurements are in microamperes, divide by 1000
            return average_current_mA

    def get_average_power_consumption_mWh(self):
        """Return average power consumption of last measurement in mWh"""
        with self.measure_lock:
            average_current_mA = self.get_average_current_mA()
            average_power_mW = (self.source_voltage_mV / 1000) * average_current_mA  # divide by 1000 as source voltage is in millivolts - this gives us milliwatts
            measurement_duration_h = self.get_measurement_duration_s() / 3600  # duration in seconds, divide by 3600 to get hours
            average_consumption_mWh = average_power_mW * measurement_duration_h
            return average_consumption_mWh

    def get_average_charge_mC(self):
        """Returns average charge in milli coulomb"""
        with self.measure_lock:
            average_current_mA = self.get_average_current_mA()
            measurement_duration_s = self.get_measurement_duration_s()  # in seconds
            return average_current_mA * measurement_duration_s

    def get_measurement_duration_s(self):
        """Returns duration of measurement"""
        measurement_duration_s = (self.measurement_stop_time - self.measurement_start_time)  # measurement duration in seconds
        return measurement_duration_s