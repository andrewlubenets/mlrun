# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import inspect
import sys
import uuid
from ast import literal_eval
from base64 import b64encode
from datetime import datetime
import json
import getpass
from copy import deepcopy
from os import environ
import pandas as pd
from io import StringIO

from ..db import get_run_db
from ..model import RunObject, ModelObj, RunTemplate, BaseMetadata, ImageBuilder
from ..secrets import SecretsStore
from ..utils import (run_keys, gen_md_table, dict_to_yaml, get_in,
                     update_in, logger, is_ipython)
from ..execution import MLClientCtx
from ..artifacts import TableArtifact
from ..lists import RunList
from .generators import GridGenerator, ListGenerator
from ..k8s_utils import k8s_helper
from ..builder import build_runtime


class RunError(Exception):
    pass


KFPMETA_DIR = environ.get('KFPMETA_OUT_DIR', '/')


class FunctionSpec(ModelObj):
    def __init__(self, command=None, args=None, image=None, rundb=None,
                 mode=None, workers=None):

        self.command = command or ''
        self.image = image or ''
        self.mode = mode or ''
        self.workers = workers
        self.args = args or []
        self.rundb = rundb or environ.get('MLRUN_META_DBPATH', '')


class RunRuntime(ModelObj):
    kind = 'base'
    _dict_fields = ['kind', 'metadata', 'spec', 'build']

    def __init__(self, metadata=None, spec=None, build=None):
        self._metadata = None
        self.metadata = metadata
        self.kfp = None
        self._spec = None
        self.spec = spec
        self._build = None
        self.build = build
        self._db_conn = None
        self._secrets = None
        self._k8s = None
        self._is_built = False

    @property
    def metadata(self) -> BaseMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        self._metadata = self._verify_dict(metadata, 'metadata', BaseMetadata)

    @property
    def spec(self) -> FunctionSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, 'spec', FunctionSpec)

    @property
    def build(self) -> ImageBuilder:
        return self._build

    @build.setter
    def build(self, build):
        self._build = self._verify_dict(build, 'build', ImageBuilder)

    def _get_k8s(self):
        if not self._k8s:
            self._k8s = k8s_helper()
        return self._k8s

    def set_label(self, key, value):
        self.metadata.labels[key] = str(value)
        return self

    def with_code(self, from_file='', body=None):
        if (not body and not from_file) or (from_file and from_file.endswith('.ipynb')):
            from nuclio import build_file
            name, spec, code = build_file(from_file)
            self.build.inline_code = get_in(spec, 'spec.build.functionSourceCode')
            return self

        if from_file:
            with open(from_file) as fp:
                body = fp.read()
        self.build.inline_code = b64encode(body.encode('utf-8')).decode('utf-8')
        return self

    def run(self, runspec: RunObject = None, handler=None, name: str = '',
            project: str = '', params: dict = None, inputs: dict = None,):
        """Run a local or remote task.

        :param runspec:    run template object or dict (see RunTemplate)
        :param handler:    pointer or name of a function handler
        :param name:       execution name
        :param project:    project name
        :param params:     input parameters (dict)
        :param inputs:     input objects (dict of key: path)
        :param rundb:      path/url to the metadata and artifact database
        :param mode:       special run mode, e.g. 'noctx', 'pass'

        :return: run context object (dict) with run metadata, results and status
        """

        def show(results, resp):
            # show ipython/jupyter result table widget
            if resp:
                results.append(resp)
            else:
                logger.info('no returned result (job may still be in progress)')
                results.append(runspec.to_dict())
            if is_ipython:
                results.show()
            return resp

        if runspec:
            runspec = deepcopy(runspec)
            if isinstance(runspec, str):
                runspec = literal_eval(runspec)

        if isinstance(runspec, RunTemplate):
            runspec = RunObject.from_template(runspec)
        if isinstance(runspec, dict) or runspec is None:
            runspec = RunObject.from_dict(runspec)
        runspec.metadata.name = name or runspec.metadata.name
        runspec.metadata.project = project or runspec.metadata.project
        runspec.spec.parameters = params or runspec.spec.parameters
        runspec.spec.inputs = inputs or runspec.spec.inputs

        if handler and self.kind not in ['handler', 'dask']:
            if inspect.isfunction(handler):
                handler = handler.__name__
            else:
                handler = str(handler)
        runspec.spec.handler = handler or runspec.spec.handler

        spec = runspec.spec
        if self.spec.mode in ['noctx', 'args']:
            params = spec.parameters or {}
            for k, v in params.items():
                self.spec.args += ['--{}'.format(k), str(v)]

        if spec.secret_sources:
            self._secrets = SecretsStore.from_dict(spec.to_dict())

        # update run metadata (uid, labels) and store in DB
        meta = runspec.metadata
        meta.uid = meta.uid or uuid.uuid4().hex
        logger.info('starting run {} uid={}'.format(meta.name, meta.uid))

        if self.spec.rundb:
            self._db_conn = get_run_db(self.spec.rundb).connect(self._secrets)

        meta.labels['kind'] = self.kind
        meta.labels['owner'] = meta.labels.get('owner', getpass.getuser())
        add_code_metadata(meta.labels)

        execution = MLClientCtx.from_dict(runspec.to_dict(),
                                          self._db_conn,
                                          autocommit=True)

        # form child run task generator from spec
        task_generator = None
        if spec.hyperparams:
            task_generator = GridGenerator(spec.hyperparams)
        elif spec.param_file:
            obj = execution.get_input('param_file.csv', spec.param_file)
            task_generator = ListGenerator(obj.get())

        if task_generator:
            # multiple runs (based on hyper params or params file)
            generator = task_generator.generate(runspec)
            results = self._run_many(generator, execution, runspec)
            self._results_to_iter(results, runspec, execution)
            resp = execution.to_dict()
            if resp and self.kfp:
                _write_kfpmeta(resp)
            result = show(results, resp)
        else:
            # single run
            results = RunList()
            try:
                self.store_run(runspec)
                resp = self._run(runspec, execution)
                if resp and self.kfp:
                    _write_kfpmeta(resp)
                result = show(results, self._post_run(resp, task=runspec))
            except RunError as err:
                logger.error(f'run error - {err}')
                result = show(results, self._post_run(task=runspec, err=err))

        if result:
            run = RunObject.from_dict(result)
            logger.info('run executed, status={}'.format(runspec.status.state))
            if runspec.status.state == 'error':
                raise RunError(runspec.status.error)
            return run

        return None

    def _get_db_run(self, task: RunObject = None):
        if self._db_conn and task:
            project = task.metadata.project
            uid = task.metadata.uid
            iter = task.metadata.iteration
            if iter:
                uid = '{}-{}'.format(uid, iter)
            return self._db_conn.read_run(uid, project, False)
        if task:
            return task.to_dict()

    def _get_cmd_args(self, runobj, with_mlrun):
        extra_env = {'MLRUN_EXEC_CONFIG': runobj.to_json()}
        if self.spec.rundb:
            extra_env['MLRUN_META_DBPATH'] = self.spec.rundb
        args = []
        command = self.spec.command
        if self.build.inline_code:
            extra_env['MLRUN_EXEC_CODE'] = self.build.inline_code
            if with_mlrun:
                command = 'mlrun'
                args = ['run', '--from-env']
        elif with_mlrun:
            command = 'mlrun'
            args = ['run', '--from-env', command]
        if runobj.spec.handler:
            args += ['--handler', runobj.spec.handler]
        if self.spec.args:
            args += self.spec.args
        return command, args

    def build_image(self, image, base_image=None, commands: list = None,
                    secret=None, with_mlrun=True, watch=True):
        self.build.image = image
        self.spec.image = ''
        if commands and isinstance(commands, list):
            self.build.commands = self.build.commands or []
            self.build.commands += commands
        if secret:
            self.build.secret = secret
        if base_image:
            self.build.base_image = base_image
        ready = self._build_image(watch, with_mlrun)
        return self

    def _build_image(self, watch=False, with_mlrun=True, execution=None):
        pod = self.build.build_pod
        if not self._is_built and pod:
            k8s = self._get_k8s()
            status = k8s.get_pod_status(pod)
            if status == 'succeeded':
                self.build.build_pod = None
                self._is_built = True
                logger.info('build completed successfully')
                return True
            if status in ['failed', 'error']:
                raise RunError(' build {}, watch the build pod logs: {}'.format(status, pod))
            logger.info('builder status is: {}, wait for it to complete'.format(status))
            return False

        if not self.build.commands and self.spec.mode == 'pass' and not self.build.source:
            if not self.spec.image and not self.build.base_image:
                raise RunError('image or base_image must be specified')
            self.spec.image = self.spec.image or self.build.base_image
        if self.spec.image:
            self._is_built = True
            return True

        if execution:
            execution.set_state('build')
        ready = build_runtime(self, with_mlrun, watch)
        self._is_built = ready

    def _run(self, runspec: RunObject, execution) -> dict:
        pass

    def _run_many(self, tasks, execution, runobj: RunObject) -> RunList:
        results = RunList()
        for task in tasks:
            try:
                self.store_run(task)
                resp = self._run(task, execution)
                resp = self._post_run(resp, task=task)
            except RunError as err:
                task.status.state = 'error'
                task.status.error = err
                resp = self._post_run(task=task, err=err)
            results.append(resp)
        return results

    def store_run(self, runobj: RunObject, commit=True):
        if self._db_conn and runobj:
            project = runobj.metadata.project
            uid = runobj.metadata.uid
            iter = runobj.metadata.iteration
            if iter:
                uid = '{}-{}'.format(uid, iter)
            self._db_conn.store_run(runobj.to_dict(), uid, project, commit)

    def _post_run(self, resp: dict = None, task: RunObject = None, err=None):
        """update the task state in the DB"""
        if resp is None and task:
            resp = self._get_db_run(task)

        if resp is None:
            return None

        if not isinstance(resp, dict):
            raise ValueError('post_run called with type {}'.format(type(resp)))

        updates = {'status.last_update': str(datetime.now())}
        if get_in(resp, 'status.state', '') != 'error' and not err:
            updates['status.state'] = 'completed'
            update_in(resp, 'status.state', 'completed')
        else:
            updates['status.state'] = 'error'
            update_in(resp, 'status.state', 'error')
            if err:
                update_in(resp, 'status.error', err)
            err = get_in(resp, 'status.error')
            if err:
                updates['status.error'] = err

        if self._db_conn:
            project = get_in(resp, 'metadata.project')
            uid = get_in(resp, 'metadata.uid')
            iter = get_in(resp, 'metadata.iteration', 0)
            if iter:
                uid = '{}-{}'.format(uid, iter)
            self._db_conn.update_run(updates, uid, project)

        return resp

    def _results_to_iter(self, results, runspec, execution):
        if not results:
            logger.error('got an empty results list in to_iter')
            return

        iter = []
        failed = 0
        for task in results:
            state = get_in(task, ['status', 'state'])
            id = get_in(task, ['metadata', 'iteration'])
            struct = {'param': get_in(task, ['spec', 'parameters'], {}),
                      'output': get_in(task, ['status', 'outputs'], {}),
                      'state': state,
                      'iter': id,
                      }
            if state == 'error':
                failed += 1
                err = get_in(task, ['status', 'error'], '')
                logger.error('error in task  {}:{} - {}'.format(
                    runspec.metadata.uid, id, err))

            self._post_run(task)
            iter.append(struct)

        df = pd.io.json.json_normalize(iter).sort_values('iter')
        header = df.columns.values.tolist()
        summary = [header] + df.values.tolist()
        item, id = selector(results, runspec.spec.selector)
        task = results[item] if id and results else None
        execution.log_iteration_results(id, summary, task)

        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False, line_terminator='\n', encoding='utf-8')
        execution.log_artifact(
            TableArtifact('iteration_results.csv',
                          body=csv_buffer.getvalue(),
                          header=header,
                          viewer='table'))
        if failed:
            execution.set_state(error='{} tasks failed, check logs for db for details'.format(failed))
        else:
            execution.set_state('completed')

    def _force_handler(self, handler):
        if not handler:
            raise RunError('handler must be provided for {} runtime'.format(self.kind))


