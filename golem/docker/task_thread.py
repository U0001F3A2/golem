import logging
import os
from pathlib import Path
from typing import ClassVar, Optional, TYPE_CHECKING, Tuple, Dict, Union, List


import requests

from golem.docker.image import DockerImage
from golem.docker.job import DockerJob
from golem.task.taskbase import ResultType
from golem.task.taskthread import TaskThread, JobException, TimeoutException
from golem.vm.memorychecker import MemoryChecker

if TYPE_CHECKING:
    from .manager import DockerManager  # noqa pylint:disable=unused-import


logger = logging.getLogger(__name__)


EXIT_CODE_MESSAGE = "Subtask computation failed with exit code {}"
EXIT_CODE_PROBABLE_CAUSES = {
    137: "probably killed by out-of-memory killer"
}


class ImageException(RuntimeError):
    pass


class DockerDirMapping:

    def __init__(self,   # pylint: disable=too-many-arguments
                 resources: str, temporary: str,
                 work: Path, output: Path, logs: Path) -> None:

        self.resources = resources
        self.temporary = temporary

        self.work: Path = work
        self.output: Path = output
        self.logs: Path = logs

    @classmethod
    def generate(cls, resources: str, temporary: str) -> 'DockerDirMapping':
        work = Path(temporary) / "work"
        output = Path(temporary) / "output"
        logs = output

        return cls(resources, temporary, work, output, logs)

    def mkdirs(self, exist_ok: bool = True) -> None:
        os.makedirs(self.resources, exist_ok=exist_ok)
        os.makedirs(self.temporary, exist_ok=exist_ok)

        self.work.mkdir(exist_ok=exist_ok)
        self.output.mkdir(exist_ok=exist_ok)
        self.logs.mkdir(exist_ok=exist_ok)


