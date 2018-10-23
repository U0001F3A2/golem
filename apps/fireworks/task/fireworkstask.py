import copy
import os
import logging
import yaml
import queue

from fireworks import LaunchPad, ScriptTask, Workflow
from typing import List, Optional

import golem_messages
from apps.core.task.coretaskstate import TaskDefinition, Options
from apps.fireworks.fireworksenvironment import FireworksTaskEnvironment
from golem.network.p2p.node import Node
from golem.resource.dirmanager import DirManager
from golem.core.common import timeout_to_deadline, string_to_timeout,\
                              to_unicode, get_golem_path
from golem.docker.environment import DockerEnvironment
from golem.task.taskbase import Task, ResultType, TaskState, TaskBuilder, \
                                TaskTypeInfo, TaskDefaults, TaskHeader, \
                                AcceptClientVerdict
from golem.task.taskclient import TaskClient
from golem_verificator.verifier import SubtaskVerificationState
from golem_messages import idgenerator
logger = logging.getLogger(__name__)

class FireWorksTaskDefinition(TaskDefinition):
    def __init__(self):
        super().__init__()
        self.task_type = 'Fireworks'

class FireworksTaskTypeInfo(TaskTypeInfo):
    def __init__(self):
        super().__init__(
            "Fireworks",
            FireWorksTaskDefinition,
            TaskDefaults(),
            Options,
            FireworksTaskBuilder
        )


class BasicTaskBuilder(TaskBuilder):
    def __init__(self,
                 owner: Node,
                 task_definition: TaskDefinition,
                 dir_manager: DirManager) -> None:
        self.task_definition = task_definition
        self.root_path = dir_manager.root_path
        self.dir_manager = dir_manager
        self.owner = owner
        self.src_code = ""

def apply(obj, *initial_data, **kwargs):
    for dictionary in initial_data:
        for key in dictionary:
            setattr(obj, key, dictionary[key])
    for key in kwargs:
        setattr(obj, key, kwargs[key])


class FireworksTaskBuilder(BasicTaskBuilder):

    DEFAULT_LAUNCHPAD = {
        'host': 'localhost',
        'port': 27017,
        'name': 'fireworks',
        'username': None,
        'password': None,
        'logdir': None,
        'strm_lvl': 'INFO'
    }

    def __init__(self,
                 owner: Node,
                 task_definition: TaskDefinition,
                 dir_manager: DirManager) -> None:
        self.task_definition = task_definition
        self.dir_manager = dir_manager
        self.owner = owner

    def build(self) -> 'Task':
        if hasattr(self.task_definition, 'launchpad_path'):
            with open(self.task_definition.launchpad_path) as lf:
                launchpad_dict = yaml.load(lf)
        else:
            launchpad_dict = self.DEFAULT_LAUNCHPAD

        return FireworksTask(self.owner,
                             self.task_definition,
                             self.dir_manager,
                             LaunchPad.from_dict(launchpad_dict),
                             Workflow.from_file(self.task_definition.firework_path))

    @classmethod
    def build_definition(cls, task_type: TaskTypeInfo, dictionary,
                         minimal=False):
        """ Build task defintion from dictionary with described options.
        :param dict dictionary: described all options need to build a task
        :param bool minimal: if this option is set too True, then only minimal
        definition that can be used for task testing can be build. Otherwise
        all necessary options must be specified in dictionary
        """
        td = task_type.definition()
        apply(td, dictionary)
        td.timeout = string_to_timeout(dictionary['timeout'])
        td.subtask_timeout = string_to_timeout(dictionary['subtask_timeout'])
        return td

class FireworksBenchmarkTaskBuilder(FireworksTaskBuilder):

    BENCHMARK_FILE = 'fw_adder.yaml'

    def build(self) -> 'Task':
        launchpad_dict = self.DEFAULT_LAUNCHPAD
        firework_bench_path = os.path.join(get_golem_path(),
                                           'apps',
                                           'fireworks',
                                           'benchmark',
                                           'data',
                                           self.BENCHMARK_FILE)
        return FireworksTask(self.owner,
                             self.task_definition,
                             self.dir_manager,
                             LaunchPad.from_dict(launchpad_dict),
                             Workflow.from_file(firework_bench_path))


class DockerizedTask(Task):
    ENVIRONMENT_CLASS=DockerEnvironment

    def __init__(self,
                 owner: Node,
                 task_definition: TaskDefinition,
                 dir_manager: DirManager) -> None:

        self.environment = self.ENVIRONMENT_CLASS()

        if task_definition.docker_images:
            self.docker_images = task_definition.docker_images
        elif isinstance(self.environment, DockerEnvironment):
            self.docker_images = self.environment.docker_images
        else:
            self.docker_images = None

        th = TaskHeader(
            task_id=task_definition.task_id,
            environment=self.environment.get_id(),
            task_owner=owner,
            deadline=timeout_to_deadline(task_definition.timeout),
            subtask_timeout=task_definition.subtask_timeout,
            subtasks_count=task_definition.subtasks_count,
            resource_size=1024,
            estimated_memory=task_definition.estimated_memory,
            max_price=task_definition.max_price,
            concent_enabled=task_definition.concent_enabled,
        )
        with open(self.environment.main_program_file, 'r') as script_file:
            src_code = script_file.read()
        super().__init__(th, src_code, task_definition)

