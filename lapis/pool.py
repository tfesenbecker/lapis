from typing import Generator, Callable
from cobald import interfaces
from usim import time, eternity, Scope


class Pool(interfaces.Pool):
    """
    A pool encapsulating a number of pools or drones. Given a specific demand, allocation and utilisation, the
    pool is able to adapt in terms of number of drones providing the given resources.

    :param env: Reference to the simulation env
    :param capacity: Maximum number of pools that can be instantiated within the pool
    :param init: Number of pools to instantiate at creation time of the pool
    :param resources: Dictionary of resources available for each pool instantiated within the pool
    """
    def __init__(self, capacity=float('inf'), init=0, name=None, make_drone: Callable=None):
        super(Pool, self).__init__()
        assert make_drone
        self.make_drone = make_drone
        self._drones = []
        self.init_pool(init=init)
        self._demand = 1
        self.name = name or id(self)
        self.level = init
        self._capacity = capacity

    def put(self, amount):
        if self.level + amount > self._capacity:
            raise ValueError
        self.level += amount

    def get(self, amount):
        if self.level - amount < 0:
            raise ValueError
        self.level -= amount

    def init_pool(self, init=0):
        """
        Initialisation of existing drones at creation time of pool.

        :param init: Number of drones to create.
        """
        for _ in range(init):
            self._drones.append(self.make_drone(0))

    # TODO: the run method currently needs to be called manually
    async def run(self):
        """
        Pool periodically checks the current demand and provided drones. If demand is higher than the current level,
        the pool takes care of initialising new drones. Otherwise drones get removed.
        """
        async with Scope() as scope:
            while True:
                drones_required = self._demand - self.level
                while drones_required > 0:
                    drones_required -= 1
                    # start a new drone
                    drone = self.make_drone(10)
                    scope.do(drone.run())
                    self._drones.append(drone)
                    self.put(1)
                if self.level > self._demand:
                    for drone in self.drones:  # only consider drones, that supply resources
                        if drone.jobs == 0:
                            break
                    else:
                        break
                    self.get(1)
                    self._drones.remove(drone)
                    scope.do(drone.shutdown())
                    del drone
                await (time + 1)

    @property
    def drones(self) -> Generator[int, None, None]:
        for drone in self._drones:
            if drone.supply > 0:
                yield drone

    def drone_demand(self) -> int:
        return len(self._drones)

    @property
    def allocation(self) -> float:
        allocations = []
        for drone in self._drones:
            allocations.append(drone.allocation)
        try:
            return sum(allocations) / len(allocations)
        except ZeroDivisionError:
            return 1

    @property
    def utilisation(self) -> float:
        utilisations = []
        for drone in self._drones:
            if drone.allocation > 0:
                utilisations.append(drone.utilisation)
        try:
            return sum(utilisations) / len(utilisations)
        except ZeroDivisionError:
            return 1

    @property
    def supply(self) -> int:
        supply = 0
        for drone in self._drones:
            supply += drone.supply
        return supply

    @property
    def demand(self) -> int:
        return self._demand

    @demand.setter
    def demand(self, value: int):
        if value > 0:
            self._demand = value
        else:
            self._demand = 0


class StaticPool(Pool):
    """
    A static pool does not react on changing conditions regarding demand, allocation and utilisation but instead
    initialises the `capacity` of given drones with initialised `resources`.

    :param capacity: Maximum number of pools that can be instantiated within the pool
    :param resources: Dictionary of resources available for each pool instantiated within the pool
    """
    def __init__(self, capacity=0, make_drone: Callable=None):
        assert capacity > 0, "Static pool was initialised without any resources..."
        super(StaticPool, self).__init__(capacity=capacity, init=capacity, make_drone=make_drone)
        self._demand = capacity

    async def run(self):
        """
        Pool runs forever and does not check if number of drones needs to be adapted.
        """
        while True:
            await eternity