class DockerTaskThread(TaskThread):

    # These files will be placed in the output dir (self.tmp_path)
    # and will contain dumps of the task script's stdout and stderr.
    STDOUT_FILE = "stdout.log"
    STDERR_FILE = "stderr.log"

    docker_manager: ClassVar[Optional['DockerManager']] = None

    def __init__(self, subtask_id: str,  # pylint: disable=too-many-arguments
                 docker_images: List[Union[DockerImage, Dict, Tuple]],
                 src_code: str,
                 extra_data: Dict,
                 dir_mapping: DockerDirMapping,
                 timeout: int,
                 check_mem: bool = False) -> None:

        if not docker_images:
            raise AttributeError("docker images is None")
        super(DockerTaskThread, self).__init__(
            subtask_id, src_code, extra_data,
            dir_mapping.resources, dir_mapping.temporary,
            timeout)

        # Find available image
        self.image = None
        logger.debug("Checking docker images %s", docker_images)
        for img in docker_images:
            img = DockerImage.build(img)
            if img.is_available():
                self.image = img
                break

        self.job: Optional[DockerJob] = None
        print(self.image)

        self.check_mem = check_mem
        self.dir_mapping = dir_mapping

    @staticmethod
    def specify_dir_mapping(resources: str, temporary: str, work: str,
                            output: str, logs: str) -> DockerDirMapping:
        return DockerDirMapping(resources, temporary,
                                Path(work), Path(output), Path(logs))

    @staticmethod
    def generate_dir_mapping(resources: str,
                             temporary: str) -> DockerDirMapping:
        return DockerDirMapping.generate(resources, temporary)

    def run(self) -> None:
        try:
            if not self.image:
                raise JobException("None of the Docker images are available")

            if self.use_timeout and self.task_timeout < 0:
                raise TimeoutException()

            estm_mem = self._run_docker_job()

        except (requests.exceptions.ReadTimeout, TimeoutException) as exc:
            if not self.use_timeout:
                self._fail(exc)
                return

            failure = TimeoutException("Task timed out after {:.1f}s"
                                       .format(self.time_to_compute))
            failure.with_traceback(exc.__traceback__)
            self._fail(failure)

        except Exception as exc:  # pylint: disable=broad-except
            self._fail(exc)

        else:
            self._task_computed(estm_mem)

        finally:
            self.job = None

    def _run_docker_job(self) -> Optional[int]:
        self.dir_mapping.mkdirs()

        params = dict(
            image=self.image,
            script_src=self.src_code,
            parameters=self.extra_data,
            resources_dir=str(self.dir_mapping.resources),
            work_dir=str(self.dir_mapping.work),
            output_dir=str(self.dir_mapping.output),
            host_config=(self.docker_manager.container_host_config
                         if self.docker_manager else None),
        )

        with DockerJob(**params) as job, MemoryChecker(self.check_mem) as mc:
            self.job = job
            job.start()

            exit_code = job.wait()
            estm_mem = mc.estm_mem

            job.dump_logs(str(self.dir_mapping.logs / self.STDOUT_FILE),
                          str(self.dir_mapping.logs / self.STDERR_FILE))

            if exit_code != 0:
                std_err = (self.dir_mapping.logs / self.STDERR_FILE).read_text()
                logger.warning(f'Task stderr:\n{std_err}')
                raise JobException(self._exit_code_message(exit_code))

        return estm_mem

    def _task_computed(self, estm_mem: Optional[int]) -> None:
        out_files = [
            str(path) for path in self.dir_mapping.output.glob("*")
        ]
        self.result = {
            "data": out_files,
            "result_type": ResultType.FILES,
        }
        if estm_mem is not None:
            self.result = (self.result, estm_mem)
        self._deferred.callback(self)

    def get_progress(self):
        # TODO: make the container update some status file? Issue #56
        return 0.0

    def end_comp(self):
        try:
            self.job.kill()
        except AttributeError:
            pass
        except requests.exceptions.BaseHTTPError:
            if self.docker_manager:
                self.docker_manager.recover_vm_connectivity(self.job.kill)

    @staticmethod
    def _exit_code_message(exit_code):
        msg = EXIT_CODE_MESSAGE.format(exit_code)
        cause = EXIT_CODE_PROBABLE_CAUSES.get(exit_code)
        if not cause:
            return msg
        return "{} ({})".format(msg, cause)


