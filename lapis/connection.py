import random

from typing import Union, Optional
from usim import Scope, time
from lapis.monitoredpipe import MonitoredPipe


from lapis.cachealgorithm import (
    CacheAlgorithm,
    check_size,
    check_relevance,
    delete_oldest_few_used,
)
from lapis.storageelement import StorageElement, RemoteStorage
from lapis.files import RequestedFile, RequestedFile_HitrateBased
from lapis.monitor import sampling_required
from lapis.monitor.caching import HitrateInfo


class Connection(object):
    """
    Class that manages and triggers file transfers. It contains a mapping of
    sitenames to storages in the `storages` dictionary and a global remote storage.
    It can be used in file based and hitrate based caching mode, however the current
    version is designed for hitrate based caching and the file based caching
    functionality should be tested thoroughly before being activated.

    TODO:: this concept should be abolished, remote storages should be created based
     on configs as normal storages. There should be an additional site class that
     manages the mapping of storages and drones and the connection class should be
     limited to managing and directing file transfers to the correct site, if this is
     even necessary. Furthermore, the mechanics for choosing between caching scenarios
     should be redesigned.
    """

    __slots__ = (
        "storages",
        "remote_connection",
        "caching_algorithm",
        "_filebased_caching",
    )

    def __init__(self, throughput, filebased_caching=True):
        """
        Initialization of the connection object
        :param throughput: throughput of the connection's remote storage
        :param filebased_caching:
        """
        self.storages = dict()
        """dictionary containing storage objects known to the connection module"""
        self.remote_connection = RemoteStorage(throughput=throughput)
        """pipe object representing the connection to a remote storage"""
        self.caching_algorithm = CacheAlgorithm(
            caching_strategy=lambda file, storage: check_size(file, storage)
            and check_relevance(file, storage),
            deletion_strategy=lambda file, storage: delete_oldest_few_used(
                file, storage
            ),
        )
        """cache behavior file based caching, contains both caching and deletion 
        strategy"""
        self._filebased_caching = filebased_caching
        """flag, true if file based caching is current caching mode"""

    async def run_pipemonitoring(self):
        """
        Starts monitoring of pipe objects, should be called during simulator/monitoring
        initialization.
        """
        async def report_load_to_monitoring(pipe: MonitoredPipe):
            async for information in pipe.load():
                await sampling_required.put(information)

        async with Scope() as scope:
            scope.do(report_load_to_monitoring(self.remote_connection.connection))
            for storage_key, storage_list in self.storages.items():
                for storage in storage_list:
                    scope.do(report_load_to_monitoring(storage.connection))

    def add_storage_element(self, storage_element: StorageElement):
        """
        Register storage element in Connection module,  clustering storage elements by
        sitename

        :param storage_element:
        :return:
        """
        storage_element.remote_storage = self.remote_connection
        try:
            self.storages[storage_element.sitename].append(storage_element)
        except KeyError:
            self.storages[storage_element.sitename] = [storage_element]

    async def _determine_inputfile_source(
        self,
        requested_file: RequestedFile,
        dronesite: Optional[str],
        job_repr: Optional[str] = None,
    ) -> Union[StorageElement, RemoteStorage]:
        """
        Collects NamedTuples containing the amount of data of the requested file
        cached in a storage element and the storage element for all reachable storage
        objects on the drone's site. The tuples are sorted by amount of cached data
        and the storage object where the biggest part of the file is cached is
        returned. If the file is not cached in any storage object the connection module
        remote connection is returned.

        :param requested_file:
        :param dronesite:
        :param job_repr:
        :return: pipe that will be used for file transfer
        """
        provided_storages = self.storages.get(dronesite, None)
        if provided_storages is not None:
            look_up_list = []
            for storage in provided_storages:
                look_up_list.append(storage.find(requested_file, job_repr))
            storage_list = sorted(
                [entry for entry in look_up_list], key=lambda x: x[0], reverse=True
            )
            for entry in storage_list:
                # TODO: check should better check that size is bigger than requested
                if entry.cached_filesize > 0:
                    return entry.storage
        return self.remote_connection

    async def stream_file(
        self, requested_file: RequestedFile, dronesite, job_repr=None
    ):
        """
        Determines which storage object is used to provide the requested file and
        starts the files transfer. For files transferred via remote connection a
        potential cache decides whether to cache the file and handles the caching
        process.

        :param requested_file:
        :param dronesite:
        :param job_repr:
        """
        used_connection = await self._determine_inputfile_source(
            requested_file, dronesite, job_repr
        )
        if self._filebased_caching:
            if used_connection == self.remote_connection and self.storages.get(
                dronesite, None
            ):
                try:
                    potential_cache = random.choice(self.storages[dronesite])
                    cache_file, files_for_deletion = self.caching_algorithm.consider(
                        file=requested_file, storage=potential_cache
                    )
                    if cache_file:
                        for file in files_for_deletion:
                            await potential_cache.remove(file, job_repr)
                        await potential_cache.add(requested_file, job_repr)
                    else:
                        print(
                            f"APPLY CACHING DECISION: Job {job_repr}, "
                            f"File {requested_file.filename}: File wasnt "
                            f"cached @ {time.now}"
                        )
                except KeyError:
                    pass
        await used_connection.transfer(requested_file, job_repr=job_repr)

    async def transfer_files(self, drone, requested_files: dict, job_repr):
        """
        Converts dict information about requested files to RequestedFile object and
        sequentially streams all files.

        :param drone:
        :param requested_files:
        :param job_repr:
        :return: time that passed while file was transferred
        """

        start_time = time.now

        # decision if a jobs inputfiles are cached based on hitrate
        random_inputfile_information = next(iter(requested_files.values()))
        if "hitrates" in random_inputfile_information.keys():
            try:
                hitrate = sum(
                    [
                        file["usedsize"] * file["hitrates"].get(drone.sitename, 0.0)
                        for file in requested_files.values()
                    ]
                ) / sum([file["usedsize"] for file in requested_files.values()])
                provides_file = int(random.random() < hitrate)

            except ZeroDivisionError:
                hitrate = 0
                provides_file = 0
        #TODO:: In which cases is hitrate not defined and how can they be covered? I
        # think that in this case this code should not be reached but I'm unsure
        # right now

        await sampling_required.put(
            HitrateInfo(
                hitrate,
                sum([file["usedsize"] for file in requested_files.values()]),
                provides_file,
            )
        )
        job_repr._read_from_cache = provides_file

        for inputfilename, inputfilespecs in requested_files.items():
            if "hitrates" in inputfilespecs.keys():
                requested_file = RequestedFile_HitrateBased(
                    inputfilename, inputfilespecs["usedsize"], provides_file
                )

            else:
                requested_file = RequestedFile(
                    inputfilename, inputfilespecs["usedsize"]
                )
            await self.stream_file(requested_file, drone.sitename, job_repr)
        stream_time = time.now - start_time
        return stream_time