def _write_kfpmeta(struct):
    if 'status' not in struct:
        return

    outputs = struct['status'].get('outputs', {})
    metrics = {'metrics':
                   [{'name': k,
                     'numberValue': v,
                     } for k, v in outputs.items() if isinstance(v, (int, float, complex))]}
    with open(KFPMETA_DIR + 'mlpipeline-metrics.json', 'w') as f:
        json.dump(metrics, f)

    output_artifacts = get_kfp_outputs(
        struct['status'].get(run_keys.output_artifacts, []))

    text = '# Run Report\n'
    if 'iterations' in struct['status']:
        iter = struct['status']['iterations']
        iter_html = gen_md_table(iter[0], iter[1:])
        text += '## Iterations\n' + iter_html
        struct = deepcopy(struct)
        del struct['status']['iterations']

    text += "## Metadata\n```yaml\n" + dict_to_yaml(struct) + "```\n"

    #with open('sum.md', 'w') as fp:
    #    fp.write(text)

    metadata = {
        'outputs': output_artifacts + [{
            'type': 'markdown',
            'storage': 'inline',
            'source': text
        }]
    }
    with open(KFPMETA_DIR + 'mlpipeline-ui-metadata.json', 'w') as f:
        json.dump(metadata, f)


def get_kfp_outputs(artifacts):
    outputs = []
    for output in artifacts:
        key = output["key"]
        target = output.get('target_path', '')
        target = output.get('inline', target)
        try:
            with open(f'/tmp/{key}', 'w') as fp:
                fp.write(target)
        except:
            pass

        if target.startswith('v3io:///'):
            target = target.replace('v3io:///', 'http://v3io-webapi:8081/')

        viewer = output.get('viewer', '')
        if viewer in ['web-app', 'chart']:
            meta = {'type': 'web-app',
                    'source': target}
            outputs += [meta]

        elif viewer == 'table':
            header = output.get('header', None)
            if header and target.endswith('.csv'):
                meta = {'type': 'table',
                        'format': 'csv',
                        'header': header,
                        'source': target}
                outputs += [meta]

    return outputs