class SgxDockerTaskThread(TaskThread):

    # These files will be placed in the output dir (self.tmp_path)
    # and will contain dumps of the task script's stdout and stderr.
    STDOUT_FILE = "stdout.log"
    STDERR_FILE = "stderr.log"

    docker_manager: ClassVar[Optional['DockerManager']] = None

    def __init__(self, subtask_id: str,  # pylint: disable=too-many-arguments
                 docker_images: List[Union[DockerImage, Dict, Tuple]],
                 src_code: str,
                 extra_data: Dict,
                 dir_mapping: DockerDirMapping,
                 timeout: int,
                 check_mem: bool = False) -> None:

        if not docker_images:
            raise AttributeError("docker images is None")
        super().__init__(
            subtask_id, src_code, extra_data,
            dir_mapping.resources, dir_mapping.temporary,
            timeout)
        print('SgxDockerTaskThread init')

        self.check_mem = check_mem
        self.dir_mapping = dir_mapping

    @staticmethod
    def specify_dir_mapping(resources: str, temporary: str, work: str,
                            output: str, logs: str) -> DockerDirMapping:
        return DockerDirMapping(resources, temporary,
                                Path(work), Path(output), Path(logs))

    @staticmethod
    def generate_dir_mapping(resources: str,
                             temporary: str) -> DockerDirMapping:
        return DockerDirMapping.generate(resources, temporary)

    def run(self) -> None:
        try:
            print('SgxDockerTaskThread run')
            estm_mem = self._run_docker_job()
        except (requests.exceptions.ReadTimeout, TimeoutException) as exc:
            if not self.use_timeout:
                self._fail(exc)
                return

            failure = TimeoutException("Task timed out after {:.1f}s"
                                       .format(self.time_to_compute))
            failure.with_traceback(exc.__traceback__)
            self._fail(failure)

        except Exception as exc:  # pylint: disable=broad-except
            self._fail(exc)

        else:
            self._task_computed(estm_mem)

        finally:
            self.job = None

    def _run_docker_job(self) -> Optional[int]:
        self.dir_mapping.mkdirs()

        # params = dict(
        #     image=self.image,
        #     script_src=self.src_code,
        #     parameters=self.extra_data,
        #     resources_dir=str(self.dir_mapping.resources),
        #     work_dir=str(self.dir_mapping.work),
        #     output_dir=str(self.dir_mapping.output),
        #     host_config=(self.docker_manager.container_host_config
        #                  if self.docker_manager else None),
        # )

        # put docker_blendertask to work as run_blender.py
        # put params to work as params.py
        # put self.src_code as blender_script.py

        # Save parameters in work_dir/PARAMS_FILE
        # params_file_path = self.dir_mapping.work / 'params.py'
        # with open(params_file_path, "wb") as params_file:
        #     for key, value in self.extra_data.items():
        #         line = "{} = {}\n".format(key, repr(value))
        #         params_file.write(bytearray(line, encoding='utf-8'))

        # Save the script in work_dir/TASK_SCRIPT
        # task_script_path = self.dir_mapping.work / 'blenderscript.py'
        # with open(task_script_path, "wb") as script_file:
        #     script_file.write(bytearray(self.extra_data['script_src'], "utf-8"))  # noqa

        # blender_script_path = self.dir_mapping.work / "docker_blendertask.py"
        # with open(blender_script_path, "w") as script_file:
        #     script_file.write(self.src_code)

        blender_script_path = self.dir_mapping.work / "blenderscript.py"
        with open(blender_script_path, "w") as script_file:
            script_file.write(self.extra_data['script_src'])

        print('scene_file', self.extra_data['scene_file'])
        scene_file = os.path.relpath(self.extra_data['scene_file'], DockerJob.RESOURCES_DIR)  # noqa

        from multiprocessing import cpu_count
        argv = [
            "{}".format('/blender/blender'),
            "-b", "{}/{}".format('/enc_input', scene_file),
            "-y",  # enable scripting by default
            "-P", "{}".format('/input/blenderscript.py'),
            "-o", "{}/{}_{}".format('/enc_output', self.extra_data['outfilebasename'], self.extra_data['start_task']),  # noqa
            "-noaudio",
            "-F", "{}".format(self.extra_data['output_format'].upper()),
            "-t", "{}".format(cpu_count()),
            "-f", "{}".format(','.join(map(str, self.extra_data['frames']))),
        ]

        argv = "\x00".join(argv) + "\x00"
        print('argv', argv)
        with open(self.dir_mapping.work / "argv", "w") as f:
            f.write(argv)

        from golem.sgx.agent import docker_run
        docker_run(
            'golem-sgx-blender-signed',
            self.extra_data['sgx_eas_key'],
            self.dir_mapping.work,
            self.dir_mapping.work,
            self.dir_mapping.resources,
            self.dir_mapping.output,
        )

        return None

    def _task_computed(self, estm_mem: Optional[int]) -> None:
        out_files = [
            str(path) for path in self.dir_mapping.output.glob("**/*")
            if path.is_file()
        ]
        self.result = {
            "data": out_files,
            "result_type": ResultType.FILES,
        }
        if estm_mem is not None:
            self.result = (self.result, estm_mem)
        print('result', self.result)
        self._deferred.callback(self)

    def get_progress(self):
        # TODO: make the container update some status file? Issue #56
        return 0.0

    def end_comp(self):
        logger.info("end_comp nope")

    @staticmethod
    def _exit_code_message(exit_code):
        msg = EXIT_CODE_MESSAGE.format(exit_code)
        cause = EXIT_CODE_PROBABLE_CAUSES.get(exit_code)
        if not cause:
            return msg
        return "{} ({})".format(msg, cause)
