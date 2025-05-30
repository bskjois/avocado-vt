"""
Virtualization test - cgroup related utility functions for libvirt

:copyright: 2019 Red Hat Inc.
"""

import logging
import os
import re

from avocado.utils import process

from virttest import utils_disk, virsh
from virttest.staging import utils_cgroup

VIRSH_BLKIOTUNE_OUTPUT_MAPPING = {
    "weight": "weight",
    "device_read_iops_sec": "riops",
    "device_write_iops_sec": "wiops",
    "device_read_bytes_sec": "rbps",
    "device_write_bytes_sec": "wbps",
}
CGROUP_V1_BLKIO_FILE_MAPPING = {
    "weight": "blkio.bfq.weight",
    "wiops": "blkio.throttle.write_iops_device",
    "riops": "blkio.throttle.read_iops_device",
    "rbps": "blkio.throttle.read_bps_device",
    "wbps": "blkio.throttle.write_bps_device",
}
CGROUP_V2_BLKIO_FILE_MAPPING = {
    "weight": "io.bfq.weight",
    "wiops": "io.max",
    "riops": "io.max",
    "rbps": "io.max",
    "wbps": "io.max",
}
CGROUP_V1_MEM_FILE_MAPPING = {
    "hard_limit": "memory.limit_in_bytes",
    "soft_limit": "memory.soft_limit_in_bytes",
    "swap_hard_limit": "memory.memsw.limit_in_bytes",
}
CGROUP_V2_MEM_FILE_MAPPING = {
    "hard_limit": "memory.max",
    "soft_limit": "memory.high",
    "swap_hard_limit": "memory.swap.max",
}
CGROUP_V1_SCHEDINFO_FILE_MAPPING = {
    "cpu_shares": "cpu.shares",
    "vcpu_period": "<vcpuX>/cpu.cfs_period_us",
    "vcpu_quota": "<vcpuX>/cpu.cfs_quota_us",
    "emulator_period": "emulator/cpu.cfs_period_us",
    "emulator_quota": "emulator/cpu.cfs_quota_us",
    "global_period": "cpu.cfs_period_us",
    "global_quota": "cpu.cfs_quota_us",
    "iothread_period": "<iothreadX>/cpu.cfs_period_us",
    "iothread_quota": "<iothreadX>/cpu.cfs_quota_us",
}
CGROUP_V2_SCHEDINFO_FILE_MAPPING = {
    "cpu_shares": "cpu.weight",
    "vcpu_period": "<vcpuX>/cpu.max",
    "vcpu_quota": "<vcpuX>/cpu.max",
    "emulator_period": "emulator/cpu.max",
    "emulator_quota": "emulator/cpu.max",
    "global_period": "cpu.max",
    "global_quota": "cpu.max",
    "iothread_period": "<iothreadX>/cpu.max",
    "iothread_quota": "<iothreadX>/cpu.max",
}

LOG = logging.getLogger("avocado." + __name__)