class FireworksTask(DockerizedTask):
    ENVIRONMENT_CLASS = FireworksTaskEnvironment

    def __init__(self,
                 owner: Node,
                 task_definition: TaskDefinition,
                 dir_manager: DirManager,
                 launchpad: LaunchPad,
                 workflow: Workflow) -> None:
        super().__init__(owner, task_definition, dir_manager)
        self.launchpad = launchpad
        self.workflow = workflow
        logger.warn(workflow)
        self.launchpad.add_wf(self.workflow)

        # Data structure used to determine sequence in a workflow
        self.links = copy.deepcopy(workflow.links)
        self.parent_links = copy.deepcopy(workflow.links.parent_links)

        self.work_queue = list()
        for firework in self.get_ready_fireworks():
            self.work_queue.append(firework.fw_id)
        self.subtasks_fireworks_mapping = {}
        self.progress = 0.0
        self.counting_nodes = set()

    def get_ready_fireworks(self):
        fws = []
        for fw in self.workflow.fws:
            if fw.state == 'READY':
                fws.append(fw)
        return fws

    def initialize(self, dir_manager):
        """Called after adding a new task, may initialize or create some resources
        or do other required operations.
        :param DirManager dir_manager: DirManager instance for accessing temp dir for this task
        """
        pass

    def _new_compute_task_def(self, subtask_id, extra_data,
                              perf_index=0):
        # TODO remove as many fields as possible
        ctd = golem_messages.message.ComputeTaskDef()
        ctd['task_id'] = self.header.task_id
        ctd['subtask_id'] = subtask_id
        ctd['extra_data'] = extra_data
        ctd['extra_data']['launchpad'] = self.launchpad.to_dict()
        fw_id = self.work_queue.pop(0)
        self.subtasks_fireworks_mapping[subtask_id] = fw_id
        logger.warn("Putting {} to work".format(fw_id))
        ctd['extra_data']['fw_id'] = fw_id
        ctd['short_description'] = self.short_extra_data_repr(extra_data)
        ctd['src_code'] = self.src_code
        ctd['performance'] = perf_index
        if self.docker_images:
            ctd['docker_images'] = [di.to_dict() for di in self.docker_images]
        ctd['deadline'] = min(timeout_to_deadline(self.header.subtask_timeout),
                              self.header.deadline)

        return ctd

    def create_subtask_id(self) -> str:
        return idgenerator.generate_new_id_from_id(self.header.task_id)

    def query_extra_data(self, perf_index: float, num_cores: int = 1,
                         node_id: Optional[str] = None,
                         node_name: Optional[str] = None) -> 'ExtraData':
        """ Called when a node asks with given parameters asks for a new
        subtask to compute.
        :param perf_index: performance that given node declares
        :param num_cores: number of cores that current node declares
        :param node_id: id of a node that wants to get a next subtask
        :param node_name: name of a node that wants to get a next subtask
        """
        # FIXME Make sure this is not called when all tasks are dispatched but none completed
        # verify calling criteria in upper level
        subtask_id = self.create_subtask_id()
        ctd = self._new_compute_task_def(subtask_id, dict(), 0)
        return Task.ExtraData(ctd=ctd)

    def query_extra_data_for_test_task(self) -> golem_messages.message.ComputeTaskDef:  # noqa pylint:disable=line-too-long
        pass

    def short_extra_data_repr(self, extra_data: Task.ExtraData) -> str:
        """ Should return a short string with general task description that may be used for logging or stats gathering.
        :param extra_data
        :return str:
        """
        return 'short extra data repr for fireworks task'


    def needs_computation(self) -> bool:
        """ Return information if there are still some subtasks that may be dispended
        :return bool: True if there are still subtask that should be computed, False otherwise
        """
        return self.work_queue

    def finished_computation(self) -> bool:
        """ Return information if tasks has been fully computed
        :return bool: True if there is all tasks has been computed and verified
        """
        return not self.work_queue and not self.subtasks_fireworks_mapping

    def computation_finished(self, subtask_id, task_result,
                             result_type=ResultType.DATA,
                             verification_finished=None):
        """ Inform about finished subtask
        :param subtask_id: finished subtask id
        :param task_result: task result, can be binary data or list of files
        :param result_type: ResultType representation
        """

        if verification_finished:
            verification_finished(subtask_id, SubtaskVerificationState.VERIFIED, task_result)
        parent_fw = self.subtasks_fireworks_mapping[subtask_id]
        for child_fw in self.links[parent_fw]:
            # Remove completed parent link from each affected children
            self.parent_links[child_fw].remove(parent_fw)
            # If there are no parent links it means that the firework is ready
            if not self.parent_links[child_fw]:
                logger.warn("Enqueued {}".format(child_fw))
                self.work_queue.append(child_fw)
        del self.subtasks_fireworks_mapping[subtask_id]
        if self.finished_computation():
            self.progress = 1.0

    def computation_failed(self, subtask_id):
        """ Inform that computation of a task with given id has failed
        :param subtask_id:
        """
        pass

    def verify_subtask(self, subtask_id):
        """ Verify given subtask
        :param subtask_id:
        :return bool: True if a subtask passed verification, False otherwise
        """
        return  True

    def verify_task(self):
        """ Verify whole task after computation
        :return bool: True if task passed verification, False otherwise
        """
        import pdb; pdb.set_trace()
        return  True

    def get_total_tasks(self) -> int:
        """ Return total number of tasks that should be computed
        :return int: number should be greater than 0
        """
        # TODO verify if fireworks gives a way to determine that in a dynamic workflow
        return self.task_definition.subtasks_count

    def get_active_tasks(self) -> int:
        """ Return number of tasks that are currently being computed
        :return int: number should be between 0 and a result of get_total_tasks
        """
        # TODO return information on how many tasks were dispatched to the workers
        import pdb; pdb.set_trace()
        return 1

    def get_tasks_left(self) -> int:
        """ Return number of tasks that still should be computed
        :return int: number should be between 0 and a result of get_total_tasks
        """
        # TODO analogical to get_total_tasks
        import pdb; pdb.set_trace()
        return 1 - self.get_active_tasks()

    def restart(self):
        """ Restart all subtask computation for this task """
        import pdb; pdb.set_trace()
        # TODO determine if needed and possible with fireworks
        return  # Implement in derived class

    def restart_subtask(self, subtask_id):
        """ Restart subtask with given id """
        import pdb; pdb.set_trace()
        # TODO determine if needed and possible with fireworks
        return  # Implement in derived class

    def abort(self):
        """ Abort task and all computations """
        import pdb; pdb.set_trace()
        # TODO determine how to do that 
        return  # Implement in derived class

    def get_progress(self) -> float:
        """ Return task computations progress
        :return float: Return number between 0.0 and 1.0.
        """
        # TODO analogical to get_total_tasks
        return self.progress

    def get_resources(self) -> list:
        """ Return list of files that are need to compute this task."""
        return self.task_definition.resources

    def update_task_state(self, task_state: TaskState):
        """Update some task information taking into account new state.
        :param TaskState task_state:
        """
        return  # Implement in derived class

    def get_trust_mod(self, subtask_id) -> int:
        """ Return trust modifier for given subtask. This number may be taken into account during increasing
        or decreasing trust for given node after successful or failed computation.
        :param subtask_id:
        :return int:
        """
        return 1.0

    def add_resources(self, resources: set):
        """ Add resources to a task
        :param resources:
        """
        import pdb; pdb.set_trace()
        return  # Implement in derived class

    def copy_subtask_results(
            self, subtask_id: int, old_subtask_info: dict, results: List[str]) \
            -> None:
        """
        Copy results of a single subtask from another task
        """
        import pdb; pdb.set_trace()
        raise NotImplementedError()

    def should_accept_client(self, node_id):
        if self.needs_computation():
            return AcceptClientVerdict.ACCEPTED
        else:
            return AcceptClientVerdict.SHOULD_WAIT
        

    def get_stdout(self, subtask_id) -> str:
        """ Return stdout received after computation of subtask_id, if there is no data available
        return empty string
        :param subtask_id:
        :return str:
        """
        import pdb; pdb.set_trace()
        return ""

    def get_stderr(self, subtask_id) -> str:
        """ Return stderr received after computation of subtask_id, if there is no data available
        return emtpy string
        :param subtask_id:
        :return str:
        """
        import pdb; pdb.set_trace()
        return ""

    def get_results(self, subtask_id) -> List:
        """ Return list of files containing results for subtask with given id
        :param subtask_id:
        :return list:
        """
        import pdb; pdb.set_trace()
        return []

    def result_incoming(self, subtask_id):
        """ Informs that a computed task result is being retrieved
        :param subtask_id:
        :return:
        """
        import pdb; pdb.set_trace()
        pass

    def get_output_names(self) -> List:
        """ Return list of files containing final import task results
        :return list:
        """
        return []

    def get_output_states(self) -> List:
        """ Return list of states of final task results
        :return list:
        """
        import pdb; pdb.set_trace()
        return []

    def to_dictionary(self):
        return {
            'id': to_unicode(self.header.task_id),
            'name': to_unicode(self.task_definition.name),
            'type': to_unicode(self.task_definition.task_type),
            'subtasks_count': self.get_total_tasks(),
            'progress': self.get_progress()
        }

    def accept_client(self, node_id):
        verdict = self.should_accept_client(node_id)

        if verdict == AcceptClientVerdict.ACCEPTED:
            client = TaskClient(node_id)
            client.start()
        self.counting_nodes.add(node_id)
        return verdict