def selector(results: list, criteria):
    if not criteria:
        return 0, 0

    idx = criteria.find('.')
    if idx < 0:
        op = 'max'
    else:
        op = criteria[:idx]
        criteria = criteria[idx + 1:]


    best_id = 0
    best_item = 0
    if op == 'max':
        best_val = sys.float_info.min
    elif op == 'min':
        best_val = sys.float_info.max
    else:
        logger.error('unsupported selector {}.{}'.format(op, criteria))
        return 0, 0

    i = 0
    for task in results:
        state = get_in(task, ['status', 'state'])
        id = get_in(task, ['metadata', 'iteration'])
        val = get_in(task, ['status', 'outputs', criteria])
        if state != 'error' and val is not None:
            if (op == 'max' and val > best_val) \
                    or (op == 'min' and val < best_val):
                best_id, best_item, best_val = id, i, val
        i += 1

    return best_item, best_id


def add_code_metadata(labels):
    dirpath = './'
    try:
        from git import Repo
        from git.exc import GitCommandError, InvalidGitRepositoryError
    except ImportError:
        return

    try:
        repo = Repo(dirpath, search_parent_directories=True)
        remotes = [remote.url for remote in repo.remotes]
        if len(remotes) > 0:
            set_if_none(labels, 'repo', remotes[0])
            set_if_none(labels, 'commit', repo.head.commit.hexsha)
    except (GitCommandError, InvalidGitRepositoryError):
        pass


def set_if_none(struct, key, value):
    if not struct.get(key):
        struct[key] = value