# cgroup related functions
class CgroupTest(object):
    """Class for libvirt cgroup related test"""

    __vm_pid = ""

    def __init__(self, vm_pid):
        self.__vm_pid = vm_pid

    def is_cgroup_v2_enabled(self):
        """
        Check if cgroup v2 enabled on host

        :return: True means cgroup v2 enabled
                 False means not
        """
        cmd = "cat /proc/mounts | grep cgroup2"
        cmd_result = process.run(cmd, ignore_status=True, shell=True)
        if cmd_result.exit_status:
            return False
        return True

    def get_cgroup_path(self, controller=None):
        """
        Get specific cgroup controller's root path

        :params controller: The cgroup controller, used for cgroup v1. For
                            cgroup v2 this param will be ignored since all
                            controllers are in the same dir
        :return: The path to the cgroup controller
        """
        cgroup_path = ""
        if self.is_cgroup_v2_enabled():
            vm_proc_cgroup_path = "/proc/%s/cgroup" % self.__vm_pid
            with open("/proc/mounts", "r") as mnts:
                cg_mount_point = re.findall(r"\s(\S*cgroup)\s", mnts.read())
            with open(vm_proc_cgroup_path, "r") as vm_cg_file:
                cg_vm_scope = re.findall(r"\S*::(\S*)", vm_cg_file.read())
            cgroup_path = os.path.join(cg_mount_point[0], cg_vm_scope[0].strip("/"))
            if "emulator" in cgroup_path:
                cgroup_path += "/.."
        else:
            cgroup_path = utils_cgroup.resolve_task_cgroup_path(
                int(self.__vm_pid), controller
            )
        if not os.path.exists(cgroup_path):
            LOG.error("cgroup path '%s' doesn't exist" % cgroup_path)
            return None
        return cgroup_path

    def __get_cpu_subdirs(self, controller_path=None, dir_keyword=None):
        """
        Search and return the sub dirs of the cpu controllers by keyword

        :param controller_path: The path of the cpu controller
        :param dir_keyword: The keyword of the sub dirs. Normally it could be
                            "vcpu", "emulator", "iothread"
        :return: The list of the sub dir names
        """
        dir_names = []
        for filename in os.listdir(controller_path):
            if dir_keyword in filename:
                dir_names.append(filename)
        if not dir_names and "iothread" in dir_keyword:
            LOG.debug(
                "No sub dirs found with keyword: '%s'. "
                "Pls check if you've executed virsh cmd "
                "'iothreadadd'.",
                dir_keyword,
            )
            return None
        return sorted(dir_names)

    def __get_standardized_cgroup1_info(self, virsh_cmd=None):
        """
        Get the cgroup info on a cgroupv1 enabled system, and standardize it to
        a dict

        :param virsh_cmd: The virsh cmd used. This is to judge which cgroup
                          info to get. Such as, when virsh cmd is 'blkiotune',
                          the blkio related cgroup info will be returned
        :return: A dict containing the cgroup info
        """

        def __get_cg_file_path(cg_key, cg_path, cg_file_name):
            """
            Get the path to a cgroup param file

            :param cg_key: The cgroup param name used in libvirt
            :param cgroup_path: The cgroup root path
            :param cg_file_name: The file name of the cgroup param
            :return: The path to the cgroup param file
            """
            if cg_key in ["weight", "cpu_shares"] and "libvirt" in cg_path:
                return os.path.join(cg_path.split("libvirt")[0], cg_file_name)
            return os.path.join(cg_path, cg_file_name)

        standardized_cgroup_info = {}
        if virsh_cmd == "blkiotune":
            cgroup_path = self.get_cgroup_path("blkio")
            dev_init_dict = {
                "rbps": "max",
                "wbps": "max",
                "riops": "max",
                "wiops": "max",
            }
            dev_list = []
            if not self.is_bfq():
                CGROUP_V1_BLKIO_FILE_MAPPING["weight"] = "blkio.weight"
                CGROUP_V1_BLKIO_FILE_MAPPING["weight_device"] = "blkio.weight_device"
            for cg_key, cg_file_name in list(CGROUP_V1_BLKIO_FILE_MAPPING.items()):
                cg_file_path = __get_cg_file_path(cg_key, cgroup_path, cg_file_name)
                with open(cg_file_path, "r") as cg_file:
                    if cg_key in ["weight"]:
                        standardized_cgroup_info[cg_key] = cg_file.read().strip()
                    if cg_key in ["rbps", "wbps", "riops", "wiops", "weight_device"]:
                        for line in cg_file.readlines():
                            dev_info = line.strip().split()
                            dev_num = dev_info[0]
                            dev_cg_value = dev_info[1]
                            if dev_num not in dev_list:
                                standardized_cgroup_info[dev_num] = dev_init_dict.copy()
                            standardized_cgroup_info[dev_num][cg_key] = dev_cg_value
                            dev_list.append(dev_num)
        elif virsh_cmd == "memtune":
            cgroup_path = self.get_cgroup_path("memory")
            cmd = "getconf PAGE_SIZE"
            page_size = process.run(
                cmd, ignore_status=True, shell=True
            ).stdout_text.strip()
            LOG.debug("page_size is %d" % int(page_size))
            if int(page_size) == 65536:
                max_mem_value = "9223372036854710272"
            else:
                max_mem_value = "9223372036854771712"
            LOG.debug("max_mem_value is %s" % max_mem_value)
            for cg_key, cg_file_name in list(CGROUP_V1_MEM_FILE_MAPPING.items()):
                with open(os.path.join(cgroup_path, cg_file_name), "r") as cg_file:
                    cg_file_value = cg_file.read().strip()
                    if cg_file_value == max_mem_value:
                        cg_file_value = "max"
                    standardized_cgroup_info[cg_key] = cg_file_value
        elif virsh_cmd == "schedinfo":
            cgroup_path = self.get_cgroup_path("cpu,cpuacct") + "/.."
            max_cpu_value = "-1"
            vcpu_dirs = self.__get_cpu_subdirs(cgroup_path, "vcpu")
            iothread_dirs = self.__get_cpu_subdirs(cgroup_path, "iothread")
            for cg_key, cg_file_name in list(CGROUP_V1_SCHEDINFO_FILE_MAPPING.items()):
                if "<vcpuX>" in cg_file_name:
                    cg_file_name = cg_file_name.replace("<vcpuX>", vcpu_dirs[0])
                if "<iothreadX>" in cg_file_name:
                    if iothread_dirs:
                        cg_file_name = cg_file_name.replace(
                            "<iothreadX>", iothread_dirs[0]
                        )
                    else:
                        continue
                cg_file_path = __get_cg_file_path(cg_key, cgroup_path, cg_file_name)
                with open(cg_file_path, "r") as cg_file:
                    cg_file_value = cg_file.read().strip()
                    if cg_file_value == max_cpu_value:
                        cg_file_value = "max"
                    if "quota" in cg_key:
                        if cg_file_value in [
                            "-1",
                            "18446744073709551",
                            "17592186044415",
                        ]:
                            standardized_cgroup_info[cg_key] = "max"
                            continue
                    standardized_cgroup_info[cg_key] = cg_file_value
        else:
            LOG.error("You've provided a wrong virsh cmd: %s", virsh_cmd)
        return standardized_cgroup_info

    def __get_standardized_cgroup2_info(self, virsh_cmd=None):
        """
        Get the cgroup info on a cgroupv2 enabled system, and standardize it to
        a dict

        :param virsh_cmd: The virsh cmd used. This is to judge which cgroup
                          info to get. Such as, when virsh cmd is 'blkiotune',
                          the blkio related cgroup info will be returned
        :return: A dict containing the cgroup info
        """
        standardized_cgroup_info = {}
        cgroup_path = self.get_cgroup_path()
        if virsh_cmd == "blkiotune":
            weight_file_name = CGROUP_V2_BLKIO_FILE_MAPPING["weight"]
            iomax_file_name = CGROUP_V2_BLKIO_FILE_MAPPING["wiops"]
            path_to_weight = os.path.join(
                cgroup_path.split("libvirt")[0], weight_file_name
            )
            with open(path_to_weight, "r") as weight_file:
                weight_value = re.search(r"\d+", weight_file.read())
                if weight_value:
                    weight_value = weight_value.group()
                standardized_cgroup_info["weight"] = weight_value
            path_to_iomax = os.path.join(cgroup_path, iomax_file_name)
            with open(path_to_iomax, "r") as iomax_file:
                iomax_info = iomax_file.readlines()
                for line in iomax_info:
                    dev_iomax_info = line.strip().split()
                    dev_iomax_dict = {}
                    dev_num = dev_iomax_info[0]
                    for i in range(1, len(dev_iomax_info)):
                        key, value = dev_iomax_info[i].split("=")
                        dev_iomax_dict[key] = value
                    standardized_cgroup_info[dev_num] = dev_iomax_dict
        elif virsh_cmd == "memtune":
            for cg_key, cg_file_name in list(CGROUP_V2_MEM_FILE_MAPPING.items()):
                with open(os.path.join(cgroup_path, cg_file_name), "r") as cg_file:
                    cg_file_value = cg_file.read().strip()
                    standardized_cgroup_info[cg_key] = cg_file_value
        elif virsh_cmd == "schedinfo":
            vcpu_dirs = self.__get_cpu_subdirs(cgroup_path, "vcpu")
            iothread_dirs = self.__get_cpu_subdirs(cgroup_path, "iothread")
            for cg_key, cg_file_name in list(CGROUP_V2_SCHEDINFO_FILE_MAPPING.items()):
                if "<vcpuX>" in cg_file_name:
                    cg_file_name = cg_file_name.replace("<vcpuX>", vcpu_dirs[0])
                if "<iothreadX>" in cg_file_name:
                    if iothread_dirs:
                        cg_file_name = cg_file_name.replace(
                            "<iothreadX>", iothread_dirs[0]
                        )
                    else:
                        continue
                cg_dir = cgroup_path
                if cg_key == "cpu_shares":
                    cg_dir = cgroup_path.split("libvirt")[0]
                with open(os.path.join(cg_dir, cg_file_name), "r") as cg_file:
                    list_index = 0
                    cg_file_values = cg_file.read().strip().split()
                    if "period" in cg_key:
                        list_index = 1
                    standardized_cgroup_info[cg_key] = cg_file_values[list_index]
        else:
            LOG.error("You've provided a wrong virsh cmd: %s", virsh_cmd)
        return standardized_cgroup_info

    def get_cgroup_file_mapping(self, virsh_cmd):
        """
        Get the cgroup file mapping

        :param virsh_cmd: The virsh cmd used. This is to judge which cgroup
                          info to get
        :return: A dict of file mapping
        """
        if self.is_cgroup_v2_enabled():
            if virsh_cmd == "memtune":
                return CGROUP_V2_MEM_FILE_MAPPING
            elif virsh_cmd == "blkiotune":
                return CGROUP_V2_BLKIO_FILE_MAPPING
            elif virsh_cmd == "schedinfo":
                return CGROUP_V2_SCHEDINFO_FILE_MAPPING
        else:
            if virsh_cmd == "memtune":
                return CGROUP_V1_MEM_FILE_MAPPING
            elif virsh_cmd == "blkiotune":
                return CGROUP_V1_BLKIO_FILE_MAPPING
            elif virsh_cmd == "schedinfo":
                return CGROUP_V1_SCHEDINFO_FILE_MAPPING

    def get_standardized_cgroup_info(self, virsh_cmd=None):
        """
        Get the cgroup info and standardize it to a dict

        :param virsh_cmd: The virsh cmd used. This is to judge which cgroup
                          info to get. Such as, when virsh cmd is 'blkiotune',
                          the blkio related cgroup info will be returned
        :return: A dict containing the cgroup info
        """
        if self.is_cgroup_v2_enabled():
            return self.__get_standardized_cgroup2_info(virsh_cmd)
        else:
            return self.__get_standardized_cgroup1_info(virsh_cmd)

    def get_virsh_output_dict(self, vm_name=None, virsh_cmd=None):
        """
        Get the virsh cmd output as a dict

        :param vm_name: Name of the vm
        :param virsh_cmd: Name of the virsh cmd

        :return: The virsh cmd output, as a dict
        """
        if virsh_cmd == "memtune":
            func = virsh.memtune_list
        elif virsh_cmd == "blkiotune":
            func = virsh.blkiotune
        elif virsh_cmd == "schedinfo":
            func = virsh.schedinfo
        else:
            LOG.error("There is no virsh cmd '%s'", virsh_cmd)
            return None
        result = func(vm_name, ignore_status=True)
        return self.convert_virsh_output_to_dict(result)

    def convert_virsh_output_to_dict(self, result):
        """
        Convert virsh command output to a dict.
        This method is only applicable to the virsh command
        with ':' in the output for now.

        :param result: Virsh command's result.
            For example, the result of virsh schedinfo, blkiotune, memtune.

        :return: The virsh cmd output, as a dict.
        """
        output = result.stdout_text.strip()
        output_list = output.splitlines()
        output_dict = {}
        for output_line in output_list:
            output_info = output_line.split(":")
            output_param = output_info[0].strip()
            if len(output_info) == 1:
                output_value = ""
            else:
                output_value = output_info[1].strip()
            output_dict[output_param] = output_value
        return output_dict

    def __get_dev_major_minor(self, dev_path="/dev/sda"):
        """
        Get the device 'major:minor' number

        :param dev_path: The path to the device
        """
        if not os.path.exists(dev_path):
            LOG.debug("device '%s' not existing", dev_path)
            return None
        dev = os.stat(dev_path)
        return "%s:%s" % (os.major(dev.st_rdev), os.minor(dev.st_rdev))

    def get_standardized_virsh_info(self, virsh_cmd=None, virsh_dict=None):
        """
        Get and standardize the info of cgroup related virsh cmd's output

        :param virsh_cmd: The virsh cmd used
        :param virsh_dict: The dict containing the virsh cmd output
        :return: Standardized info of the virsh cmd output, as a dict
        """
        standardized_virsh_output_info = {}
        if virsh_cmd == "blkiotune":
            virsh_output_mapping = VIRSH_BLKIOTUNE_OUTPUT_MAPPING.copy()
            dev_list = []
            dev_init_dict = {
                "rbps": "max",
                "wbps": "max",
                "riops": "max",
                "wiops": "max",
            }
            for io_item, io_item_value in list(virsh_dict.items()):
                if io_item in ["weight"]:
                    standardized_virsh_output_info[io_item] = io_item_value
                elif io_item in list(virsh_output_mapping.keys()) and io_item_value:
                    io_value_list = io_item_value.split(",")
                    for i in range(len(io_value_list)):
                        if "dev" in io_value_list[i]:
                            dev_num = self.__get_dev_major_minor(io_value_list[i])
                            if dev_num not in dev_list:
                                standardized_virsh_output_info[dev_num] = (
                                    dev_init_dict.copy()
                                )
                                dev_list.append(dev_num)
                            standardized_virsh_output_info[dev_num][
                                virsh_output_mapping[io_item]
                            ] = io_value_list[i + 1]
        elif virsh_cmd == "memtune":
            standardized_virsh_output_info = {
                "hard_limit": "max",
                "soft_limit": "max",
                "swap_hard_limit": "max",
            }
            for mem_item, mem_item_value in list(virsh_dict.items()):
                if mem_item_value in ["unlimited"]:
                    standardized_virsh_output_info[mem_item] = "max"
                elif mem_item_value.isdigit():
                    standardized_virsh_output_info[mem_item] = str(
                        int(mem_item_value) * 1024
                    )
                else:
                    standardized_virsh_output_info[mem_item] = mem_item_value
                    LOG.debug(
                        "memtune: the value '%s' for '%s' is " "new to us, pls check.",
                        mem_item_value,
                        mem_item,
                    )
        elif virsh_cmd == "schedinfo":
            for schedinfo_item, schedinfo_value in list(virsh_dict.items()):
                if schedinfo_item.lower() in ["scheduler"]:
                    # no need to check scheduler type, it's fixed for qemu
                    continue
                if "quota" in schedinfo_item:
                    if (
                        schedinfo_value in ["18446744073709551", "17592186044415"]
                        or int(schedinfo_value) < 0
                    ):
                        # When set cfs_quota values with negative values or
                        # maximum acceptable values, it's means 'max' or
                        # 'unlimited', so match these values to 'max'.
                        standardized_virsh_output_info[schedinfo_item] = "max"
                        continue
                standardized_virsh_output_info[schedinfo_item] = schedinfo_value
        else:
            LOG.error("You've provided an unsupported virsh cmd: %s", virsh_cmd)
            return None
        return standardized_virsh_output_info

    def is_bfq(self):
        """
        Judge which scheduler is used, 'bfq' or 'cfq'

        :return: bool, True if bfq is used, False if cfq is used
        """

        first_blk = utils_disk.get_first_disk()
        schedulerfd = "/sys/block/%s/queue/scheduler" % first_blk
        bfq_scheduler = False
        with open(schedulerfd, "r") as iosche:
            if "bfq" in iosche.readline():
                bfq_scheduler = True
        return bfq_scheduler

    def get_standardized_virsh_output_by_name(self, vm_name=None, virsh_cmd=None):
        """
        Get the standardized output of a vm with a certain virsh cmd

        :param vm_name: The name of the vm
        :param virsh_cmd: The virsh cmd to get the output
        :return: The standardized dict of the virsh cmd output
        """
        virsh_output_dict = self.get_virsh_output_dict(vm_name, virsh_cmd)
        return self.get_standardized_virsh_info(virsh_cmd, virsh_output_dict)

    def _produce_cpuset_cpus_file(self, cpuset_path, vm_name, is_cgroup2=False):
        """
        The utility function is to help produce cpuset.cpus file on cgroup v1

        :param cpuset_path: str, the file path of cpuset.cpus
        :param vm_name: vm name
        :param is_cgroup2: boolean, True if cgroup v2, otherwise, False
        """
        if is_cgroup2:
            LOG.debug("cpuset.cpus is not used in cgroup v2")
            return

        if not os.path.exists(cpuset_path):
            # Produce the cpuset.cpus file by starting the vm
            virsh_dargs = {"ignore_status": False, "debug": True}
            virsh.start(vm_name, **virsh_dargs)
            virsh.destroy(vm_name, **virsh_dargs)

    def set_cpuset_cpus(self, value, vm_name):
        """
        Set the cpuset.cpus file to the specified content

        :param value: to be set
        :param vm_name: the vm name
        """
        if self.is_cgroup_v2_enabled():
            LOG.debug("v2 cgroup doesn't have cpuset.cpus")
            return
        cpuset_path = "/sys/fs/cgroup/cpuset/machine.slice/cpuset.cpus"
        self._produce_cpuset_cpus_file(cpuset_path, vm_name)
        LOG.debug("Set %s to %s", cpuset_path, value)
        cmd = "echo %s > %s" % (value, cpuset_path)
        process.run(cmd, ignore_status=False, shell=True)

    def get_cpuset_cpus(self, vm_name):
        """
        Get the cpuset.cpus file content

        :param vm_name: the vm name
        :return: str, the value of cpuset.cpus content
        """
        if self.is_cgroup_v2_enabled():
            LOG.debug("v2 cgroup doesn't have cpuset.cpus")
            return
        cpuset_path = "/sys/fs/cgroup/cpuset/machine.slice/cpuset.cpus"
        self._produce_cpuset_cpus_file(cpuset_path, vm_name)
        LOG.debug("Get %s value", cpuset_path)
        cmd = "cat %s" % cpuset_path
        return process.run(cmd, ignore_status=False, shell=True).stdout_text.strip()
